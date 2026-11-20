"""Global audio, editor, workspace, and Windows startup settings."""
from .i18n import DEFAULT_LANGUAGE, LANGUAGES, normalize_language, tr

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .engine_manager_ui import EngineManager
from .mp3_format import BITRATES, CHANNELS, SAMPLE_RATES
from .runtime_storage import DEFAULT_SETTINGS
from .runtime_core import EDITOR_FONT_MIN, EDITOR_FONT_MAX, TEXT_BACKUP_RETENTION_MAX_DAYS


MP3_CHOICES = (
    ("bitrate", "Битрейт", BITRATES),
    ("mp3_channels", "Количество каналов", CHANNELS),
    ("mp3_sample_rate_hz", "Частота дискретизации", SAMPLE_RATES),
)


def build_settings_page(parent: ttk.Notebook, app) -> ttk.Frame:
    page = ttk.Frame(parent)
    settings_tabs = ttk.Notebook(page, style="Section.TNotebook")
    settings_tabs.pack(fill="both", expand=True, padx=8, pady=(10, 0))
    general = ttk.Frame(settings_tabs)
    texts = ttk.Frame(settings_tabs)
    settings_tabs.add(general, text=tr("Общие"))
    settings_tabs.add(texts, text=tr("Текст и резервные копии"))
    app.engine_manager = EngineManager(app, settings_tabs)
    settings_tabs.add(app.engine_manager.frame, text=tr("Голосовые движки"))

    output_dir = _setting_variable(page, app, "mp3_output_dir", tk.StringVar)
    quiet = _setting_variable(page, app, "quiet_mp3_notifications", tk.BooleanVar)
    font_size = _setting_variable(page, app, "editor_font_size", tk.IntVar)
    highlight = _setting_variable(page, app, "highlight_current_sentence", tk.BooleanVar)
    save_text = _setting_variable(page, app, "save_workspace_text", tk.BooleanVar)
    archive_versions = _setting_variable(page, app, "archive_text_versions", tk.BooleanVar)

    mp3_card = _card(general, 0, tr("Параметры MP3"))
    _add_mp3_options(mp3_card, output_dir, app)
    system = ttk.Frame(general)
    system.grid(row=0, column=1, sticky="nw")
    _add_notifications(_card(system, 0, tr("Уведомления")), quiet)
    _add_autostart(_card(system, 0, "Windows", row=1), app)
    _add_language(_card(system, 0, tr("Язык интерфейса"), row=2), app)
    _add_editor(_card(texts, 0, tr("Редактор")), font_size, highlight)
    _add_backups(_card(texts, 1, tr("Текст и резервные копии")), save_text, archive_versions, app)
    return page


def _apply_setting(app, key: str, value) -> None:
    if app.shutdown_in_progress or app.settings.get(key, DEFAULT_SETTINGS[key]) == value:
        return
    app.settings[key] = value
    if key == "editor_font_size":
        app.editor_font.configure(size=value)
    elif key == "highlight_current_sentence":
        for task in app.tabs.values():
            task.apply_editor_highlight()
    app.schedule_settings_save()


def _setting_variable(parent, app, key: str, variable_type) -> tk.Variable:
    variable = variable_type(master=parent, value=app.settings.get(key, DEFAULT_SETTINGS[key]))
    variable.trace_add("write", lambda *_args: _apply_setting(app, key, variable.get()))
    return variable


def _card(parent: ttk.Frame, column: int, title: str, *, row: int = 0) -> ttk.Frame:
    card = ttk.Frame(parent, padding=(17, 14), style="Card.TFrame")
    card.grid(row=row, column=column, sticky="nw", padx=(0, 12) if column == 0 else 0, pady=(10, 0))
    ttk.Label(card, text=title, style="Heading.Card.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
    return card


def _add_language(card: ttk.Frame, app) -> None:
    codes = tuple(LANGUAGES)
    combo = ttk.Combobox(card, state="readonly", values=tuple(LANGUAGES.values()), width=20)
    active_language = normalize_language(app.settings.get("ui_language"))
    combo.current(codes.index(active_language))
    combo.grid(row=1, column=0, sticky="w")
    warning = ttk.Label(
        card, style="Warning.Card.TLabel", wraplength=260, justify="left",
    )
    warning.grid(row=2, column=0, sticky="w", pady=(7, 0))
    restart = ttk.Button(
        card, text=tr("Перезапустить программу"), style="Primary.TButton",
        command=app.restart_for_language,
    )
    restart.grid(row=3, column=0, sticky="w", pady=(7, 0))

    def update_warning(*, failed: bool = False) -> None:
        pending = app.settings.get("ui_language", DEFAULT_LANGUAGE) != active_language
        restart.state(["!disabled"] if pending else ["disabled"])
        text = ""
        if failed:
            text = tr("Не удалось сохранить язык.")
        elif pending:
            text = tr("Перезапустите ZeTer Voice, чтобы применить выбранный язык.")
        warning.configure(text=text)
        if text:
            warning.grid()
        else:
            warning.grid_remove()

    def save_language(_event) -> None:
        previous = normalize_language(app.settings.get("ui_language"))
        index = combo.current()
        if app.shutdown_in_progress or index < 0 or codes[index] == previous:
            return
        app.settings["ui_language"] = codes[index]
        if app.save_settings():
            update_warning()
        else:
            app.settings["ui_language"] = previous
            combo.current(codes.index(previous))
            update_warning(failed=True)

    combo.bind("<<ComboboxSelected>>", save_language)
    update_warning()


def _add_mp3_options(card: ttk.Frame, output_dir: tk.StringVar, app) -> None:
    card.columnconfigure(1, weight=1)
    for row, (key, title, values) in enumerate(MP3_CHOICES, start=1):
        ttk.Label(card, text=tr(title), style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 14), pady=4)
        keys = tuple(values)
        labels = (
            [tr("{0} кбит/с", value[:-1]) for value in keys] if key == "bitrate"
            else [tr(values[value]) for value in keys]
        )
        combo = ttk.Combobox(card, state="readonly", width=13, values=labels)
        combo.current(keys.index(app.settings.get(key, DEFAULT_SETTINGS[key])))
        combo.grid(row=row, column=1, sticky="ew", pady=4)

        def apply_choice(event, key=key, keys=keys) -> None:
            index = event.widget.current()
            if index >= 0:
                _apply_setting(app, key, keys[index])

        combo.bind("<<ComboboxSelected>>", apply_choice)

    def choose_folder() -> None:
        directory = filedialog.askdirectory(
            parent=card.winfo_toplevel(), title=tr("Папка по умолчанию для MP3"),
            initialdir=output_dir.get() or None, mustexist=True,
        )
        if directory:
            output_dir.set(directory)

    ttk.Label(card, text=tr("Папка по умолчанию для MP3"), style="Card.TLabel").grid(
        row=4, column=0, columnspan=2, sticky="w", pady=(8, 4),
    )
    ttk.Entry(card, textvariable=output_dir, state="readonly", width=1).grid(
        row=5, column=0, columnspan=2, sticky="ew",
    )
    folder_actions = ttk.Frame(card, style="Paper.TFrame")
    folder_actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 3))
    ttk.Button(folder_actions, text=tr("Выбрать…"), command=choose_folder, style="Compact.TButton").pack(
        side="left", padx=(0, 6),
    )
    ttk.Button(
        folder_actions, text=tr("Очистить"), command=lambda: output_dir.set(""), style="Compact.TButton",
    ).pack(side="left")
    _note(card, 7, tr("Если поле пустое, окно сохранения откроется в последней доступной папке."))


def _add_notifications(card: ttk.Frame, quiet: tk.BooleanVar) -> None:
    ttk.Checkbutton(
        card, text=tr("Тихий режим для готовых MP3"), variable=quiet, style="Switch.Card.TCheckbutton",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 2))
    _note(card, 2, tr("В тихом режиме сообщение о готовности остаётся в строке состояния."))


def _add_autostart(card: ttk.Frame, app) -> None:
    toggle = ttk.Checkbutton(
        card, text=tr("Автозапуск с Windows"), variable=app.autostart_windows_var,
        command=app.toggle_windows_autostart, style="Switch.Card.TCheckbutton",
    )
    if os.name != "nt":
        toggle.state(["disabled"])
    toggle.grid(row=1, column=0, columnspan=2, sticky="w")


def _add_editor(card: ttk.Frame, font_size: tk.StringVar, highlight: tk.BooleanVar) -> None:
    ttk.Label(card, text=tr("Размер шрифта"), style="Card.TLabel").grid(
        row=1, column=0, sticky="w", padx=(0, 12),
    )
    ttk.Combobox(
        card, textvariable=font_size, state="readonly", width=5,
        values=tuple(range(EDITOR_FONT_MIN, EDITOR_FONT_MAX + 1)),
    ).grid(row=1, column=1, sticky="w")
    ttk.Checkbutton(
        card, text=tr("Подсветка читаемого предложения"), variable=highlight, style="Switch.Card.TCheckbutton",
    ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))


def _add_backups(
    card: ttk.Frame, save_text: tk.BooleanVar, archive_versions: tk.BooleanVar, app,
) -> None:
    ttk.Checkbutton(
        card, text=tr("Сохранять текст между запусками"), variable=save_text, style="Switch.Card.TCheckbutton",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 2))
    _note(card, 2, tr("Вкладки и их порядок сохраняются всегда. Режим действует со следующего запуска. "
                   "Когда он выключен, новый текст остаётся до закрытия."))
    ttk.Checkbutton(
        card, text=tr("Копии версий при автосохранении"), variable=archive_versions, style="Switch.Card.TCheckbutton",
    ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 2))
    _note(card, 4, tr("Защитные копии перед очисткой вкладки и созданием MP3 сохраняются отдельно."))
    ttk.Label(card, text=tr("Срок хранения копий, дней"), style="Card.TLabel").grid(
        row=5, column=0, sticky="w", padx=(0, 12), pady=(10, 0),
    )
    key = "text_backup_retention_days"
    days = ttk.Spinbox(card, from_=1, to=TEXT_BACKUP_RETENTION_MAX_DAYS, width=6)
    days.set(str(app.settings.get(key, DEFAULT_SETTINGS[key])))
    days.grid(row=5, column=1, sticky="w", pady=(10, 0))
    status = ttk.Label(card, style="Note.Card.TLabel")
    status.grid(row=6, column=0, columnspan=2, sticky="w", pady=(3, 0))

    def apply_retention(_event=None) -> None:
        if app.shutdown_in_progress:
            return
        old_days = app.settings.get(key, DEFAULT_SETTINGS[key])
        raw = days.get().strip()
        value = int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 4 else 0
        if value == old_days:
            return
        # The confirmation can itself move focus. Restore the committed value
        # first so a nested FocusOut cannot open a second confirmation.
        days.set(str(old_days))
        if not 1 <= value <= TEXT_BACKUP_RETENTION_MAX_DAYS:
            status.configure(text=tr("Укажите от 1 до {0} дней.", TEXT_BACKUP_RETENTION_MAX_DAYS))
            return
        if value < old_days and not messagebox.askyesno(
            tr("Срок хранения резервных копий"),
            tr("При плановой очистке копии старше {0} дн. будут удалены. Применить этот срок?", value),
            parent=app.root, default=messagebox.NO,
        ):
            return
        _apply_setting(app, key, value)
        days.set(str(app.settings.get(key, DEFAULT_SETTINGS[key])))
        status.configure(text="")

    days.bind("<FocusOut>", apply_retention)
    days.bind("<Return>", apply_retention)


def _note(card: ttk.Frame, row: int, text: str) -> None:
    ttk.Label(card, text=text, style="Note.Card.TLabel", wraplength=300, justify="left").grid(
        row=row, column=0, columnspan=2, sticky="w",
    )
