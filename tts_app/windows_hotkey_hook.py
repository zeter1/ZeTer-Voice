from __future__ import annotations

import ctypes
from ctypes import wintypes


WM_COPY_SELECTION = 0x8002
_MODIFIER_VKS = {
    "CTRL": (0x11, 0xA2, 0xA3),
    "SHIFT": (0x10, 0xA0, 0xA1),
    "ALT": (0x12, 0xA4, 0xA5),
}


class WindowsHotkeyHookMixin:
    _HOOK_MODIFIER_VKS = _MODIFIER_VKS

    @classmethod
    def _hook_event_is_injected(cls, flags):
        return bool(int(flags) & 0x10)

    def _hook_modifiers_match(self):
        return self.hotkey_modifiers == self._pressed_modifiers(self._pressed_vks)

    @staticmethod
    def _pressed_modifiers(pressed):
        return {modifier for modifier, vks in _MODIFIER_VKS.items() if pressed.intersection(vks)}

    def _dispatch_keyboard(self, vk, *, is_down, flags, pressed, suppressed):
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
            return True
        if not is_down or pressed.intersection((0x5B, 0x5C)):
            return False
        modifiers = self._pressed_modifiers(pressed)
        native = vk == ord("C") and modifiers == {"CTRL"}
        if already_down and not native:
            return False
        if self._request_capture(vk, modifiers, native=native, repeat=already_down):
            suppressed.add(vk)
            return True
        return False

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
                    flags=event.flags, pressed=pressed, suppressed=suppressed,
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
