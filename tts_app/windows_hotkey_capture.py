from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes

from .runtime import (
    CLIPBOARD_POLL_INTERVAL_MS,
    CLIPBOARD_READ_TIMEOUT_SEC,
    tk,
)
from .windows_hotkey_hook import WindowsHotkeyHookMixin


class WindowsCopyCapture(WindowsHotkeyHookMixin):
    """One keyboard hook, clipboard listener and delivery queue for the Tk root."""

    def __init__(self, root):
        self.root = root
        self._lock = threading.Lock()
        self._registrations = ()
        self._pending = None
        self._capture_id = 0
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
                    self._expire_copy_request()
            for _ in range(8):
                if self._stop_event.is_set():
                    break
                try:
                    monitor, info = self._events.get_nowait()
                except queue.Empty:
                    break
                if monitor.enabled and monitor._binding_generation == info["binding_generation"]:
                    info["source_process"] = self._process_name_from_pid(info["source_pid"])
                    info["delivery_ms"] = round((time.perf_counter() - info.pop("queued_perf")) * 1000, 1)
                    monitor.callback(info)
            else:
                delay = 1
        finally:
            if not self._stop_event.is_set():
                self._poll_id = self.root.after(delay, self._poll_events)

    def _report(self, monitor, context, *, reason=None, sequence=None, text=None):
        if monitor is not None:
            now = time.perf_counter()
            self._events.put((monitor, {
                **context,
                "capture_rejected": reason,
                "clipboard_sequence_after": sequence,
                "clipboard_changed": text is not None,
                "captured_text": text,
                "queued_perf": now,
                "copy_latency_ms": round((now - context["trigger_perf"]) * 1000, 1),
            }))

    def _abandon_pending(self, reason):
        pending = self._pending
        self._pending = None
        self._report(pending["monitor"], pending["context"], reason=reason)

    def _wait_for_clipboard_change(self, *, clipboard_notification=False):
        # Runs only on the clipboard thread. Never block the keyboard hook or Tk
        # while another process owns/serves the clipboard.
        with self._lock:
            pending = self._pending
            if pending is None:
                return
            now = time.perf_counter()
            context = pending["context"]
            pending["clipboard_notified"] |= clipboard_notification
            context["clipboard_notified"] = pending["clipboard_notified"]
            if now >= pending["deadline"]:
                self._expire_copy_request()
                return
            if not pending["sent"]:
                self._wake_copy_after_input_release(pending)
                return
            if pending["sending"]:
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
            owner = self._clipboard_owner_pid()
            context["clipboard_owner_pid"] = owner
            context["clipboard_sequence_changed"] = bool(sequence and sequence != before)
            if owner == context["source_pid"] and "clipboard_update_ms" not in context:
                context["clipboard_update_ms"] = round((now - context["trigger_perf"]) * 1000, 1)
                # Give an observed response time to render or unlock its Unicode format.
                pending["deadline"] = max(pending["deadline"], now + CLIPBOARD_READ_TIMEOUT_SEC)
            observed = pending.setdefault("observed_sequence", sequence)
            context["clipboard_sequence_latest"] = sequence
            if observed != before and observed != sequence:
                writer = context.get("clipboard_source_owner_hwnd")
                check = self._source_publication_check(context, writer, sequence)
                context["clipboard_source_check"] = check
                if check != "matched":
                    context["clipboard_conflict"] = "while_waiting_to_read"
                    self._abandon_pending("clipboard_superseded")
                    return
                context[self._source_sequence_advance_field(context)] = True
            pending["observed_sequence"] = sequence
            context["clipboard_sequence_observed"] = sequence
            context["capture_stage"] = "opening_clipboard"
            context["read_attempts"] = context.get("read_attempts", 0) + 1

        def progress(stage, **fields):
            with self._lock:
                if self._pending is pending:
                    context.update(capture_stage=stage, **fields)

        result = self._read_unicode_snapshot(context, observed_sequence=sequence, progress=progress)
        if result is None:
            return
        sequence, captured, reason = result
        with self._lock:
            if self._pending is not pending:
                return
            if time.perf_counter() >= pending["deadline"]:
                self._abandon_pending("clipboard_timeout")
                return
            if reason:
                self._abandon_pending(reason)
                return
            self._pending = None
            context["capture_stage"] = "snapshot_ready"
            context["clipboard_sequence_changed"] = True
            self._report(pending["monitor"], context, sequence=sequence, text=captured)

    @staticmethod
    def _clipboard_sequence_number():
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())

    @staticmethod
    def _clipboard_owner_window():
        user32 = ctypes.windll.user32
        user32.GetClipboardOwner.restype = wintypes.HWND
        return user32.GetClipboardOwner()

    def _clipboard_owner_pid(self):
        return self._window_pid(self._clipboard_owner_window())

    def _source_publication_check(self, context, writer, sequence):
        if not sequence or sequence == context["clipboard_sequence_before"]:
            return "no_fresh_version"
        if not writer:
            return "owner_unconfirmed"
        owner = self._clipboard_owner_window()
        if not owner:
            return "owner_unavailable"
        if owner != writer:
            return "owner_window_changed"
        if self._window_pid(writer) != context["source_pid"]:
            return "owner_process_changed"
        foreground = self._foreground_capture_context()
        return "matched" if self._same_source_window(context, foreground) else "source_focus_changed"

    @staticmethod
    def _source_sequence_advance_field(context):
        if context.get("clipboard_source_lock_confirmed"):
            return "clipboard_sequence_advanced_after_lock"
        if context.get("clipboard_source_owner_hwnd"):
            return "clipboard_sequence_advanced_after_busy"
        return "clipboard_sequence_advanced_at_open"

    def _read_unicode_snapshot(self, context, *, observed_sequence, progress=None):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        before = context["clipboard_sequence_before"]
        owner_before_open = None
        if (context.get("copy_kind") == "injected_ctrl_c" and context.get("read_attempts") == 1
                and observed_sequence):
            # The unlocked sequence can advance while the same window publishes.
            # Check the owner around a fresh sequence sample, then verify it under lock.
            candidate = self._clipboard_owner_window()
            sampled_sequence = self._clipboard_sequence_number()
            sample_check = self._source_publication_check(context, candidate, sampled_sequence)
            if sample_check == "matched":
                owner_before_open = candidate
            if progress is not None:
                progress("opening_clipboard", clipboard_sequence_before_open=sampled_sequence,
                         clipboard_owner_sample=sample_check)
        writer = context.get("clipboard_source_owner_hwnd") or owner_before_open
        if not user32.OpenClipboard(self._reader_hwnd):
            if progress is not None:
                # OpenClipboard may fail even if we cannot identify the opener.
                # Keep the first verified clipboard owner across read attempts.
                confirmed_opener = None
                user32.GetOpenClipboardWindow.restype = wintypes.HWND
                opener = user32.GetOpenClipboardWindow()
                if (opener and opener == self._clipboard_owner_window()
                        and self._window_pid(opener) == context["source_pid"]):
                    confirmed_opener = opener
                if observed_sequence and observed_sequence != before:
                    writer = writer or confirmed_opener
                opener_kind = "unknown"
                if opener:
                    opener_kind = "source_owner" if confirmed_opener else "other_window"
                progress("clipboard_open_busy", clipboard_source_owner_hwnd=writer,
                         clipboard_open_busy_count=context.get("clipboard_open_busy_count", 0) + 1,
                         clipboard_busy_opener=opener_kind,
                         clipboard_source_lock_confirmed=bool(
                             context.get("clipboard_source_lock_confirmed")
                             or (writer and writer == confirmed_opener)
                         ))
            return None
        try:
            sequence = self._clipboard_sequence_number()
            owner = self._clipboard_owner_pid()
            if progress is not None:
                progress("checking_clipboard", clipboard_owner_pid=owner, clipboard_sequence_latest=sequence)
            if sequence != observed_sequence:
                check = self._source_publication_check(context, writer, sequence)
                if check != "matched":
                    if progress is not None:
                        progress("checking_clipboard", clipboard_source_check=check,
                                 clipboard_conflict="at_snapshot_open")
                    return sequence, None, "clipboard_superseded"
                if progress is not None:
                    progress("checking_clipboard", clipboard_source_check=check,
                             **{self._source_sequence_advance_field(context): True})
            if owner != context["source_pid"]:
                return (sequence, None, "clipboard_owner_changed") if sequence != before else None
            if writer and self._clipboard_owner_window() != writer:
                return sequence, None, "clipboard_owner_changed"
            if not user32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
                if progress is not None:
                    progress("checking_clipboard", clipboard_unicode_available=False)
                return (sequence, None, "clipboard_empty") if sequence != before else None
            if progress is not None:
                progress("reading_unicode", clipboard_unicode_available=True)
            handle = user32.GetClipboardData(13)
            # A delayed-rendering owner may advance the sequence during GetClipboardData.
            sequence = self._clipboard_sequence_number()
            if not sequence or sequence == before:
                return None
            if (not handle or self._clipboard_owner_pid() != owner
                    or (writer and self._clipboard_owner_window() != writer)):
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
