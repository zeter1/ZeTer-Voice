from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from ctypes import wintypes
from pathlib import PureWindowsPath

from .runtime import CLIPBOARD_COPY_TIMEOUT_SEC, CLIPBOARD_POLL_INTERVAL_MS, tk
from .windows_hotkey_hook import WM_COPY_SELECTION, WindowsHotkeyHookMixin


class GUIThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class WindowsCopyCapture(WindowsHotkeyHookMixin):
    """One keyboard hook, clipboard listener and delivery queue for the Tk root."""

    def __init__(self, root):
        self.root = root
        self._lock = threading.Lock()
        self._registrations = ()
        self._pending = None
        self._unresolved = {}
        self._events = queue.SimpleQueue()
        self._stop_event = threading.Event()
        self._hook_ready_event = threading.Event()
        self._reader_ready_event = threading.Event()
        self._thread_id = None
        self._hook_id = None
        self._reader_hwnd = None
        self._poll_id = None

    def start(self):
        self.root.winfo_id()
        threading.Thread(target=self._clipboard_listener_loop, daemon=True).start()
        if not self._reader_ready_event.wait(1.5) or not self._reader_hwnd:
            raise RuntimeError("Windows не смог запустить наблюдение за буфером обмена.")
        threading.Thread(target=self._windows_keyboard_hook_loop, daemon=True).start()
        if not self._hook_ready_event.wait(1.5) or not self._hook_id:
            raise RuntimeError("Windows не смог запустить глобальные горячие клавиши.")
        self._poll_id = self.root.after(30, self._poll_events)

    def register(self, monitor):
        with self._lock:
            registration = (
                monitor,
                frozenset(monitor.hotkey_vks),
                frozenset(monitor.hotkey_modifiers),
                monitor._hotkey_is_plain_ctrl_c(),
            )
            self._registrations = (*self._registrations, registration)

    def unregister(self, monitor):
        with self._lock:
            self._registrations = tuple(item for item in self._registrations if item[0] is not monitor)
            if self._pending is not None and self._pending["monitor"] is monitor:
                self._abandon_pending("binding_changed")

    def stop(self):
        self._stop_event.set()
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        user32 = ctypes.windll.user32
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._reader_hwnd:
            self._wake_reader()

    def _poll_events(self):
        self._poll_id = None
        delay = 30
        try:
            with self._lock:
                if self._pending is not None and time.perf_counter() >= self._pending["deadline"]:
                    self._abandon_pending("clipboard_timeout")
            for _ in range(8):
                if self._stop_event.is_set():
                    break
                try:
                    monitor, info = self._events.get_nowait()
                except queue.Empty:
                    break
                if monitor.enabled and monitor._binding_generation == info["binding_generation"]:
                    info["source_process"] = self._process_name_from_pid(info["source_pid"])
                    monitor.callback(info)
            else:
                delay = 1
        finally:
            if not self._stop_event.is_set():
                self._poll_id = self.root.after(delay, self._poll_events)

    def _report(self, monitor, context, *, reason=None, sequence=None, text=None):
        if monitor is not None:
            self._events.put((monitor, {
                **context,
                "capture_rejected": reason,
                "clipboard_sequence_after": sequence,
                "clipboard_changed": text is not None,
                "captured_text": text,
                "copy_latency_ms": round((time.perf_counter() - context["trigger_perf"]) * 1000, 1),
            }))

    def _abandon_pending(self, reason, *, unresolved=True):
        pending = self._pending
        self._pending = None
        context = pending["context"]
        if unresolved and pending["sent"]:
            self._mark_unresolved(context["source_pid"], self._clipboard_sequence_number()
                                  or context["clipboard_sequence_before"])
        self._report(pending["monitor"], context, reason=reason)

    def _mark_unresolved(self, pid, baseline):
        _, count = self._unresolved.get(pid, (baseline, 0))
        self._unresolved[pid] = baseline, count + 1

    def _request_capture(self, vk, modifiers, *, native, repeat=False):
        if self._stop_event.is_set():
            return False
        send_copy = False
        with self._lock:
            monitor = next((
                item[0] for item in self._registrations
                if not repeat and item[1] == {vk} and item[2] == modifiers and item[3] == native
            ), None)
            if monitor is None and not native:
                if (self._pending is not None and not self._pending["sent"]
                        and not any(vk in keys for keys in self._HOOK_MODIFIER_VKS.values())):
                    self._abandon_pending("selection_may_have_changed")
                return False
            if monitor is None and self._pending is None and not self._registrations:
                return False
            context = self._foreground_capture_context()
            pid = context["source_pid"]
            before = context["clipboard_sequence_before"]
            if pid == os.getpid():
                monitor = None
                if not native or self._pending is None:
                    return False
            if monitor is not None:
                context["binding_generation"] = monitor._binding_generation
            if not pid or not context["source_hwnd"] or not before:
                self._report(monitor, context, reason="source_unavailable")
                return monitor is not None and not native

            if self._pending is not None and time.perf_counter() >= self._pending["deadline"]:
                self._abandon_pending("clipboard_timeout")
            if self._pending is not None:
                if not native:
                    if not self._pending["sent"]:
                        self._abandon_pending("selection_may_have_changed")
                    self._report(monitor, context, reason="capture_busy")
                    return True
                self._abandon_pending("clipboard_superseded")
                # The two physical copy attempts can complete in either order.
                self._mark_unresolved(pid, before)
                self._report(monitor, context, reason="clipboard_superseded")
            elif pid in self._unresolved:
                self._report(monitor, context, reason="clipboard_needs_manual_copy")
            else:
                # Unassigned native Ctrl+C can still race a later custom chord.
                # Observe its metadata only. Never read its text.
                self._pending = {
                    "monitor": monitor,
                    "context": context,
                    "sent": native,
                    "sending": False,
                    "clipboard_notified": False,
                    "deadline": context["trigger_perf"] + CLIPBOARD_COPY_TIMEOUT_SEC,
                }
                send_copy = not native

        if send_copy:
            self._wake_keyboard()
        else:
            self._wake_reader()
        return monitor is not None and not native

    def _wake_keyboard(self):
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_COPY_SELECTION, 0, 0)

    def _send_pending_copy(self):
        # Called outside LowLevelKeyboardProc on the same keyboard thread.
        with self._lock:
            pending = self._pending
            if pending is None or pending["sent"] or self._stop_event.is_set():
                return
            if time.perf_counter() >= pending["deadline"]:
                self._abandon_pending("clipboard_timeout")
                return
            context = pending["context"]
            foreground = self._foreground_capture_context()
            keys = ("source_hwnd", "source_pid", "source_focus", "clipboard_sequence_before")
            if any(context[key] != foreground[key] for key in keys):
                self._abandon_pending("source_window_changed")
                return
            pending["sent"] = True
            pending["sending"] = True
        sent = self._send_ctrl_c_to_active_window()
        with self._lock:
            if self._pending is not pending:
                return
            pending["sending"] = False
            if not sent:
                self._abandon_pending("clipboard_send_failed")
                return
        self._wake_reader()

    def _wait_for_clipboard_change(self, *, clipboard_notification=False):
        # Runs only on the clipboard thread. Never block the keyboard hook or Tk
        # while another process owns/serves the clipboard.
        with self._lock:
            pending = self._pending
            if pending is None:
                if self._unresolved:
                    sequence = self._clipboard_sequence_number()
                    owner = self._clipboard_owner_pid()
                    if owner in self._unresolved:
                        baseline, count = self._unresolved[owner]
                        if (sequence not in (None, 0, baseline)
                                and self._clipboard_sequence_number() == sequence):
                            if count == 1:
                                del self._unresolved[owner]
                            else:
                                self._unresolved[owner] = sequence, count - 1
                return
            if time.perf_counter() >= pending["deadline"]:
                self._abandon_pending("clipboard_timeout")
                return
            context = pending["context"]
            pending["clipboard_notified"] |= clipboard_notification
            if not pending["sent"] or pending["sending"]:
                return
            sequence = self._clipboard_sequence_number()
            before = context["clipboard_sequence_before"]
            if pending["monitor"] is None:
                if (sequence != before and self._clipboard_owner_pid() == context["source_pid"]
                        and self._clipboard_sequence_number() == sequence):
                    self._pending = None
                return
            if sequence == before and not pending["clipboard_notified"]:
                return
            observed = pending.setdefault("observed_sequence", sequence)
            if observed != before and observed != sequence:
                self._abandon_pending("clipboard_superseded")
                return
            pending["observed_sequence"] = sequence

        result = self._read_unicode_snapshot(context, observed_sequence=sequence)
        if result is None:
            return
        sequence, captured, reason = result
        with self._lock:
            if self._pending is not pending:
                return
            if time.perf_counter() >= pending["deadline"]:
                self._abandon_pending("clipboard_timeout", unresolved=False)
                return
            if reason:
                self._abandon_pending(reason, unresolved=reason in {
                    "clipboard_owner_changed", "clipboard_superseded",
                })
                return
            self._pending = None
            self._report(pending["monitor"], context, sequence=sequence, text=captured)

    @staticmethod
    def _clipboard_sequence_number():
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())

    @staticmethod
    def _window_pid(hwnd):
        if not hwnd:
            return None
        user32 = ctypes.windll.user32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None

    def _clipboard_owner_pid(self):
        user32 = ctypes.windll.user32
        user32.GetClipboardOwner.restype = wintypes.HWND
        return self._window_pid(user32.GetClipboardOwner())

    @staticmethod
    def _process_name_from_pid(pid):
        if not pid:
            return ""
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            count = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(count)):
                return PureWindowsPath(buffer.value).name
            return ""
        finally:
            kernel32.CloseHandle(handle)

    def _foreground_capture_context(self):
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        gui = GUIThreadInfo()
        gui.cbSize = ctypes.sizeof(gui)
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUIThreadInfo)]
        user32.GetGUIThreadInfo(0, ctypes.byref(gui))
        return {
            "source_pid": self._window_pid(hwnd),
            "source_hwnd": hwnd,
            "source_focus": gui.hwndFocus,
            "clipboard_sequence_before": self._clipboard_sequence_number(),
            "trigger_perf": time.perf_counter(),
        }

    def _read_unicode_snapshot(self, context, *, observed_sequence):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        before = context["clipboard_sequence_before"]
        if not user32.OpenClipboard(self._reader_hwnd):
            return None
        try:
            sequence = self._clipboard_sequence_number()
            owner = self._clipboard_owner_pid()
            if sequence != observed_sequence:
                return sequence, None, "clipboard_superseded"
            if owner != context["source_pid"]:
                return (sequence, None, "clipboard_owner_changed") if sequence != before else None
            if not user32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
                return (sequence, None, "clipboard_empty") if sequence != before else None
            handle = user32.GetClipboardData(13)
            # A delayed-rendering owner may advance the sequence during GetClipboardData.
            sequence = self._clipboard_sequence_number()
            if not sequence or sequence == before:
                return None
            if not handle or self._clipboard_owner_pid() != owner:
                return sequence, None, "clipboard_read_failed"
            size = kernel32.GlobalSize(handle)
            data = kernel32.GlobalLock(handle)
            if not data:
                return sequence, None, "clipboard_read_failed"
            try:
                raw = ctypes.string_at(data, size - size % 2)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
        end = raw.find(b"\x00\x00")
        while end >= 0 and end % 2:
            end = raw.find(b"\x00\x00", end + 1)
        if end < 0:
            return sequence, None, "clipboard_read_failed"
        try:
            text = raw[:end].decode("utf-16-le")
        except UnicodeDecodeError:
            return sequence, None, "clipboard_read_failed"
        return sequence, text, None if text and not text.isspace() else "clipboard_empty"

    def _wake_reader(self):
        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        hwnd = self._reader_hwnd
        if hwnd:
            user32.PostMessageW(hwnd, 0x8001, 0, 0)  # WM_APP + 1

    def _clipboard_listener_loop(self):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        for name in ("AddClipboardFormatListener", "RemoveClipboardFormatListener", "DestroyWindow", "OpenClipboard"):
            getattr(user32, name).argtypes = [wintypes.HWND]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        user32.SetTimer.argtypes = [wintypes.HWND, wintypes.WPARAM, wintypes.UINT, ctypes.c_void_p]
        user32.SetTimer.restype = wintypes.WPARAM
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, -3, None, None, None)
        registered = bool(hwnd and user32.AddClipboardFormatListener(hwnd))
        try:
            if not registered or not user32.SetTimer(hwnd, 1, CLIPBOARD_POLL_INTERVAL_MS, None):
                return
            self._reader_hwnd = hwnd
            self._reader_ready_event.set()
            msg = wintypes.MSG()
            while not self._stop_event.is_set():
                if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0 or self._stop_event.is_set():
                    break
                if msg.message in (0x031D, 0x0113, 0x8001):  # Clipboard update, retry timer, request
                    self._wait_for_clipboard_change(clipboard_notification=msg.message == 0x031D)
                else:
                    user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._reader_hwnd = None
            self._reader_ready_event.set()
            if registered:
                user32.RemoveClipboardFormatListener(hwnd)
            if hwnd:
                user32.DestroyWindow(hwnd)
