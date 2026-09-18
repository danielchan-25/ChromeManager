"""Validated, per-profile Chrome process lifecycle operations."""

from __future__ import annotations

import ctypes
import psutil

from chrome_manager.db.database import Database
from chrome_manager.db.repository import Profile


class ProcessStopError(RuntimeError):
    """Raised when the recorded process cannot safely be stopped."""


class ProcessManager:
    def __init__(self, database: Database, shutdown_timeout: int) -> None:
        self.database = database
        self.shutdown_timeout = shutdown_timeout

    def stop_profile(self, profile: Profile) -> None:
        """Stop only the Chrome process whose command line matches this Profile."""
        with self.database.read() as connection:
            session = connection.execute(
                "SELECT id, pid, cdp_port FROM runtime_sessions WHERE profile_id = ? AND stopped_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (profile.id,),
            ).fetchone()
        if session is None or session["pid"] is None:
            raise ProcessStopError(f"Profile「{profile.name}」没有正在运行的受管进程")
        try:
            process = psutil.Process(session["pid"])
            expected_data_dir = f"--user-data-dir={profile.user_data_dir}"
            expected_port = f"--remote-debugging-port={session['cdp_port']}"
            if expected_data_dir not in process.cmdline() or expected_port not in process.cmdline():
                raise ProcessStopError("记录的 PID 与该 Profile 不匹配，已拒绝停止以保护其他 Chrome")
            process.terminate()
            try:
                process.wait(timeout=self.shutdown_timeout)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        except psutil.NoSuchProcess:
            pass  # It was manually closed; safely reconcile the stale record.
        except psutil.AccessDenied as exc:
            raise ProcessStopError("Windows 拒绝访问该受管 Chrome 进程") from exc

        with self.database.transaction() as connection:
            connection.execute("UPDATE profiles SET status = 'stopped', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (profile.id,))
            connection.execute("UPDATE runtime_sessions SET stopped_at = CURRENT_TIMESTAMP, stop_reason = 'manager_stop' WHERE id = ?", (session["id"],))
            connection.execute("UPDATE cdp_ports SET last_stopped_at = CURRENT_TIMESTAMP WHERE profile_id = ?", (profile.id,))
            connection.execute("INSERT INTO events(profile_id, event_type, message) VALUES (?, 'chrome_stopped', ?)", (profile.id, "Chrome stopped by Chrome Manager"))

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
