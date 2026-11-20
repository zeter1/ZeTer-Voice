from __future__ import annotations

import os
import subprocess
import sys

from .i18n import set_language, tr

from .runtime import app_code_dir, load_settings, messagebox, tk
from .app_workspace import AppWorkspaceMixin
from .app_recovery_hotkeys import AppRecoveryHotkeysMixin
from .app_audio_settings import AppAudioSettingsMixin
from .app_events import AppEventsMixin
from .ui_stall_diagnostics import acquire_single_instance, release_single_instance


class TTSApp(
    AppWorkspaceMixin,
    AppRecoveryHotkeysMixin,
    AppAudioSettingsMixin,
    AppEventsMixin,
):
    """Main application assembled from feature-focused mixins."""

    restart_requested = False


def _relaunch() -> None:
    command = [sys.executable]
    env = None
    if getattr(sys, "frozen", False):
        env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    else:
        command.append(str(app_code_dir() / "ZeTer Voice.py"))
    try:
        subprocess.Popen(
            command, env=env, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        messagebox.showerror(
            "ZeTer Voice",
            tr("Не удалось запустить ZeTer Voice снова. Откройте программу вручную."),
        )


def main() -> None:
    instance_lock, already_running = acquire_single_instance()
    if already_running:
        # No second Tk instance, hotkey monitors or competing workspace writes.
        import ctypes

        try:
            set_language(load_settings()["ui_language"])
        except (OSError, ValueError):
            pass
        ctypes.windll.user32.MessageBoxW(
            None, tr("Программа уже запущена. Переключитесь на открытое окно."),
            "ZeTer Voice", 0x40,
        )
        return
    try:
        root = tk.Tk()
        app = TTSApp(root)
        root.mainloop()
    finally:
        release_single_instance(instance_lock)
    if app.restart_requested:
        _relaunch()
