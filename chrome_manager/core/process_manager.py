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
        user32.GetWindow.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        user32.GetWindow.restype = ctypes.c_void_p
        user32.GetClassNameW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
        user32.IsIconic.argtypes = (ctypes.c_void_p,)
        user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
        user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        user32.SetForegroundWindow.restype = ctypes.c_bool
        user32.BringWindowToTop.argtypes = (ctypes.c_void_p,)
        user32.SetActiveWindow.argtypes = (ctypes.c_void_p,)
        user32.SetFocus.argtypes = (ctypes.c_void_p,)
        user32.SetWindowPos.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        )
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.AttachThreadInput.argtypes = (ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool)
        user32.AttachThreadInput.restype = ctypes.c_bool
        user32.AllowSetForegroundWindow.argtypes = (ctypes.c_ulong,)
        user32.AllowSetForegroundWindow.restype = ctypes.c_bool
        user32.PeekMessageW.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
        candidates: list[tuple[int, int]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def find_window(handle: int, _: int) -> bool:
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
            if (process_id.value in process_ids and user32.IsWindowVisible(handle)
                    and not user32.GetWindow(handle, 4)):  # GW_OWNER: skip Chrome popups/tooltips
                class_name = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(handle, class_name, len(class_name))
                rank = 0 if class_name.value == 'Chrome_WidgetWin_1' else 1
                candidates.append((rank, handle))
            return True

        user32.EnumWindows(find_window, 0)
        if not candidates:
            raise ProcessStopError("未找到该 Profile 的可见 Chrome 窗口")
        window = ctypes.c_void_p(min(candidates, key=lambda candidate: candidate[0])[1])
        def window_owner(handle: int | None) -> tuple[int, int]:
            owner = ctypes.c_ulong()
            thread = user32.GetWindowThreadProcessId(handle, ctypes.byref(owner)) if handle else 0
            return owner.value, thread

        target_thread = window_owner(window.value)[1]
        foreground = user32.GetForegroundWindow()
        foreground_thread = window_owner(foreground)[1]
        current_thread = kernel32.GetCurrentThreadId()

        class Message(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint), ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long),
            ]

        # A Uvicorn worker thread may not yet have a Win32 message queue; AttachThreadInput
        # fails for such threads. Create one before temporarily sharing the input queues.
        message = Message()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        user32.AllowSetForegroundWindow(root.pid)
        attached: list[tuple[int, int]] = []
        for thread_id in dict.fromkeys((foreground_thread, target_thread)):
            if thread_id and thread_id != current_thread and user32.AttachThreadInput(current_thread, thread_id, True):
                attached.append((current_thread, thread_id))

        try:
            # Restore even when the window is visible but covered by another window.
            user32.ShowWindow(window, 9)  # SW_RESTORE
            top_flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
            user32.SetWindowPos(window, ctypes.c_void_p(0), 0, 0, 0, 0, top_flags)
            user32.BringWindowToTop(window)
            user32.SetActiveWindow(window)
            user32.SetFocus(window)
            user32.SetForegroundWindow(window)
            # Verify the foreground owner, rather than trusting the API's return value alone.
            foreground = user32.GetForegroundWindow()
            foreground_owner = window_owner(foreground)[0]
            if foreground_owner not in process_ids:
                user32.AllowSetForegroundWindow(root.pid)
                user32.BringWindowToTop(window)
                user32.SetForegroundWindow(window)
                foreground = user32.GetForegroundWindow()
                foreground_owner = window_owner(foreground)[0]
            if foreground_owner not in process_ids:
                logging.getLogger('chrome_manager.lifecycle').error(
                    '无法将实例 %s 的 Chrome 窗口激活到前台：目标 HWND=%s，目标线程=%s，当前前台线程=%s',
                    profile.id, window.value, target_thread, foreground_thread,
                )
                raise ProcessStopError("Windows 仍阻止窗口前台激活，请检查是否以不同权限运行 Chrome Manager 和 Chrome")
        finally:
            for source_thread, destination_thread in reversed(attached):
                user32.AttachThreadInput(source_thread, destination_thread, False)
