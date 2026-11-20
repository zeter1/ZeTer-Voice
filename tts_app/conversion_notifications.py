from __future__ import annotations

from .i18n import tr
from .capture_notification import CaptureNotificationCard

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk


def notification_work_area(root: tk.Tk) -> tuple[int, int, int, int]:
    """Use the main window's monitor, excluding the Windows taskbar."""
    if os.name == "nt":
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        monitor = user32.MonitorFromWindow(root.winfo_id(), 2)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        rect = info.rcWork
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def reveal_audio_file(path: Path) -> None:
    """Select the output without creating directories or changing any files."""
    path = path.resolve()
    if os.name == "nt" and path.is_file():
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    elif path.parent.is_dir():
        if os.name == "nt":
            os.startfile(str(path.parent))
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    else:
        raise FileNotFoundError(tr("Файл и его папка больше недоступны:\n") + str(path))


class ConversionNotifications:
    """Independent permanent windows; every method runs on the owning Tk thread.

    No timer, grab, transient owner, or focus request: other tasks keep running
    and minimizing the main window does not hide the notifications.
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.thread_id = threading.get_ident()
        self.windows: list[tk.Toplevel] = []
        self._capture_card: CaptureNotificationCard | None = None
        self._capture_failure_count = 0

    def show(self, output: Path, note: str = "") -> None:
        area = self._work_area()
        window, body = self._new_card(tr("MP3 готов"), tr("Аудиофайл готов"))
        width = min(400, area[2] - area[0] - 24)
        # Keep controls reachable for long Windows paths; the action retains
        # the complete path, while the label shows the identifying suffix.
        name = output.name if len(output.name) <= 100 else output.name[:80] + "…" + output.suffix
        folder = str(output.parent)
        if len(folder) > 120:
            folder = "…" + folder[-120:]
        for text in (name, folder, note):
            if text:
                ttk.Label(body, text=text, wraplength=max(100, width - 32),
                          justify="left").pack(fill="x", pady=(6, 0))
        ttk.Button(body, text=tr("Показать файл"),
                   command=lambda: self.reveal(window, output)).pack(anchor="w", pady=(10, 0))
        self._present(window, area, width)

    def show_capture_failure(self, hotkey: str, message: str, *,
                             source_process: str = "", insertion_uncertain: bool = False) -> None:
        area = self._work_area()
        self._capture_failure_count += 1
        width = min(400, area[2] - area[0] - 24)
        card = self._capture_card
        if card is None:
            window = self._new_window()
            try:
                card = CaptureNotificationCard(window, lambda: self.dismiss(window))
                card.update(hotkey, message, width, self._capture_failure_count,
                            source_process=source_process, insertion_uncertain=insertion_uncertain)
            except (OSError, tk.TclError):
                window.destroy()
                raise
            self._present(window, area, width)
            self._capture_card = card
        else:
            card.update(hotkey, message, width, self._capture_failure_count,
                        source_process=source_process, insertion_uncertain=insertion_uncertain)
            card.window.update_idletasks()
            self._reflow(area)

    def _work_area(self) -> tuple[int, int, int, int]:
        if threading.get_ident() != self.thread_id:
            raise RuntimeError("Уведомления должны создаваться в потоке Tk.")
        return notification_work_area(self.root)

    def _new_window(self) -> tk.Toplevel:
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        # Ignore WM close / Alt+F4: only the visible cross dismisses this item.
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        return window

    def _new_card(self, title: str, heading: str) -> tuple[tk.Toplevel, ttk.Frame]:
        window = self._new_window()
        window.title(title)

        body = ttk.Frame(window, padding=12, relief="solid", borderwidth=1)
        body.pack(fill="both", expand=True)
        header = ttk.Frame(body)
        header.pack(fill="x")
        ttk.Button(header, text="×", width=3,
                   command=lambda: self.dismiss(window)).pack(side="right")
        ttk.Label(header, text=heading, font=("Segoe UI", 11, "bold")).pack(side="left")
        return window, body

    def _present(self, window: tk.Toplevel, area: tuple[int, int, int, int], width: int) -> None:
        self.windows.append(window)
        try:
            window.geometry(f"{width}x1")
            window.update_idletasks()
            self._reflow(area)
        except (OSError, tk.TclError):
            self.windows.remove(window)
            window.destroy()
            raise

    def dismiss(self, window: tk.Toplevel) -> None:
        if window not in self.windows:
            return
        self.windows.remove(window)
        if self._capture_card is not None and window is self._capture_card.window:
            self._capture_card = None
            self._capture_failure_count = 0
        window.destroy()
        self._reflow()

    def reveal(self, window: tk.Toplevel, output: Path) -> None:
        try:
            reveal_audio_file(output)
        except OSError as exc:
            messagebox.showerror(tr("Не удалось открыть расположение файла"), str(exc), parent=window)
        # Opening Explorer, including a failed attempt, never dismisses the item.

    def _reflow(self, area: tuple[int, int, int, int] | None = None) -> None:
        if not self.windows:
            return
        left, top, right, bottom = area or notification_work_area(self.root)
        margin = 12
        width = min(400, right - left - margin * 2)
        x, y = right - width - margin, bottom - margin
        for window in reversed(self.windows):
            height = window.winfo_reqheight()
            if y - height < top + margin:
                x = max(left + margin, x - width - margin)
                y = bottom - margin
            y -= height
            window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
            window.deiconify()
            if os.name == "nt":
                # Tk negative geometry means distance from the right/bottom edge.
                # Native coordinates also handle monitors left/above the primary.
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.GetParent.argtypes = [wintypes.HWND]
                user32.GetParent.restype = wintypes.HWND
                user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                               ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
                user32.SetWindowPos.restype = wintypes.BOOL
                hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
                if not user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0015):
                    raise ctypes.WinError(ctypes.get_last_error())
            y -= margin
