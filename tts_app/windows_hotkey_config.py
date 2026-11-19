from __future__ import annotations

import os
import time

from .runtime import tk
from .windows_hotkey_capture import WindowsCopyCapture

class WindowsHotkeyConfigMixin:
    def __init__(self, root, callback, hotkey="Ctrl+C"):
        self.root = root
        self.callback = callback
        self.enabled = False
        self.is_windows = (os.name == "nt")
        self._service = None
        self._binding_generation = 0
        self._local_binding_sequence = None
        self._local_binding_id = None
        self._last_text = None
        self._last_text_time = 0
        self._pressed_vks = set()
        self.set_hotkey(hotkey)

    @classmethod
    def parse_hotkey(cls, hotkey):
        raw = (hotkey or "").strip()
        if not raw:
            raise ValueError("Введите горячую клавишу, например F8 или Ctrl+Shift+C.")

        prepared = (
            raw.replace("Control", "Ctrl")
               .replace("CONTROL", "Ctrl")
               .replace("control", "Ctrl")
               .replace("контрол", "Ctrl")
               .replace("кнтрл", "Ctrl")
               .replace("альт", "Alt")
               .replace("шифт", "Shift")
               .replace(" ", "")
        )
        parts = [p for p in prepared.split("+") if p]
        if not parts:
            raise ValueError("Не удалось прочитать горячую клавишу.")

        modifiers = set()
        main_keys = []

        aliases = {
            "CTRL": "CTRL",
            "CONTROL": "CTRL",
            "SHIFT": "SHIFT",
            "ALT": "ALT",
            "OPTION": "ALT",
        }

        for part in parts:
            token = part.upper()
            token = cls.CYRILLIC_TO_LATIN.get(token, token)
            if token in aliases:
                modifiers.add(aliases[token])
                continue

            if token not in cls.VK_BY_NAME:
                raise ValueError(
                    "Эта клавиша пока не поддерживается. Используйте буквы, цифры, F1-F24 "
                    "или сочетания вроде Ctrl+Alt+X."
                )
            if token not in main_keys:
                main_keys.append(token)

        if not main_keys:
            raise ValueError("Добавьте основную клавишу: F8, Q+W, C, X и т.д.")

        if len(main_keys) > 3:
            raise ValueError("В горячей клавише можно использовать максимум 3 основные клавиши.")

        normalized_parts = []
        for mod in cls.MODIFIER_ORDER:
            if mod in modifiers:
                normalized_parts.append({"CTRL": "Ctrl", "SHIFT": "Shift", "ALT": "Alt"}[mod])

        def display_key(key):
            if key.startswith("F") and key[1:].isdigit():
                return key
            if key == "SPACE":
                return "Space"
            if key == "TAB":
                return "Tab"
            if key == "ENTER":
                return "Enter"
            return key

        normalized_parts.extend(display_key(k) for k in main_keys)
        normalized = "+".join(normalized_parts)

        return {
            "text": normalized,
            "modifiers": modifiers,
            "main_keys": tuple(main_keys),
            "vks": {cls.VK_BY_NAME[k] for k in main_keys},
        }

    def set_hotkey(self, hotkey):
        parsed = self.parse_hotkey(hotkey)
        was_enabled = self.enabled
        if was_enabled and self.unsafe_hotkey_reason(hotkey):
            raise ValueError(self.unsafe_hotkey_reason(hotkey))
        if was_enabled:
            self.disable()
        self.hotkey_text = parsed["text"]
        self.hotkey_modifiers = parsed["modifiers"]
        self.hotkey_main_keys = parsed["main_keys"]
        self.hotkey_main = self.hotkey_main_keys[0]
        self.hotkey_vks = parsed["vks"]
        self.hotkey_vk = next(iter(self.hotkey_vks))
        self._pressed_vks.clear()

        if was_enabled:
            self.enable()

        return self.hotkey_text

    def get_hotkey(self):
        return self.hotkey_text

    @classmethod
    def unsafe_hotkey_reason(cls, hotkey):
        parsed = cls.parse_hotkey(hotkey)
        keys = parsed["main_keys"]
        if len(keys) != 1 or (
            (len(keys[0]) == 1 or keys[0] in ("SPACE", "ПРОБЕЛ"))
            and not parsed["modifiers"].intersection(("CTRL", "ALT"))
        ):
            return "Это сочетание может изменить выделение. Выберите F8 или Ctrl+Shift+C."
        return None

    def _hotkey_is_plain_ctrl_c(self):
        return self.hotkey_main_keys == ("C",) and self.hotkey_modifiers == {"CTRL"}

    def enable(self):
        if self.enabled:
            return
        reason = self.unsafe_hotkey_reason(self.hotkey_text)
        if reason:
            raise ValueError(reason)
        self._binding_generation += 1
        if self.is_windows:
            service = getattr(self.root, "_global_copy_service", None)
            if service is None:
                service = WindowsCopyCapture(self.root)
                try:
                    service.start()
                except Exception:
                    service.stop()
                    raise
                self.root._global_copy_service = service
            service.register(self)
            self._service = service
        self.enabled = True
        if not self.is_windows:
            self._enable_local_binding()

    def disable(self):
        self.enabled = False
        if self._service is not None:
            self._service.unregister(self)
            self._service = None
        if self._local_binding_sequence is not None:
            self._disable_local_binding()

    def stop(self):
        self.disable()

    @staticmethod
    def stop_shared(root):
        service = getattr(root, "_global_copy_service", None)
        if service is not None:
            service.stop()
            root._global_copy_service = None

    def _enable_local_binding(self):
        modifiers = [
            name.title().replace("Ctrl", "Control")
            for name in self.MODIFIER_ORDER if name in self.hotkey_modifiers
        ]
        key = self.hotkey_main
        sequence = "<" + "-".join([*modifiers, key.lower() if len(key) == 1 else key]) + ">"
        self._local_binding_id = self.root.bind_all(sequence, self._local_hotkey, add="+")
        self._local_binding_sequence = sequence

    def _disable_local_binding(self):
        try:
            self.root.unbind_all(self._local_binding_sequence)
            self.root.deletecommand(self._local_binding_id)
        except tk.TclError:
            pass
        self._local_binding_sequence = self._local_binding_id = None

    def _local_hotkey(self, event=None):
        # Preserve ordinary in-app copy and paste in the non-Windows fallback.
        return None

    def remember_text_and_check_duplicate(self, text):
        """Защита от случайного двойного срабатывания на один и тот же текст."""
        now = time.monotonic()
        if text == self._last_text and now - self._last_text_time < 1.2:
            return True
        self._last_text = text
        self._last_text_time = now
        return False
