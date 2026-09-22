"""Validated, per-profile Chrome process lifecycle operations."""

from __future__ import annotations

import ctypes
import logging
import psutil

from chrome_manager.db.database import Database
from chrome_manager.db.repository import Profile


class ProcessStopError(RuntimeError):
    """Raised when the recorded process cannot safely be stopped."""


class ProcessManager:
    def __init__(self, database: Database, shutdown_timeout: int) -> None:
        self.database = database
        self.shutdown_timeout = shutdown_timeout

    def reconcile(self) -> None:
        """Close stale running sessions without touching unrelated processes."""
        with self.database.read() as connection:
            sessions = connection.execute(
                "SELECT r.id, r.pid, r.process_create_time, r.profile_id FROM runtime_sessions r "
                "JOIN profiles p ON p.id=r.profile_id WHERE r.stopped_at IS NULL AND p.status='running'"
            ).fetchall()
        for session in sessions:
            try:
                process = psutil.Process(session['pid'])
                if session['process_create_time'] is None or abs(process.create_time() - session['process_create_time']) < 1:
                    continue
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                continue
            with self.database.transaction() as connection:
                connection.execute("UPDATE runtime_sessions SET stopped_at=CURRENT_TIMESTAMP, stop_reason='process_exited' WHERE id=?", (session['id'],))
                connection.execute("UPDATE profiles SET status='stopped' WHERE id=? AND status='running'", (session['profile_id'],))
                connection.execute("INSERT INTO events(profile_id,event_type,message) VALUES (?, 'process_exited', 'Chrome process exited outside manager')", (session['profile_id'],))
            logging.getLogger('chrome_manager.lifecycle').warning('实例 %s 的 Chrome 已退出，状态已同步', session['profile_id'])

    def stop_profile(self, profile: Profile) -> None:
        """Stop only the Chrome process whose command line matches this Profile."""
        with self.database.read() as connection:
            session = connection.execute(
                "SELECT id, pid, cdp_port, process_create_time FROM runtime_sessions WHERE profile_id = ? AND stopped_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (profile.id,),
            ).fetchone()
        if session is None or session["pid"] is None:
            raise ProcessStopError(f"Profile「{profile.name}」没有正在运行的受管进程")
        try:
            process = psutil.Process(session["pid"])
            if session['process_create_time'] is not None and abs(process.create_time() - session['process_create_time']) >= 1:
                raise ProcessStopError("记录的 PID 已被其他进程使用，已拒绝停止")
            expected_data_dir = f"--user-data-dir={profile.user_data_dir}"
            expected_port = f"--remote-debugging-port={session['cdp_port']}"
            if expected_data_dir not in process.cmdline() or expected_port not in process.cmdline():
                raise ProcessStopError("记录的 PID 与该 Profile 不匹配，已拒绝停止以保护其他 Chrome")
            # Ask Chrome to close its windows first so session data can be flushed.
            self._request_close(process.pid)
            try:
                process.wait(timeout=self.shutdown_timeout)
            except psutil.TimeoutExpired:
                logging.getLogger('chrome_manager.lifecycle').warning('实例 %s 未在超时内退出，将强制结束', profile.id)
                process.kill()
                process.wait(timeout=3)
        except psutil.NoSuchProcess:
            pass  # It was manually closed; safely reconcile the stale record.
        except psutil.AccessDenied as exc:
            raise ProcessStopError("Windows 拒绝访问该受管 Chrome 进程") from exc
        except psutil.TimeoutExpired as exc:
            raise ProcessStopError("Chrome 进程仍未退出，请稍后重试") from exc

        with self.database.transaction() as connection:
            connection.execute("UPDATE profiles SET status = 'stopped', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (profile.id,))
            connection.execute("UPDATE runtime_sessions SET stopped_at = CURRENT_TIMESTAMP, stop_reason = 'manager_stop' WHERE id = ?", (session["id"],))
            connection.execute("UPDATE cdp_ports SET last_stopped_at = CURRENT_TIMESTAMP WHERE profile_id = ?", (profile.id,))
            connection.execute("INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_stopped', ?)", (profile.id, "Chrome stopped by Chrome Manager"))
        logging.getLogger('chrome_manager.lifecycle').info('实例 %s 已停止', profile.id)

    @staticmethod
    def _request_close(pid: int) -> None:
        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        user32.PostMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def close_window(handle, _):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == pid:
                user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE
            return True

        user32.EnumWindows(close_window, 0)

    def focus_profile(self, profile: Profile) -> None:
        """Bring the verified managed Chrome window to the foreground on Windows."""
        with self.database.read() as connection:
            session = connection.execute(
                "SELECT pid, cdp_port FROM runtime_sessions WHERE profile_id = ? AND stopped_at IS NULL "
                "ORDER BY id DESC LIMIT 1", (profile.id,)
            ).fetchone()
        if session is None or session["pid"] is None:
            raise ProcessStopError(f"Profile「{profile.name}」没有正在运行的受管窗口")
        try:
            root = psutil.Process(session["pid"])
            expected_data_dir = f"--user-data-dir={profile.user_data_dir}"
            expected_port = f"--remote-debugging-port={session['cdp_port']}"
            if expected_data_dir not in root.cmdline() or expected_port not in root.cmdline():
                raise ProcessStopError("记录的 PID 与该 Profile 不匹配，已拒绝切换窗口")
            process_ids = {root.pid, *(child.pid for child in root.children(recursive=True))}
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            raise ProcessStopError("无法访问该 Profile 的 Chrome 进程") from exc

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
        user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        user32.SetWindowPos.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        )
        window = ctypes.c_void_p()

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def find_window(handle: int, _: int) -> bool:
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
            if process_id.value in process_ids and user32.IsWindowVisible(handle):
                window.value = handle
                return False
            return True

        user32.EnumWindows(find_window, 0)
        if not window.value:
            raise ProcessStopError("未找到该 Profile 的可见 Chrome 窗口")
        top_flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        if user32.GetForegroundWindow() == window.value:
            user32.SetWindowPos(window, ctypes.c_void_p(0), 0, 0, 0, 0, top_flags)  # HWND_TOP
            return
        user32.ShowWindow(window, 9)  # SW_RESTORE
        user32.SetWindowPos(window, ctypes.c_void_p(0), 0, 0, 0, 0, top_flags)  # HWND_TOP
        if not user32.SetForegroundWindow(window) and user32.GetForegroundWindow() != window.value:
            raise ProcessStopError("Windows 拒绝将 Chrome 窗口切换到前台")
