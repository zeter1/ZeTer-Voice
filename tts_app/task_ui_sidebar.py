"""The voice, audio and capture controls shown alongside the text editor."""
from __future__ import annotations

from .runtime import PITCH_MAX, PITCH_MIN, RATE_MAX, RATE_MIN, TAB_HOTKEY_NONE_LABEL, clamp_int, tk, ttk
from .voice_catalog import voice_spec
from .windows_tools import GlobalCopyHotkeyMonitor
from .workspace_theme import SURFACE


class TaskUiSidebarMixin:
    def _build_sidebar(self) -> None:
        self.sidebar.rowconfigure(0, weight=1)
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar_canvas = tk.Canvas(self.sidebar, background=SURFACE, borderwidth=0, highlightthickness=0, height=1, takefocus=False)
        self.sidebar_canvas.grid(row=0, column=0, sticky="nsew")
        self.sidebar_scrollbar = ttk.Scrollbar(self.sidebar, orient="vertical", command=self.sidebar_canvas.yview)
        self.sidebar_scrollbar.grid(row=0, column=1, sticky="ns")
        self.sidebar_canvas.configure(yscrollcommand=self.sidebar_scrollbar.set, yscrollincrement=18)
        self.sidebar_content = ttk.Frame(self.sidebar_canvas)
        self.sidebar_canvas.create_window(0, 0, window=self.sidebar_content, anchor="nw")
        self._build_voice_controls()
        self._build_capture_controls()
        self.sidebar_content.bind("<Configure>", self._sync_sidebar_scroll)
        self.sidebar_canvas.bind("<Configure>", self._sync_sidebar_scroll)
        self.sidebar_canvas.bind("<MouseWheel>", self._scroll_sidebar)
        self._bind_sidebar_navigation(self.sidebar_content)

    def _bind_sidebar_navigation(self, widget: tk.Widget) -> None:
        widget.bind("<MouseWheel>", self._scroll_sidebar, add="+")
        widget.bind("<FocusIn>", self._reveal_sidebar_focus, add="+")
        for child in widget.winfo_children():
            self._bind_sidebar_navigation(child)

    def _sync_sidebar_scroll(self, _event: tk.Event) -> None:
        width = self.sidebar_content.winfo_reqwidth()
        height = self.sidebar_content.winfo_reqheight()
        self.sidebar_canvas.configure(width=width, scrollregion=(0, 0, width, height))
        if self.sidebar_canvas.winfo_height() < height:
            self.sidebar_scrollbar.grid()
        else:
            self.sidebar_scrollbar.grid_remove()
            self.sidebar_canvas.yview_moveto(0)

    def _scroll_sidebar(self, event: tk.Event) -> str | None:
        if self.sidebar_canvas.yview() != (0.0, 1.0) and event.delta:
            self.sidebar_canvas.yview_scroll(-2 if event.delta > 0 else 2, "units")
            return "break"
        return None

    def _reveal_sidebar_focus(self, event: tk.Event) -> None:
        canvas = self.sidebar_canvas
        viewport_top = canvas.canvasy(0)
        viewport_height = canvas.winfo_height()
        widget_top = event.widget.winfo_rooty() - self.sidebar_content.winfo_rooty()
        widget_bottom = widget_top + event.widget.winfo_height()
        if widget_top < viewport_top:
            canvas.yview_moveto((widget_top - 18) / self.sidebar_content.winfo_reqheight())
        elif widget_bottom > viewport_top + viewport_height:
            canvas.yview_moveto((widget_bottom - viewport_height + 18) / self.sidebar_content.winfo_reqheight())

    def _build_voice_controls(self) -> None:
        voice = ttk.Frame(self.sidebar_content, style="Card.TFrame", padding=(15, 12))
        voice.pack(fill="x")
        voice.columnconfigure(0, weight=1)
        ttk.Label(voice, text="Голос и звук", style="Heading.Card.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 9))
        ttk.Label(voice, text="Голос", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        self.voice_combo = ttk.Combobox(voice, textvariable=self.voice_var, values=self.app.voices, state="readonly", width=44)
        self.voice_combo.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        voice_notice = ttk.Label(voice, style="Note.Card.TLabel", width=1)
        voice_notice.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(3, 0))

        def update_voice_notice(*_args) -> None:
            selected = self.voice_var.get()
            spec = voice_spec(selected)
            engine = spec[0] if spec else None
            voice_notice.configure(text={
                "silero": "Silero работает без интернета",
                "edge": "Edge передаёт текст Microsoft",
                None: "Голос Windows работает локально" if selected else "Выберите голос или установите движок в настройках",
            }[engine])
            self._sync_edge_privacy_note()

        self.voice_var.trace_add("write", update_voice_notice)
        update_voice_notice()
        self.rate_scale = self._add_voice_slider(voice, 4, "Скорость", self.rate_var, self.rate_value_var, RATE_MIN, RATE_MAX)
        self.pitch_scale = self._add_voice_slider(voice, 6, "Высота голоса", self.pitch_var, self.pitch_value_var, PITCH_MIN, PITCH_MAX)
        volume_display = tk.StringVar(value=str(self.volume_var.get()))

        def sync_volume(*_args) -> None:
            try:
                value = self.volume_var.get()
            except tk.TclError:
                return
            volume_display.set(str(clamp_int(value, 0, 100, 100)))

        self.volume_var.trace_add("write", sync_volume)
        self._add_voice_slider(voice, 8, "Громкость", self.volume_var, volume_display, 0, 100)
        ttk.Label(voice, text="Устройство воспроизведения", style="Card.TLabel").grid(row=10, column=0, columnspan=2, sticky="w", pady=(7, 0))
        output = ttk.Frame(voice, style="Paper.TFrame")
        output.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        output.columnconfigure(0, weight=1)
        self.audio_output_combo = ttk.Combobox(
            output, textvariable=self.audio_output_var, values=self.app.audio_output_choices, state="readonly", width=1,
        )
        self.audio_output_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(output, text="↻", width=3, command=self.app.refresh_audio_outputs, style="Compact.TButton").grid(
            row=0, column=1, padx=(6, 0),
        )

    def _add_voice_slider(
        self, parent: ttk.Frame, row: int, title: str, variable: tk.IntVar, display: tk.StringVar,
        minimum: int, maximum: int,
    ) -> ttk.Scale:
        ttk.Label(parent, text=title, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=(6, 0))

        def set_value(raw: float | str) -> None:
            value = round(float(raw))
            display.set(str(value))
            try:
                current = variable.get()
            except tk.TclError:
                current = None
            if current != value:
                variable.set(value)

        def step(delta: int) -> str:
            set_value(max(minimum, min(maximum, round(float(display.get())) + delta)))
            return "break"

        scale = ttk.Scale(
            parent, from_=minimum, to=maximum, variable=display, command=set_value,
            style="Voice.Horizontal.TScale", takefocus=True, length=120,
        )
        scale.bind("<Left>", lambda _event: step(-1))
        scale.bind("<Right>", lambda _event: step(1))
        scale.grid(row=row + 1, column=0, sticky="ew", padx=(0, 7))
        ttk.Spinbox(parent, from_=minimum, to=maximum, textvariable=variable, width=5).grid(row=row + 1, column=1, sticky="e")
        return scale

    def _build_capture_controls(self) -> None:
        capture = ttk.Frame(self.sidebar_content, style="Card.TFrame", padding=(15, 12))
        capture.pack(fill="x", pady=(12, 0))
        capture.columnconfigure(0, weight=1)
        ttk.Label(capture, text="Захват текста", style="Heading.Card.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 9))
        ttk.Label(capture, text="Горячая клавиша", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.copy_hotkey_combo = ttk.Combobox(
            capture, textvariable=self.copy_hotkey_var,
            values=(TAB_HOTKEY_NONE_LABEL, *GlobalCopyHotkeyMonitor.PRESET_HOTKEYS),
            state="normal", width=11,
        )
        self.copy_hotkey_combo.grid(row=1, column=1, sticky="e")
        self.copy_hotkey_combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_copy_hotkey())
        self.copy_hotkey_combo.bind("<FocusOut>", lambda _event: self.apply_copy_hotkey(silent=True))
        self.copy_hotkey_combo.bind("<Return>", lambda _event: self.apply_copy_hotkey())
        hotkey_status = ttk.Label(
            capture, textvariable=self.copy_hotkey_status_var, style="Note.Card.TLabel", width=1, wraplength=280,
        )
        hotkey_status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        capture_hint = ttk.Label(
            capture, text="Выделите текст в другой программе", style="Note.Card.TLabel",
        )
        capture_hint.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Checkbutton(
            capture, text="Сразу читать добавленный текст", variable=self.auto_read_hotkey_var, style="Switch.Card.TCheckbutton",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(
            capture, text="Удалять прочитанные предложения", variable=self.delete_read_text_var, style="Switch.Card.TCheckbutton",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=3)

    def _resize_sidebar(self, event: tk.Event) -> None:
        compact = event.width < max(1060, self.sidebar_content.winfo_reqwidth() + 680)
        if compact != self._sidebar_compact:
            self._sidebar_compact = compact
            self.playback_bar.grid_configure(columnspan=2 if compact else 1)
            self._show_sidebar(not compact)

    def _toggle_sidebar(self) -> None:
        self._show_sidebar(not bool(self.sidebar.winfo_manager()))

    def _show_sidebar(self, show: bool) -> None:
        if show:
            self.sidebar.grid(rowspan=1 if self._sidebar_compact else 2)
            self.sidebar_hotkey_hint.grid_remove()
        else:
            focused = self.frame.focus_get()
            if focused is not None and str(focused).startswith(f"{self.sidebar}."):
                self.sidebar_button.focus_set()
            self.sidebar.grid_remove()
            self.sidebar_hotkey_hint.grid()
        self.sidebar_button.configure(text="Скрыть параметры" if show else "Параметры вкладки")
        self._sync_edge_privacy_note()

    def _sync_edge_privacy_note(self) -> None:
        spec = voice_spec(self.voice_var.get())
        if spec and spec[0] == "edge" and not self.sidebar.winfo_manager():
            self.edge_privacy_note.grid()
        else:
            self.edge_privacy_note.grid_remove()
