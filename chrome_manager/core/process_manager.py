"""Validated, per-profile Chrome process lifecycle operations."""

from __future__ import annotations

import logging
import subprocess
import psutil
import time
import os
from pathlib import Path

from chrome_manager.db.database import Database
from chrome_manager.db.repository import Profile
from chrome_manager.utils.audit import event, operation, step
from chrome_manager.core.cdp import probe_browser
from chrome_manager.core.cdp_ports import browser_port
from chrome_manager.core.linux_runtime import chrome_root, systemctl, unit_name, close_process, focus_window
from chrome_manager.core.linux_runtime import matches_profile
from chrome_manager.config import Settings


class ProcessStopError(RuntimeError):
    """Raised when the recorded process cannot safely be stopped."""


class ProcessManager:
    def __init__(self, database: Database, shutdown_timeout: int, settings: Settings | None = None) -> None:
        self.database = database
        self.shutdown_timeout = shutdown_timeout
        self.settings = settings or Settings(database.path.parent.parent)

    def recover_interrupted_states(self) -> None:
        """Run only at manager startup, never during an active API start/stop."""
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT id,status FROM profiles WHERE is_deleted=0 AND status IN ('starting','stopping')").fetchall()
            for row in rows:
                connection.execute("UPDATE profiles SET status=CASE WHEN EXISTS (SELECT 1 FROM runtime_sessions WHERE profile_id=? AND stopped_at IS NULL) THEN 'running' ELSE 'stopped' END WHERE id=?", (row['id'], row['id']))
                connection.execute("INSERT INTO events(profile_id,event_type,message) VALUES (?,'interrupted_state_recovered',?)",
                                   (row['id'], f"Manager restarted during {row['status']}; reconcile before reattachment"))
                event('browser.interrupted_state.recovered', level=logging.WARNING,
                      profile_id=row['id'], previous_state=row['status'])

    def recover_restarted(self, profiles: list[Profile]) -> None:
        """Reattach only a uniquely identified Chrome root with verified CDP PID.

        Chrome can replace its PID on restart/update outside the manager. Never
        adopt by port alone or interfere with an in-progress manager start/stop.
        """
        eligible = [p for p in profiles if p.status == 'stopped' and p.cdp_port and not p.is_deleted]
        if not eligible:
            return
        candidates = []
        for process in psutil.process_iter(['name', 'cmdline']):
            try:
                args = process.info['cmdline'] or []
                if not chrome_root(process, args) or any(a.startswith('--type=') for a in args):
                    continue
                candidates.append((process, args))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for profile in eligible:
            internal_port = browser_port(self.database, profile)
            matches = []
            for process, args in candidates:
                if matches_profile(process, profile.user_data_dir, internal_port):
                    matches.append(process)
            if len(matches) != 1:
                continue
            process = matches[0]
            try:
                created = process.create_time()
                probe_browser(internal_port, 2, process.pid)
                if not process.is_running() or process.create_time() != created:
                    continue
            except Exception:
                event('browser.recover.verification_failed', level=logging.WARNING,
                      profile_id=profile.id, browser_pid=process.pid, port=profile.cdp_port, exc_info=True)
                continue
            with self.database.transaction() as connection:
                changed = connection.execute(
                    "UPDATE profiles SET status='running', updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND status='stopped' AND is_deleted=0",
                    (profile.id,),
                ).rowcount
                if not changed:
                    continue
                connection.execute("UPDATE runtime_sessions SET stopped_at=CURRENT_TIMESTAMP,stop_reason='verified_reattach' WHERE profile_id=? AND stopped_at IS NULL", (profile.id,))
                connection.execute(
                    "INSERT INTO runtime_sessions(profile_id,pid,process_create_time,cdp_port,started_at) "
                    "VALUES (?,?,?,?,datetime(?,'unixepoch'))",
                    (profile.id, process.pid, created, internal_port, created),
                )
                connection.execute(
                    "UPDATE cdp_ports SET last_pid=?,last_started_at=datetime(?,'unixepoch') WHERE profile_id=?",
                    (process.pid, created, profile.id),
                )
                connection.execute("INSERT INTO events(profile_id,event_type,message) VALUES (?,'chrome_reattached',?)",
                                   (profile.id, f'Reattached verified Chrome PID {process.pid} on port {profile.cdp_port}'))
            event('browser.recover.completed', level=logging.INFO, profile_id=profile.id,
                  browser_pid=process.pid, port=profile.cdp_port, process_create_time=created)

    def reconcile(self) -> None:
        """Close stale running sessions without touching unrelated processes."""
        with self.database.transaction() as connection:
            connection.execute("UPDATE profiles SET status='stopped' WHERE is_deleted=0 AND status='running' AND NOT EXISTS (SELECT 1 FROM runtime_sessions r WHERE r.profile_id=profiles.id AND r.stopped_at IS NULL)")
        with self.database.read() as connection:
            sessions = connection.execute(
                "SELECT r.id, r.pid, r.process_create_time, r.profile_id FROM runtime_sessions r "
                "JOIN profiles p ON p.id=r.profile_id WHERE r.stopped_at IS NULL AND p.status='running'"
            ).fetchall()
        for session in sessions:
            try:
                process = psutil.Process(session['pid'])
                if process.status() != psutil.STATUS_ZOMBIE and (session['process_create_time'] is None or abs(process.create_time() - session['process_create_time']) < 1):
                    continue
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                event('browser.reconcile.access_denied', level=logging.WARNING,
                      profile_id=session['profile_id'], browser_pid=session['pid'], exc_info=True)
                continue
            with step('browser.reconcile.stale_session', profile_id=session['profile_id'],
                      browser_pid=session['pid'], session_id=session['id']), self.database.transaction() as connection:
                connection.execute("UPDATE runtime_sessions SET stopped_at=CURRENT_TIMESTAMP, stop_reason='process_exited' WHERE id=?", (session['id'],))
                stopped = connection.execute(
                    "UPDATE profiles SET status='stopped' WHERE id=? AND status='running' "
                    "AND NOT EXISTS (SELECT 1 FROM runtime_sessions WHERE profile_id=? AND stopped_at IS NULL)",
                    (session['profile_id'], session['profile_id']),
                ).rowcount
                connection.execute("INSERT INTO events(profile_id,event_type,message) VALUES (?, 'process_exited', 'Chrome process exited outside manager')", (session['profile_id'],))
            logging.getLogger('chrome_manager.lifecycle').warning('实例 %s 的 Chrome 已退出，状态已同步', session['profile_id'])

    def stop_profile(self, profile: Profile) -> None:
        """Stop only the Chrome process whose command line matches this Profile."""
        with operation('browser.stop', profile_id=profile.id, profile_name=profile.name,
                       port=profile.cdp_port):
            self._stop_profile(profile)

    def _stop_profile(self, profile: Profile) -> None:
        with step('browser.stop.find_session'), self.database.read() as connection:
            session = connection.execute(
                "SELECT id, pid, cdp_port, process_create_time FROM runtime_sessions WHERE profile_id = ? AND stopped_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (profile.id,),
            ).fetchone()
        if session is None or session["pid"] is None:
            raise ProcessStopError(f"Profile「{profile.name}」没有正在运行的受管进程")
        event('browser.stop.session', browser_pid=session['pid'], session_id=session['id'],
              process_create_time=session['process_create_time'])
        try:
            with step('browser.stop.verify_process', browser_pid=session['pid']):
                process = psutil.Process(session["pid"])
                if session['process_create_time'] is None or not matches_profile(process, profile.user_data_dir, session['cdp_port'], session['process_create_time']):
                    raise ProcessStopError('进程身份不匹配，拒绝停止')
                if session['process_create_time'] is not None and abs(process.create_time() - session['process_create_time']) >= 1:
                    raise ProcessStopError("记录的 PID 已被其他进程使用，已拒绝停止")
            with step('browser.stop.request_close', browser_pid=process.pid) as result:
                if self.settings.runtime == 'systemd':
                    systemctl('stop', unit_name(profile.id), timeout=self.shutdown_timeout + 15)
                    result['unit_stopped'] = True
                else:
                    result['process_notified'] = self._request_close(process.pid)
            try:
                started = time.perf_counter()
                event('browser.stop.wait.begin', browser_pid=session['pid'], timeout_seconds=self.shutdown_timeout)
                exit_code = process.wait(timeout=self.shutdown_timeout)
                event('browser.stop.wait.completed', browser_pid=session['pid'], exit_code=exit_code,
                      duration_ms=round((time.perf_counter() - started) * 1000, 2))
            except psutil.TimeoutExpired:
                event('browser.stop.wait.timeout', level=logging.WARNING, browser_pid=session['pid'],
                      timeout_seconds=self.shutdown_timeout, exc_info=True)
                logging.getLogger('chrome_manager.lifecycle').warning('实例 %s 未在超时内退出，将强制结束', profile.id)
                with step('browser.stop.kill', browser_pid=session['pid']):
                    process.kill()
                # Wait for termination to be observable before changing DB state.
                with step('browser.stop.wait_after_kill', browser_pid=session['pid']) as result:
                    result['exit_code'] = process.wait(timeout=max(10, self.shutdown_timeout))
        except psutil.NoSuchProcess:
            event('browser.stop.already_exited', browser_pid=session['pid'])
            pass  # It was manually closed; safely reconcile the stale record.
        except psutil.AccessDenied as exc:
            raise ProcessStopError("Linux 拒绝访问该受管 Chrome 进程") from exc
        except psutil.TimeoutExpired as exc:
            raise ProcessStopError("Chrome 进程仍未退出，请稍后重试") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProcessStopError(f'Linux 停止操作失败：{exc}') from exc

        with step('browser.stop.record_stopped', browser_pid=session['pid']), self.database.transaction() as connection:
            connection.execute("UPDATE runtime_sessions SET stopped_at = CURRENT_TIMESTAMP, stop_reason = 'manager_stop' WHERE id = ?", (session["id"],))
            connection.execute(
                "UPDATE profiles SET status='stopped', updated_at=CURRENT_TIMESTAMP WHERE id=? "
                "AND NOT EXISTS (SELECT 1 FROM runtime_sessions WHERE profile_id=? AND stopped_at IS NULL)",
                (profile.id, profile.id),
            )
            connection.execute("UPDATE cdp_ports SET last_stopped_at = CURRENT_TIMESTAMP WHERE profile_id = ?", (profile.id,))
            connection.execute("INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_stopped', ?)", (profile.id, "Chrome stopped by Chrome Manager"))
        logging.getLogger('chrome_manager.lifecycle').info('实例 %s 已停止', profile.id)

    @staticmethod
    def _request_close(pid: int) -> int:
        return close_process(pid)

    def focus_profile(self, profile: Profile) -> None:
        from chrome_manager.core.linux_runtime import matches_profile
        with self.database.read() as connection:
            session = connection.execute('SELECT pid,cdp_port,process_create_time FROM runtime_sessions WHERE profile_id=? AND stopped_at IS NULL ORDER BY id DESC LIMIT 1', (profile.id,)).fetchone()
        if not session:
            raise ProcessStopError('实例没有运行中的浏览器')
        try:
            process = psutil.Process(session['pid'])
            if not matches_profile(process, profile.user_data_dir, session['cdp_port'], session['process_create_time']):
                raise ProcessStopError('进程身份不匹配，拒绝操作窗口')
            focus_window(process.pid)
        except (OSError, subprocess.SubprocessError, psutil.Error) as exc:
            raise ProcessStopError(str(exc)) from exc
