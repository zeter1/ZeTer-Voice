from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from pathlib import PureWindowsPath

from .runtime import CLIPBOARD_COPY_TIMEOUT_SEC, CLIPBOARD_DISPATCH_TIMEOUT_SEC


WM_COPY_SELECTION = 0x8002
_MODIFIER_VKS = {
    "CTRL": (0x11, 0xA2, 0xA3),
    "SHIFT": (0x10, 0xA0, 0xA1),
    "ALT": (0x12, 0xA4, 0xA5),
}


class GUIThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class WindowsHotkeyHookMixin:
    _HOOK_MODIFIER_VKS = _MODIFIER_VKS

    @staticmethod
    def _window_pid(hwnd):
        if not hwnd:
            return None
        user32 = ctypes.windll.user32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None

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

    @classmethod
    def _hook_event_is_injected(cls, flags):
        return bool(int(flags) & 0x10)

    def _hook_modifiers_match(self):
        return self.hotkey_modifiers == self._pressed_modifiers(self._pressed_vks)

    @staticmethod
    def _pressed_modifiers(pressed):
        return {modifier for modifier, vks in _MODIFIER_VKS.items() if pressed.intersection(vks)}

    def _dispatch_keyboard(self, vk, *, is_down, flags, pressed, suppressed, input_tick=None):
        if self._hook_event_is_injected(flags):
            return False
        already_down = vk in pressed
        if is_down:
            pressed.add(vk)
        else:
            pressed.discard(vk)
        if vk in suppressed:
            if not is_down:
                suppressed.discard(vk)
                if input_tick is not None:
                    self._confirm_copy_repeat(vk, input_tick)
            return True
        if not is_down or pressed.intersection((0x5B, 0x5C)):
            return False
        modifiers = self._pressed_modifiers(pressed)
        native = vk == ord("C") and modifiers == {"CTRL"}
        if already_down and not native:
            return False
        if self._request_capture(
            vk, modifiers, native=native, repeat=already_down, can_queue=input_tick is not None,
        ):
            suppressed.add(vk)
            return True
        return False

    @staticmethod
    def _same_source_window(first, second):
        return all(first[key] == second[key] for key in ("source_hwnd", "source_pid", "source_focus"))

    def _request_capture(self, vk, modifiers, *, native, repeat=False, can_queue=False):
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
                self._capture_id += 1
                context.update(
                    capture_id=self._capture_id, capture_protocol=5, capture_stage="trigger",
                    copy_kind="native_ctrl_c" if native else "injected_ctrl_c",
                )
            if not pid or not context["source_hwnd"] or not before:
                self._report(monitor, context, reason="source_unavailable")
                return monitor is not None and not native

            if self._pending is not None and time.perf_counter() >= self._pending["deadline"]:
                # This key itself is a fresh intent. Prefer it to an older queued repeat.
                self._abandon_pending("clipboard_timeout")
            if self._pending is not None and not self._pending["sent"]:
                previous = self._pending["context"]
                same_window = self._same_source_window(previous, context)
                if (can_queue and not native and self._pending["monitor"] is monitor and same_window
                        and previous["capture_stage"] == "waiting_input_release"
                        and previous["clipboard_sequence_before"] == before):
                    return True
                self._abandon_pending("selection_may_have_changed" if same_window else "source_window_changed")
            if self._pending is not None:
                if not native:
                    if can_queue and self._queue_copy_repeat(self._pending, monitor, context, vk):
                        return True
                    previous = self._pending["context"]
                    context.update(blocked_by_capture_id=previous.get("capture_id"),
                                   blocked_by_capture_stage=previous["capture_stage"])
                    self._report(monitor, context, reason="capture_busy")
                    return True
                # The two physical copy attempts can complete in either order.
                self._pending["context"]["clipboard_conflict"] = "overlapping_copy"
                context["clipboard_conflict"] = "overlapping_copy"
                self._abandon_pending("clipboard_superseded")
                self._report(monitor, context, reason="clipboard_superseded")
            else:
                # Unassigned native Ctrl+C can still race a later custom chord.
                # Observe its metadata only. Never read its text.
                self._pending = self._new_copy_request(monitor, context, native, context["trigger_perf"])
                send_copy = not native

        if send_copy:
            if not self._wake_keyboard():
                with self._lock:
                    if self._pending is not None and self._pending["context"] is context:
                        self._abandon_pending("clipboard_dispatch_failed")
        else:
            self._wake_reader()
        return monitor is not None and not native

    @staticmethod
    def _new_copy_request(monitor, context, native, now):
        timeout = CLIPBOARD_COPY_TIMEOUT_SEC if native else CLIPBOARD_DISPATCH_TIMEOUT_SEC
        context["capture_stage"] = "waiting_clipboard" if native else "dispatch"
        return {
            "monitor": monitor,
            "context": context,
            "sent": native,
            "sending": False,
            "clipboard_notified": False,
            "deadline": now + timeout,
        }

    def _queue_copy_repeat(self, pending, monitor, context, vk):
        previous = pending["context"]
        if (pending["monitor"] is not monitor or previous.get("copy_kind") != "injected_ctrl_c"
                or previous["capture_stage"] != "waiting_clipboard" or pending["clipboard_notified"]
                or "capture_retry_of" in previous or not self._same_source_window(previous, context)
                or previous["clipboard_sequence_before"] != context["clipboard_sequence_before"]):
            return False
        context["capture_retry_of"] = previous["capture_id"]
        pending["queued_repeat"] = {"vk": vk, "context": context, "input_ticks": ()}
        return True

    def _confirm_copy_repeat(self, vk, release_tick):
        with self._lock:
            queued = self._pending.get("queued_repeat") if self._pending else None
            if queued is not None and queued["vk"] == vk:
                last_tick = self._last_input_tick()
                if last_tick is not None:
                    queued["input_ticks"] = (last_tick, release_tick)

    def _expire_copy_request(self):
        # Caller holds _lock. Only an explicit repeat can be sent after a no-response timeout.
        pending = self._pending
        queued = pending.get("queued_repeat")
        previous = pending["context"]
        if queued is not None and previous["capture_stage"] == "waiting_clipboard":
            context = queued["context"]
            foreground = self._foreground_capture_context()
            if self._stop_event.is_set() or self._last_input_tick() not in queued["input_ticks"]:
                previous["capture_retry_skipped"] = "input_changed"
            elif not self._same_source_window(context, foreground):
                previous["capture_retry_skipped"] = "source_changed"
            elif pending["clipboard_notified"] or foreground["clipboard_sequence_before"] != context["clipboard_sequence_before"]:
                previous["capture_retry_skipped"] = "clipboard_changed"
            else:
                replacement = self._new_copy_request(pending["monitor"], context, False, time.perf_counter())
                replacement["retry_input_ticks"] = queued["input_ticks"]
                self._pending = replacement
                if not self._wake_keyboard():
                    self._abandon_pending("clipboard_dispatch_failed")
                return
        self._abandon_pending("clipboard_timeout")

    @staticmethod
    def _last_input_tick():
        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        last_input = LastInputInfo(ctypes.sizeof(LastInputInfo))
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last_input)):
            return last_input.dwTime
        return None

    @staticmethod
    def _copy_input_blocker():
        key_state = ctypes.windll.user32.GetAsyncKeyState
        if any(key_state(vk) & 0x8000 for vk in (0x01, 0x02)):
            return "mouse"
        modifiers = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt and Windows
        if any(key_state(vk) & 0x8000 for vk in modifiers):
            return "modifier"
        return None

    def _wake_copy_after_input_release(self, pending):
        context = pending["context"]
        if context["capture_stage"] == "waiting_input_release" and not self._copy_input_blocker():
            context["capture_stage"] = "dispatch"
            if not self._wake_keyboard():
                self._abandon_pending("clipboard_dispatch_failed")

    def _wake_keyboard(self):
        return bool(ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_COPY_SELECTION, 0, 0))

    def _send_pending_copy(self):
        # Called outside LowLevelKeyboardProc on the same keyboard thread.
        with self._lock:
            pending = self._pending
            if pending is None or pending["sent"] or self._stop_event.is_set():
                return
            if time.perf_counter() >= pending["deadline"]:
                self._expire_copy_request()
                return
            context = pending["context"]
            foreground = self._foreground_capture_context()
            if not self._same_source_window(context, foreground):
                self._abandon_pending("source_window_changed")
                return
            if context["clipboard_sequence_before"] != foreground["clipboard_sequence_before"]:
                context["clipboard_conflict"] = "before_dispatch"
                context["clipboard_sequence_latest"] = foreground["clipboard_sequence_before"]
                self._abandon_pending("clipboard_superseded")
                return
            if ("retry_input_ticks" in pending
                    and self._last_input_tick() not in pending["retry_input_ticks"]):
                self._abandon_pending("selection_may_have_changed")
                return
            blocker = self._copy_input_blocker()
            if blocker:
                context.update(capture_stage="waiting_input_release", copy_waited_for_input=blocker)
                return
            pending["clipboard_notified"] = False
            context["clipboard_notified"] = False
            pending["sent"] = True
            pending["sending"] = True
            context["capture_stage"] = "send_input"
        sent = self._send_ctrl_c_to_active_window()
        with self._lock:
            if self._pending is not pending:
                return
            pending["sending"] = False
            context["send_input_accepted"] = sent
            if not sent:
                self._abandon_pending("clipboard_send_failed")
                return
            now = time.perf_counter()
            context["dispatch_ms"] = round((now - context["trigger_perf"]) * 1000, 1)
            context["capture_stage"] = "waiting_clipboard"
            pending["deadline"] = now + CLIPBOARD_COPY_TIMEOUT_SEC
        self._wake_reader()

    def _windows_keyboard_hook_loop(self):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        result_type = ctypes.c_ssize_t

        class KeyboardEvent(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
            ]

        prototype = ctypes.WINFUNCTYPE(
            result_type, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
        )
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, prototype, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = result_type
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        # Modifiers pressed before the hook starts still affect the first chord.
        pressed = {
            vk for vk in (0xA2, 0xA3, 0xA0, 0xA1, 0xA4, 0xA5, 0x5B, 0x5C)
            if user32.GetAsyncKeyState(vk) & 0x8000
        }
        suppressed = set()

        def hook_proc(code, message, address):
            if code == 0 and message in (0x0100, 0x0101, 0x0104, 0x0105):
                event = ctypes.cast(address, ctypes.POINTER(KeyboardEvent)).contents
                if self._dispatch_keyboard(
                    event.vkCode, is_down=message in (0x0100, 0x0104),
                    flags=event.flags, pressed=pressed, suppressed=suppressed, input_tick=event.time,
                ):
                    return 1
            return user32.CallNextHookEx(self._hook_id, code, message, address)

        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._thread_id = kernel32.GetCurrentThreadId()
        hook = prototype(hook_proc)
        self._hook_id = user32.SetWindowsHookExW(13, hook, kernel32.GetModuleHandleW(None), 0)
        self._hook_ready_event.set()
        if not self._hook_id:
            return

        try:
            while not self._stop_event.is_set():
                if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0 or self._stop_event.is_set():
                    break
                if msg.message == WM_COPY_SELECTION:
                    self._send_pending_copy()
                else:
                    user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnhookWindowsHookEx(self._hook_id)
            self._hook_id = None
            self._thread_id = None

    @staticmethod
    def _send_ctrl_c_to_active_window():
        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("vk", wintypes.WORD), ("scan", wintypes.WORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t),
            ]

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t),
            ]

        class InputValue(ctypes.Union):
            _fields_ = [("keyboard", KeyboardInput), ("mouse", MouseInput)]

        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("value", InputValue)]

        user32 = ctypes.windll.user32
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        # Release interfering modifiers in the same input batch as Ctrl+C.
        # The hook ignores the injected events, so no other binding can fire.
        keys = [
            (vk, 2 | int(vk in (0xA3, 0xA5)))
            for name in ("SHIFT", "ALT") for vk in _MODIFIER_VKS[name][1:]
            if user32.GetAsyncKeyState(vk) & 0x8000
        ]
        ctrl_held = any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS["CTRL"][1:])
        if not ctrl_held:
            keys.append((0xA2, 0))
        keys.extend(((ord("C"), 0), (ord("C"), 2)))
        if not ctrl_held:
            keys.append((0xA2, 2))
        inputs = (Input * len(keys))()
        for item, (vk, flags) in zip(inputs, keys):
            item.type = 1  # INPUT_KEYBOARD
            item.value.keyboard = KeyboardInput(vk, 0, flags, 0, 0)
        return user32.SendInput(len(inputs), inputs, ctypes.sizeof(Input)) == len(inputs)
