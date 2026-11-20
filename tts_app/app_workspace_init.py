from __future__ import annotations

from tkinter import font as tkfont

from .about_ui import build_about_page
from .mp3_settings_ui import build_settings_page
from .workspace_text_history import cleanup_workspace_text_history
from .workspace_theme import apply_workspace_theme

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *
from .ui_stall_diagnostics import UiStallMonitor, ExceptionHooks, ui_operation

class AppWorkspaceInitMixin:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        while True:
            try:
                self.settings = load_settings()
                break
            except (OSError, ValueError) as exc:
                if isinstance(exc, ValueError):
                    detail = "Восстановите settings.json из своей копии или переместите повреждённый файл в резервную папку."
                else:
                    detail = "Освободите файл settings.json или восстановите доступ к нему."
                if not messagebox.askretrycancel(
                    APP_TITLE,
                    f"Не удалось прочитать настройки.\n\n{detail}\n\nЗатем нажмите «Повторить».",
                    parent=root,
                ):
                    root.destroy()
                    return
        self.workspace_text_enabled = self.settings.get("save_workspace_text", True)
        self.logger = ProblemLogger()
        try:
            moved = migrate_text_storage()
            ensure_help_files()
        except OSError as exc:
            self.logger.error(
                task_id="app", stage="text_storage_startup", exc=exc,
                traceback_text=traceback.format_exc(), context={},
            )
            self.logger.close(status="startup_failed")
            messagebox.showerror(
                APP_TITLE, f"Не удалось подготовить папку «Текст».\n\n{exc}",
                parent=root,
            )
            root.destroy()
            return
        if moved:
            self.logger.event("text_storage_migrated", directories=moved)
        self.exception_hooks = ExceptionHooks(root, self.logger)
        self.ui_watchdog = UiStallMonitor(root, self.logger)
        self.ui_watchdog.start()

        # One Speed value for the whole application and all tabs.
        self.global_rate_var = tk.IntVar(
            value=clamp_int(self.settings.get("rate"), RATE_MIN, RATE_MAX, 0)
        )
        self._global_rate_syncing = False
        self.global_rate_var.trace_add("write", self._on_global_rate_changed)

        # One Pitch value for the whole application and all tabs.
        self.global_pitch_var = tk.IntVar(
            value=clamp_int(self.settings.get("pitch"), PITCH_MIN, PITCH_MAX, 0)
        )
        self._global_pitch_syncing = False
        self.global_pitch_var.trace_add("write", self._on_global_pitch_changed)

        self.global_audio_output_var = tk.StringVar(
            value=str(
                self.settings.get("audio_output")
                or DEFAULT_AUDIO_OUTPUT_LABEL
            )
        )
        self.global_audio_output_var.trace_add(
            "write",
            self._on_global_audio_output_changed,
        )
        self.audio_outputs: list[str] = []
        self.audio_output_choices: list[str] = [DEFAULT_AUDIO_OUTPUT_LABEL]

        self.root.title(
            f"{APP_TITLE} — {APP_VERSION}"
        )
        self.root.minsize(900, 680)

        try:
            self.root.geometry(
                str(self.settings.get("window_geometry") or "1200x820")
            )
        except tk.TclError:
            self.root.geometry("1200x820")

        self.events: queue.Queue[
            tuple[str, str, object]
        ] = queue.Queue()

        self.voices: list[str] = []
        self.tabs: dict[str, TaskTab] = {}
        self.task_counter = 0
        self.settings_after_id: str | None = None
        self.workspace_after_id: str | None = None
        self.workspace_save_due_perf: float | None = None
        self.workspace_last_saved_perf = time.perf_counter()
        self.workspace_save_failure_count = 0
        self.context_tab_index: int | None = None
        self.shutdown_in_progress = False
        self.shutdown_deadline = 0.0

        # One low-level monitor per ASSIGNED tab hotkey. A monitor's callback is
        # permanently bound to that tab's workspace_id, so changing the visible
        # active tab cannot redirect captured text to the wrong place.
        self.tab_hotkey_monitors: dict[
            str,
            GlobalCopyHotkeyMonitor,
        ] = {}

        # Состояние берём из реальной записи Windows, а не только из JSON.
        autostart_enabled = False
        if os.name == "nt":
            autostart_enabled = windows_autostart_is_enabled()
            if autostart_enabled:
                # Если файл/EXE перенесли, при ручном запуске обновляем путь
                # существующей записи автозапуска на текущую копию программы.
                try:
                    set_windows_autostart(True)
                except Exception:
                    pass
        self.autostart_windows_var = tk.BooleanVar(
            value=autostart_enabled
        )
        self.settings["autostart_windows"] = autostart_enabled

        with ui_operation(self, "build_app_ui"):
            self._build_ui()
        with ui_operation(self, "restore_workspace"):
            self._restore_workspace()
        self._activate_restored_tab_hotkeys()
        if self._unread_workspace_tabs or self._workspace_manifest_needs_read:
            self.root.after_idle(self._warn_unread_workspace_tabs)

        legacy_removed = cleanup_legacy_temp_workdirs()
        if legacy_removed:
            self.logger.event(
                "legacy_temp_cleanup",
                removed_dirs=legacy_removed,
                retention_days=RECOVERY_RETENTION_DAYS,
            )

        self.root.after(100, self._process_events)
        self.root.after(900, self._offer_recovery_jobs)
        self._load_voices_async()
        self._load_audio_outputs_async()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._text_backup_cleanup_id = self.root.after_idle(self._cleanup_text_backups)
        # Outside a named operation, the event loop is idle; an unrelated
        # late Tk freeze must not be misreported as app startup.
        self.ui_watchdog.stage = "idle_tk_mainloop"

    def _cleanup_text_backups(self) -> None:
        self._text_backup_cleanup_id = self.root.after(3_600_000, self._cleanup_text_backups)
        if self.shutdown_in_progress:
            return
        days = self.settings.get("text_backup_retention_days", MP3_TEXT_BACKUP_RETENTION_DAYS)
        active_backups = frozenset(
            task.current_mp3_text_backup_path for task in self.tabs.values() if task.current_mp3_text_backup_path
        )
        for kind, cleanup in (
            ("workspace", lambda: cleanup_workspace_text_history(WORKSPACE_TEXT_HISTORY_DIR, retention_days=days)),
            ("mp3", lambda: cleanup_old_mp3_text_backups(days, protected_paths=active_backups)),
        ):
            try:
                removed, failed = cleanup()
            except OSError as exc:
                self.logger.error(
                    task_id="app", stage="text_backup_cleanup", exc=exc,
                    traceback_text=traceback.format_exc(), context={"kind": kind},
                )
            else:
                self.logger.event(
                    "text_backup_cleanup", kind=kind, retention_days=days,
                    removed=removed, failed=failed,
                )

    def _build_ui(self) -> None:
        apply_workspace_theme(self.root)
        self.editor_font = tkfont.Font(
            root=self.root, family="Segoe UI", size=self.settings.get("editor_font_size", EDITOR_FONT_DEFAULT),
        )
        outer = ttk.Frame(self.root, padding=(22, 10))
        outer.pack(fill="both", expand=True)

        pages = self.section_notebook = ttk.Notebook(outer, style="Section.TNotebook")
        pages.pack(fill="both", expand=True)
        reading = ttk.Frame(pages)
        nav_font = tkfont.Font(root=self.root, family="Segoe UI", size=11)
        label_width = nav_font.measure("Озвучивание") + 24
        self.section_label_space = tk.PhotoImage(master=self.root, width=label_width, height=1)
        tabs = {"image": self.section_label_space, "compound": "center"}
        pages.add(reading, text="Озвучивание", **tabs)
        pages.add(build_settings_page(pages, self), text="Настройки", **tabs)
        pages.add(build_about_page(pages), text="О программе", **tabs)

        self.notebook = ClosableNotebook(reading, new_tab=self.new_tab)
        self.notebook.pack(fill="both", expand=True, pady=(16, 0))

        self.notebook.bind(
            "<<NotebookCloseRequested>>",
            self._on_notebook_close_requested,
            add="+",
        )
        self.notebook.bind(
            "<Button-3>",
            self._on_tab_right_click,
            add="+",
        )
        self.notebook.bind(
            "<<NotebookTabChanged>>",
            lambda _event: self.schedule_workspace_save(),
            add="+",
        )
        self.notebook.bind(
            "<<NotebookTabsReordered>>",
            lambda _event: self.renumber_default_tabs(),
            add="+",
        )

        self.tab_menu = tk.Menu(self.root, tearoff=False)
        self.tab_menu.add_command(
            label="Переименовать вкладку",
            command=self.rename_context_tab,
        )
        self.tab_menu.add_command(
            label="Закрыть вкладку",
            command=self.close_context_tab,
        )

    def _restore_workspace(self) -> None:
        entries: list[dict] = []
        selected_index = 0
        self._unread_workspace_tabs: list[dict] = []
        self._workspace_manifest_needs_read = False
        self._workspace_recovery_id: str | None = None
        self._absorbed_workspace_recovery_id: str | None = None
        self._workspace_recovery_blocked = False
        self._preserved_workspace_text: dict[str, dict] = {}
        restore_text = self.workspace_text_enabled

        try:
            payload = read_workspace_manifest(WORKSPACE_FILE)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self._workspace_manifest_needs_read = True
            self.logger.error(
                task_id="app",
                stage="restore_workspace_manifest",
                exc=exc,
                traceback_text=traceback.format_exc(),
                context={"workspace_file": str(WORKSPACE_FILE)},
            )
        else:
            entries = payload["tabs"]
            self._absorbed_workspace_recovery_id = payload.get("absorbed_recovery_id")
            selected_index = clamp_int(
                payload.get("selected_index", 0), 0, max(0, len(entries) - 1), 0,
            )

        try:
            recovery = read_workspace_manifest(WORKSPACE_RECOVERY_FILE)
            recovery_id = recovery.get("recovery_id")
            if not isinstance(recovery_id, str) or not recovery_id:
                raise ValueError("Отсутствует идентификатор восстановления.")
            try:
                receipt = WORKSPACE_RECOVERY_FILE.with_suffix(".absorbed").read_text(encoding="utf-8")
            except FileNotFoundError:
                receipt = None
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self._workspace_recovery_blocked = True
            self.logger.error(
                task_id="app", stage="read_workspace_recovery", exc=exc,
                traceback_text=traceback.format_exc(), context={},
            )
        else:
            consumed = recovery_id in (self._absorbed_workspace_recovery_id, receipt)
            if not consumed or not self._workspace_manifest_needs_read:
                self._workspace_recovery_id = recovery_id
            if not consumed:
                recovered = recovery["tabs"]
                if not entries:
                    selected_index = clamp_int(
                        recovery.get("selected_index", 0), 0, max(0, len(recovered) - 1), 0,
                    )
                entries = merge_workspace_tabs(entries, recovered)

        has_per_tab_hotkeys = any(
            "copy_hotkey" in entry
            for entry in entries
        )
        legacy_hotkey = TAB_HOTKEY_NONE_LABEL
        if (
            entries
            and not has_per_tab_hotkeys
            and bool(
                self.settings.get(
                    "global_copy_enabled",
                    False,
                )
            )
        ):
            legacy_hotkey = str(
                self.settings.get("global_copy_hotkey")
                or TAB_HOTKEY_NONE_LABEL
            ).strip() or TAB_HOTKEY_NONE_LABEL

        selected_frame = None
        restored_text_chars = 0
        for entry_index, entry in enumerate(entries):
            workspace_id = str(
                entry.get("workspace_id") or uuid.uuid4().hex
            )
            text_file_name = str(
                entry.get("text_file") or f"{workspace_id}.txt"
            )
            text_path = WORKSPACE_TEXT_DIR / Path(text_file_name).name

            restored_text = ""
            read_error = None
            if restore_text:
                try:
                    restored_text = text_path.read_bytes().decode("utf-8")
                except (OSError, UnicodeError) as exc:
                    if not (isinstance(exc, FileNotFoundError) and not entry.get("text_restore_pending")
                            and not entry.get("text_file")):
                        read_error = exc
            else:
                self._preserved_workspace_text[workspace_id] = {
                    key: entry[key] for key in ("text_file", "preview_bookmark", "text_restore_pending") if key in entry
                }

            if read_error is not None:
                self.logger.error(
                    task_id="app",
                    stage="restore_workspace_text",
                    exc=read_error,
                    traceback_text="".join(traceback.format_exception(read_error)),
                    context={"text_path": str(text_path)},
                )

            restored_settings = {
                "voice": entry.get("voice", self.settings["voice"]),
                # Speed and Pitch are global now. Old per-tab values are
                # intentionally ignored during migration.
                "rate": self.settings["rate"],
                "pitch": self.settings["pitch"],
                "audio_output": self.settings["audio_output"],
                "volume": entry.get("volume", self.settings["volume"]),
                "delete_read_text": bool(
                    entry.get("delete_read_text", False)
                ),
                "auto_read_hotkey_text": bool(
                    entry.get("auto_read_hotkey_text", True)
                ),
                "copy_hotkey": (
                    entry.get(
                        "copy_hotkey",
                        TAB_HOTKEY_NONE_LABEL,
                    )
                    if has_per_tab_hotkeys
                    else (
                        legacy_hotkey
                        if entry_index == selected_index
                        else TAB_HOTKEY_NONE_LABEL
                    )
                ),
            }

            raw_title = str(entry.get("title") or "")
            stored_custom_title = entry.get("custom_title")
            if isinstance(stored_custom_title, bool):
                custom_title = stored_custom_title
            else:
                # Migration from 2.2: "Задача 37", "Вкладка 18", etc. were
                # automatic names and should be renumbered, not preserved.
                custom_title = not is_automatic_tab_title(raw_title)

            if read_error is not None:
                # Keep the original file and its bookmark in the manifest.
                # New input goes to an independent tab with its own path.
                self._unread_workspace_tabs.append({
                    **entry,
                    "workspace_id": workspace_id,
                    "text_file": text_file_name,
                    "copy_hotkey": TAB_HOTKEY_NONE_LABEL,
                    "text_restore_pending": True,
                })
                if entry.get("text_restore_pending"):
                    continue

            task = self.new_tab(
                title=(raw_title or "Вкладка") + " — новый текст" if read_error else raw_title,
                workspace_id=None if read_error else workspace_id,
                restored_text=restored_text,
                restored_settings=restored_settings,
                restored_preview_bookmark=(
                    entry.get("preview_bookmark")
                    if restore_text and read_error is None and isinstance(entry.get("preview_bookmark"), dict)
                    else None
                ),
                custom_title=bool(read_error) or custom_title,
                save_workspace=False,
            )
            restored_text_chars += len(restored_text)
            if entry_index == selected_index:
                selected_frame = task.frame

        if not self.tabs:
            self.new_tab(save_workspace=False)

        # Normalize all non-custom tabs after restoration. This also fixes old
        # titles such as "Задача 37" / "Задача 38".
        self.renumber_default_tabs()

        try:
            if selected_frame is not None:
                self.notebook.select(selected_frame)
        except tk.TclError:
            pass

        self.settings["global_copy_enabled"] = False
        self.logger.event(
            "workspace_restored", tabs=len(self.tabs), text_chars=restored_text_chars,
            unread_tabs=len(self._unread_workspace_tabs), manifest_unreadable=self._workspace_manifest_needs_read,
        )
        self.schedule_workspace_save()

    def _warn_unread_workspace_tabs(self) -> None:
        if self.shutdown_in_progress:
            return
        if self._workspace_manifest_needs_read:
            if self._workspace_recovery_blocked:
                text = ("Не удалось открыть workspace.json и workspace.recovery.json. "
                        "Восстановите доступ к списку вкладок перед закрытием программы.")
            else:
                text = ("Не удалось открыть workspace.json. Новые вкладки будут сохраняться отдельно "
                        "и восстановятся при следующем запуске. Прежний файл сохранён. "
                        "Если он повреждён, восстановите его из резервной копии.")
            messagebox.showwarning(
                APP_TITLE,
                text,
                parent=self.root,
            )
            return
        if not self._unread_workspace_tabs:
            return
        messagebox.showwarning(
            APP_TITLE,
            f"Не удалось открыть текст вкладок: {len(self._unread_workspace_tabs)}. "
            "Прежние файлы защищены от записи. Новый текст сохраняется отдельно. "
            "После восстановления доступа перезапустите программу.",
            parent=self.root,
        )
