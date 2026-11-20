from __future__ import annotations

from .i18n import tr

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .ui_stall_diagnostics import ui_operation
from .task_ui_sidebar import TaskUiSidebarMixin
from .workspace_theme import FOCUS, INK, WHITE

class TaskUiBuildMixin(TaskUiSidebarMixin):
    def __init__(
        self,
        app: "TTSApp",
        number: int,
        *,
        workspace_id: str | None = None,
        restored_title: str | None = None,
        restored_text: str = "",
        restored_settings: dict | None = None,
        restored_preview_bookmark: dict | None = None,
        custom_title: bool = False,
    ) -> None:
        self.app = app
        self.number = number
        self.workspace_id = workspace_id or uuid.uuid4().hex
        self.task_id = f"{number:02d}_{uuid.uuid4().hex[:6]}"

        tab_settings = dict(app.settings)
        if isinstance(restored_settings, dict):
            tab_settings.update(restored_settings)

        self.frame = ttk.Frame(app.notebook, padding=8)
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.preview_thread: threading.Thread | None = None
        self.preview_stop_event = threading.Event()
        self.preview_pause_event = threading.Event()
        self.preview_is_paused = False
        self.current_preview_segment_text = ""
        self.current_preview_run_id: str | None = None
        self.current_preview_base_index = "1.0"
        self.current_preview_base_text_offset = 0
        self.current_preview_source_mode = ""
        self.current_preview_segment_start = 0
        self.current_preview_segment_end = 0
        self.current_preview_range_end = 0
        self.current_preview_snapshot_end = 0
        self.current_preview_follow_end = False
        self.current_preview_segment_index = 0
        self.current_preview_segments_total = 0
        self.current_preview_word_length = 0
        self.preview_keep_bookmark_after_stop = False
        self.preview_resume_previous_pending = False

        mark_suffix = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            self.workspace_id[:16],
        )
        self.preview_base_mark = f"_preview_base_{mark_suffix}"
        self.preview_snapshot_end_mark = f"_preview_snapshot_end_{mark_suffix}"
        self.preview_pending_delete_start_mark = f"_preview_delete_start_{mark_suffix}"
        self.preview_pending_delete_end_mark = f"_preview_delete_end_{mark_suffix}"
        self.preview_deleted_chars = 0
        self.preview_pending_delete_active = False

        # A running MP3 task owns one immutable source snapshot. Marks keep its
        # exact widget range stable while hotkey text is appended after it.
        self.mp3_source_start_mark = f"_mp3_source_start_{mark_suffix}"
        self.mp3_source_end_mark = f"_mp3_source_end_{mark_suffix}"
        self.mp3_source_run_id: str | None = None
        self.mp3_source_chars = 0
        self.mp3_source_sha256 = ""
        self.mp3_source_separator_pending = False

        # Compact read/delete diagnostics and per-tab queue counters.
        self._delete_log_segments = 0
        self._delete_log_chars = 0
        self._delete_log_last_flush_perf = time.perf_counter()
        self._text_state_last_log_perf = 0.0
        self.session_captured_chars = 0
        self.session_read_segments = 0
        self.session_read_chars = 0
        self.session_deleted_segments = 0
        self.session_deleted_chars = 0
        self.queue_status_after_id: str | None = None

        # Preview finish cause is logged separately from SAPI's stopped=True/False.
        self.current_preview_finish_reason: str | None = None
        self.preview_resume_from_run_id: str | None = None

        self.preview_bookmark: dict | None = (
            dict(restored_preview_bookmark)
            if isinstance(restored_preview_bookmark, dict)
            else None
        )
        self.current_output_path: Path | None = None
        self.current_mp3_text_backup_path: Path | None = None
        self.current_run_id: str | None = None
        self.current_job_stage = "idle"

        self.voice_var = tk.StringVar(value=str(tab_settings.get("voice") or DEFAULT_VOICE_HINT))

        # Speed and Pitch are intentionally ONE shared Tk variable each for the
        # whole application. Spinboxes edit these variables directly, and the
        # application trace synchronizes each slider's display value.
        self.rate_var = app.global_rate_var
        self.pitch_var = app.global_pitch_var
        self.audio_output_var = app.global_audio_output_var

        self.volume_var = tk.IntVar(value=clamp_int(tab_settings.get("volume"), 0, 100, 100))
        self.delete_read_text_var = tk.BooleanVar(
            value=bool(tab_settings.get("delete_read_text", False))
        )
        self.auto_read_hotkey_var = tk.BooleanVar(
            value=bool(tab_settings.get("auto_read_hotkey_text", True))
        )

        # Hotkey belongs to THIS tab. It is intentionally not inherited from
        # application settings: every newly created tab starts with "Нет".
        restored_copy_hotkey = str(
            tab_settings.get("copy_hotkey")
            or TAB_HOTKEY_NONE_LABEL
        ).strip() or TAB_HOTKEY_NONE_LABEL
        self.copy_hotkey_var = tk.StringVar(
            value=restored_copy_hotkey
        )
        self.copy_hotkey_status_var = tk.StringVar(
            value=tr("Горячая клавиша не назначена.")
        )
        self.applied_copy_hotkey = TAB_HOTKEY_NONE_LABEL

        self.status_var = tk.StringVar(value=tr("Готово к работе."))
        self.queue_status_var = tk.StringVar(value=tr("Очередь: считаю…"))
        self.rate_value_var = tk.StringVar(value=str(self.rate_var.get()))
        self.pitch_value_var = tk.StringVar(value=str(self.pitch_var.get()))

        self.workspace_dirty = False
        self._text_metrics_cache = None
        self._display_long_line = False
        self._word_wrap_auto_disabled = False
        self._manual_wrap_large_line = False
        self._word_wrap_var = tk.BooleanVar(value=True)
        self.custom_title = bool(custom_title)
        self.restored_title = restored_title or tr("Вкладка {0}", number)

        self._build_ui()

        if restored_text:
            self.configure_text_view_for_content(restored_text, origin="restore")
            # Restored data is not a user edit; don't fill Tk's undo history.
            with ui_operation(app, "restore_tab_text", text_chars=len(restored_text)):
                self.text_box.configure(undo=False)
                try:
                    self.text_box.insert("1.0", restored_text)
                    self.text_box.edit_reset()
                finally:
                    self.text_box.configure(undo=True)

        self._restore_preview_bookmark_ui()
        self.text_box.edit_modified(False)
        self.text_box.bind("<<Modified>>", self._on_text_modified, add="+")
        self.schedule_queue_status_refresh()

        # Speed and Pitch have one application-level trace each. Do not
        # attach one trace per tab to shared variables, otherwise a single slider
        # movement would cause duplicate callbacks from every open tab.
        for variable in (
            self.voice_var,
            self.volume_var,
            self.delete_read_text_var,
            self.auto_read_hotkey_var,
        ):
            variable.trace_add("write", self._settings_changed)

    def _build_ui(self) -> None:
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        body = ttk.Frame(self.frame)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_editor(body)

        self.sidebar = ttk.Frame(body)
        self.sidebar.grid(row=0, column=1, sticky="nsew", padx=(14, 0))
        self._sidebar_compact = None

        self._build_playback_bar(body)
        status = ttk.Frame(self.frame)
        status.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var, style="Note.TLabel", width=1).grid(row=0, column=0, sticky="ew")
        self.edge_privacy_note = ttk.Label(status, text=tr("Edge передаёт текст Microsoft"), style="Note.TLabel")
        self.edge_privacy_note.grid(row=0, column=1, padx=(8, 0))
        self.sidebar_hotkey_hint = ttk.Frame(status)
        self.sidebar_hotkey_hint.grid(row=0, column=2, padx=(8, 10))
        ttk.Label(self.sidebar_hotkey_hint, text=tr("Захват:"), style="Note.TLabel").pack(side="left")
        self.sidebar_button = ttk.Button(status, command=self._toggle_sidebar)
        self.sidebar_button.grid(row=0, column=3)
        self._build_sidebar()
        self.sidebar_hotkey = ttk.Label(
            self.sidebar_hotkey_hint, textvariable=self.copy_hotkey_combo.display,
            style="Note.TLabel", width=10,
        )
        self.sidebar_hotkey.pack(side="left", padx=(4, 0))
        self._show_sidebar(True)
        body.bind("<Configure>", self._resize_sidebar, add="+")

    def _build_editor(self, body: ttk.Frame) -> None:
        editor = ttk.Frame(body, style="Card.TFrame")
        editor.grid(row=0, column=0, sticky="nsew")
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(2, weight=1)

        header = ttk.Frame(editor, style="Paper.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=11)
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=tr("Текст для озвучивания"), style="Heading.Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text=tr("⎘  Вставить текст"), command=self.paste_text_from_clipboard).grid(row=0, column=1, padx=(8, 6))
        menu_button = ttk.Menubutton(header, text="⋯", width=3, style="Compact.TMenubutton")
        menu_button.grid(row=0, column=2)
        self.editor_menu = tk.Menu(menu_button, tearoff=False)
        self.editor_menu.add_command(label=tr("Начать чтение заново"), command=self.start_preview)
        self.editor_menu.add_separator()
        self.editor_menu.add_command(label=tr("Открыть текстовый файл…"), command=self.load_file)
        self.editor_menu.add_command(label=tr("Очистить текст"), command=self.clear_text)
        menu_button.configure(menu=self.editor_menu)
        ttk.Separator(editor).grid(row=1, column=0, sticky="ew")

        text_frame = ttk.Frame(editor, style="Paper.TFrame")
        text_frame.grid(row=2, column=0, sticky="nsew")
        text_row = ttk.Frame(text_frame, style="Paper.TFrame")
        text_row.pack(side="top", fill="both", expand=True)
        self.text_box = tk.Text(
            text_row, wrap="word", undo=True, font=self.app.editor_font,
            background=WHITE, foreground=INK, insertbackground=INK,
            selectbackground="#d7e5ff", selectforeground=INK,
            padx=18, pady=14, spacing1=2, spacing3=3, width=1, height=1,
            borderwidth=0, highlightthickness=1, highlightbackground=WHITE, highlightcolor=FOCUS,
        )
        scrollbar = ttk.Scrollbar(text_row, orient="vertical", command=self.text_box.yview)
        def sync_vertical_scroll(first: str, last: str) -> None:
            scrollbar.set(first, last)
            fits = float(first) <= 0 and float(last) >= 1
            if fits and scrollbar.winfo_manager():
                scrollbar.pack_forget()
            elif not fits and not scrollbar.winfo_manager():
                scrollbar.pack(side="right", fill="y")

        self.text_box.configure(yscrollcommand=sync_vertical_scroll)
        self.apply_editor_highlight()
        self.text_box.bind(
            "<Control-KeyPress>",
            lambda event: handle_text_ctrl_shortcut(self.text_box, event),
            add="+",
        )
        self.text_box.bind("<<Paste>>", self.paste_text_from_editor, add="+")
        self.text_box.pack(side="left", fill="both", expand=True)
        self.horizontal_scrollbar = ttk.Scrollbar(text_frame, orient="horizontal", command=self.text_box.xview)
        self.text_box.configure(xscrollcommand=self.horizontal_scrollbar.set)

        ttk.Separator(editor).grid(row=3, column=0, sticky="ew")
        footer = ttk.Frame(editor, style="Paper.TFrame")
        footer.grid(row=4, column=0, sticky="ew", padx=16, pady=7)
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.queue_status_var, style="Note.Card.TLabel", width=1).grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(
            footer, text=tr("Перенос строк"), style="Card.TCheckbutton",
            variable=self._word_wrap_var, command=self._on_word_wrap_changed,
        ).grid(row=0, column=1, padx=(8, 0))

    def apply_editor_highlight(self) -> None:
        enabled = self.app.settings.get("highlight_current_sentence", True)
        self.text_box.tag_configure(
            "preview_current", background="#fff2a8" if enabled else "",
            relief="solid" if enabled else "flat", borderwidth=1 if enabled else 0,
        )

    def _build_playback_bar(self, body: ttk.Frame) -> None:
        bar = self.playback_bar = ttk.Frame(body, style="Card.TFrame", padding=(15, 12))
        bar.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        bar.columnconfigure(3, weight=1)
        self.preview_button = ttk.Button(bar, style="Primary.TButton", width=13)
        self.preview_button.grid(row=0, column=0, padx=(0, 8))
        self.preview_cursor_button = ttk.Button(bar, text=tr("Читать с курсора"), command=self.start_preview_from_cursor)
        self.preview_cursor_button.grid(row=0, column=1, padx=(0, 8))
        self.stop_preview_button = ttk.Button(bar, text=tr("■  Остановить"), command=self.stop_preview)
        self.stop_preview_button.grid(row=0, column=2)

        self.start_button = ttk.Button(bar, text=tr("↓  Создать MP3"), command=self.start_job)
        self.start_button.grid(row=0, column=5, padx=(7, 0))
        self.progress = ttk.Progressbar(bar, maximum=100, mode="determinate", length=130)
        self.progress.grid(row=0, column=4, padx=(8, 0))
        self.cancel_button = ttk.Button(bar, text=tr("Отменить MP3"), command=self.cancel_job)
        self.cancel_button.grid(row=0, column=5, padx=(8, 0))
        self._set_mp3_controls(active=False)
        self._set_playback_controls("idle")

    def _set_playback_controls(self, mode: str) -> None:
        text, action = {
            "idle": (tr("▶ Читать"), self.start_preview),
            "running": (tr("Пауза"), self.pause_preview),
            "paused": (tr("Продолжить"), self.resume_preview),
            "saved": (tr("Продолжить"), self.resume_preview),
            "busy": (tr("Подождите…"), self.start_preview),
        }[mode]
        self.preview_button.configure(text=text, command=action, state="disabled" if mode == "busy" else "normal")
        self.preview_cursor_button.configure(state="normal" if mode in ("idle", "saved") else "disabled")
        self.stop_preview_button.configure(state="normal" if mode in ("running", "paused", "saved") else "disabled")
        self.editor_menu.entryconfigure(0, state="normal" if mode in ("idle", "saved") else "disabled")

    def _set_mp3_controls(self, *, active: bool) -> None:
        active_controls = (self.progress, self.cancel_button)
        if active:
            self.start_button.grid_remove()
            for widget in active_controls:
                widget.grid()
        else:
            for widget in active_controls:
                widget.grid_remove()
            self.start_button.grid()
