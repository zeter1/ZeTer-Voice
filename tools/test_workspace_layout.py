"""Synthetic Tk acceptance checks for the redesigned workspace."""
from __future__ import annotations

from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import os
import unittest
import tkinter as tk
from tkinter import font, ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tts_app.app import TTSApp
from tts_app.app_audio_settings import AppAudioSettingsMixin
from tts_app.app_event_dispatch import AppEventDispatchMixin
from tts_app.runtime_storage import STORAGE_BASE_DIR, load_settings, read_workspace_manifest
from tts_app import app_tabs, app_workspace_save
from tts_app.task_ui_state import VOICES_LOADING_STATUS, VOICES_UNAVAILABLE_STATUS


class WorkspaceLayoutTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(STORAGE_BASE_DIR, ROOT)
        self.root = tk.Tk()
        self.addCleanup(self.close_tk)
        self.errors = []
        self.root.createcommand("bgerror", self.errors.append)
        self.root.withdraw()
        for method in (
            "_load_voices_async", "_load_audio_outputs_async",
            "_activate_restored_tab_hotkeys", "_offer_recovery_jobs",
            "_process_events", "_cleanup_text_backups", "schedule_workspace_save", "schedule_settings_save",
        ):
            self.enterContext(patch.object(TTSApp, method))
        self.enterContext(patch("tts_app.app_workspace_init.windows_autostart_is_enabled", return_value=False))
        self.enterContext(patch("tts_app.app_workspace_init.set_windows_autostart"))
        self.enterContext(patch("tts_app.app_workspace_init.UiStallMonitor.start"))
        self.enterContext(patch("tts_app.app_workspace_init.ExceptionHooks"))
        self.enterContext(patch("tts_app.app_workspace_init.ProblemLogger"))
        self.enterContext(patch("tts_app.app_workspace_init.load_settings", return_value={}))
        self.enterContext(patch("tts_app.app_workspace_init.read_workspace_manifest", side_effect=FileNotFoundError))
        self.app = TTSApp(self.root)
        self.task = self.app.current_tab()
        self.task.text_box.insert("1.0", "Первое предложение. Второе предложение.")
        self.root.deiconify()
        self.resize(1420)

    def close_tk(self):
        for callback in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", callback)
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def resize(self, width):
        self.root.geometry(f"{width}x776+0+0")
        self.root.update_idletasks()
        self.root.update()

    def test_closing_tab_is_committed_before_discarding_widget(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = directory / "workspace.json"
            with patch.multiple(
                app_workspace_save, WORKSPACE_FILE=manifest, WORKSPACE_TEXT_DIR=directory,
                WORKSPACE_TEXT_HISTORY_DIR=directory / "history",
            ), patch.object(app_tabs, "WORKSPACE_TEXT_DIR", directory):
                self.app.new_tab()
                self.app.notebook.select(self.task.frame)
                tab_id = self.task.workspace_id
                original = self.task._full_text()
                index = self.app.notebook.index(self.task.frame)
                self.assertTrue(self.app.save_workspace())
                initial = manifest.read_bytes()
                write = app_workspace_save.atomic_write_json

                def fail_without_closing_tab(path, data):
                    if not any(record["workspace_id"] == tab_id for record in data["tabs"]):
                        raise PermissionError("workspace is locked")
                    return write(path, data)

                with patch.object(app_workspace_save, "atomic_write_json", side_effect=fail_without_closing_tab), \
                        patch.object(app_tabs.messagebox, "showerror") as warning:
                    self.app.close_tab_by_index(index)
                    self.assertIs(self.app.current_tab(), self.task)
                    self.assertEqual(self.task._full_text(), original)
                    self.assertEqual(manifest.read_bytes(), initial)
                    warning.assert_called_once()

                self.app.close_tab_by_index(index)
                self.assertIsNone(self.app._task_by_workspace_id(tab_id))
                tabs = read_workspace_manifest(manifest)
                self.assertNotIn(tab_id, [record["workspace_id"] for record in tabs["tabs"]])
                self.assertEqual(tabs["selected_index"], 0)

    @unittest.skipUnless(os.name == "nt", "requires Windows sharing modes")
    def test_failed_recovery_receipt_does_not_commit_tab_removal(self):
        import win32con
        import win32file

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = directory / "workspace.json"
            recovery = directory / "workspace.recovery.json"
            with patch.multiple(
                app_workspace_save, WORKSPACE_FILE=manifest, WORKSPACE_RECOVERY_FILE=recovery,
                WORKSPACE_TEXT_DIR=directory, WORKSPACE_TEXT_HISTORY_DIR=directory / "history",
            ), patch.object(app_tabs, "WORKSPACE_TEXT_DIR", directory):
                self.app.new_tab()
                self.assertTrue(self.app.save_workspace())
                self.app._workspace_recovery_id = "previous-recovery"
                tab_id = self.task.workspace_id
                index = self.app.notebook.index(self.task.frame)
                receipt = recovery.with_suffix(".absorbed")
                for path in (recovery, receipt):
                    path.write_bytes(b"existing")
                with ExitStack() as locks, patch.object(app_tabs.messagebox, "showerror") as warning:
                    for path in (recovery, receipt):
                        handle = win32file.CreateFile(
                            str(path), win32con.GENERIC_READ, win32con.FILE_SHARE_READ,
                            None, win32con.OPEN_EXISTING, 0, None,
                        )
                        locks.callback(handle.Close)
                    self.app.close_tab_by_index(index)
                    self.assertIs(self.app._task_by_workspace_id(tab_id), self.task)
                    saved = read_workspace_manifest(manifest)
                    self.assertIn(tab_id, [record["workspace_id"] for record in saved["tabs"]])
                    warning.assert_called_once()
                self.app.close_tab_by_index(index)
                self.assertIsNone(self.app._task_by_workspace_id(tab_id))
                saved = read_workspace_manifest(manifest)
                self.assertNotIn(tab_id, [record["workspace_id"] for record in saved["tabs"]])

    def test_editor_menu_opens_file_and_leaves_active_reading_untouched(self):
        menu = self.task.editor_menu
        index, = [i for i in range(menu.index("end") + 1)
                  if menu.type(i) == "command" and "Открыть текстовый файл" in menu.entrycget(i, "label")]
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "Документ.md"
            path.write_bytes("Текст из файла".encode("cp1251"))
            original = self.task._full_text()
            with patch("tts_app.task_file_io.filedialog.askopenfilename", return_value=str(path)) as chooser, \
                    patch.object(self.app, "_archive_tab_before_overwrite", return_value=True) as archive, \
                    patch("tts_app.task_file_io.messagebox.showwarning") as warning:
                self.task.current_preview_run_id = "active"
                menu.invoke(index)
                warning.assert_called_once()
                chooser.assert_not_called()
                self.assertEqual(self.task._full_text(), original)
                self.task.current_preview_run_id = None
                menu.invoke(index)
                archive.assert_called_once_with(self.task, "load_file")
                self.assertEqual(self.task._full_text(), "Текст из файла")
                self.assertEqual(self.task.tab_title(), path.stem)

    def test_selected_task_tab_uses_fill_without_dotted_focus_element(self):
        style = ttk.Style(self.root)
        names = repr(style.layout("ClosableNotebook.Tab"))
        self.assertNotIn("Notebook.focus", names)
        self.assertIn("ClosableNotebook.close", names)

    def test_voice_discovery_updates_selection_without_replacing_an_operation_status(self):
        task = self.task
        task.status_var.set("Воспроизвожу текст…")
        task.apply_voice_list(["Microsoft Irina Desktop - Russian"])
        self.assertEqual(task.voice_var.get(), "Microsoft Irina Desktop - Russian")
        self.assertEqual(task.status_var.get(), "Воспроизвожу текст…")
        task.status_var.set(VOICES_LOADING_STATUS)
        task.apply_voice_list(["Microsoft Irina Desktop - Russian"])
        self.assertEqual(task.status_var.get(), "Готово к работе.")
        task.apply_voice_list([])
        self.assertEqual(task.status_var.get(), VOICES_UNAVAILABLE_STATUS)
        task.apply_voice_list(["Microsoft Irina Desktop - Russian"])
        self.assertEqual(task.status_var.get(), "Готово к работе.")

    def test_stale_discovery_does_not_restore_an_uninstalled_voice_or_show_its_error(self):
        self.app._voice_load_generation = 2
        edges = ["Microsoft Edge — Светлана (интернет)"]
        local = ["Microsoft Irina Desktop - Russian"]
        self.app.events.put(("2", "voices_loaded", local))
        self.app.events.put(("1", "voices_loaded", local + edges))
        self.app.events.put(("1", "voices_error", {"exc": RuntimeError("old"), "traceback": ""}))
        with patch("tts_app.app_audio_settings.messagebox.showerror") as show_error:
            AppEventDispatchMixin._process_events(self.app)
        show_error.assert_not_called()
        self.assertEqual(self.app.voices, local)
        self.assertEqual(list(self.task.voice_combo["values"]), local)

    def test_voice_refresh_never_auto_selects_an_online_replacement(self):
        task = self.task
        edge = "Microsoft Edge — Светлана (интернет)"
        task.voice_var.set("Silero — Ксения (без интернета)")
        self.app.settings["voice"] = edge
        task.apply_voice_list([edge])
        self.assertEqual(task.voice_var.get(), "")
        task.voice_var.set(edge)
        task.apply_voice_list([edge])
        self.assertEqual(task.voice_var.get(), edge)

    def test_equal_section_tabs_and_new_tab_button_moves_with_titles(self):
        sections = self.app.section_notebook
        self.assertEqual(
            [sections.tab(tab, "text") for tab in sections.tabs()],
            ["Озвучивание", "Настройки", "О программе"],
        )
        widths = [0, 0, 0]
        for x in range(sections.winfo_width()):
            try:
                index = sections.index(f"@{x},20")
            except tk.TclError:
                continue
            widths[index] += 1
        self.assertGreater(widths[0], 100)
        self.assertEqual(widths, [widths[0]] * 3)
        notebook = self.app.notebook
        plus = notebook.new_tab_button
        self.assertEqual(plus.cget("text"), "+")
        before = plus.winfo_x()
        self.task.set_tab_title("Название вкладки стало заметно длиннее")
        self.root.update()
        self.assertGreater(plus.winfo_x(), before)
        count = len(notebook.tabs())
        plus.invoke()
        self.root.update()
        self.assertEqual(len(notebook.tabs()), count + 1)
        self.assertIsNot(self.app.current_tab(), self.task)
        self.resize(900)
        self.assertLessEqual(plus.winfo_x() + plus.winfo_width(), notebook.winfo_width())
        before = plus.winfo_x()
        with patch.object(self.app, "_archive_tab_before_overwrite", return_value=True):
            self.app.close_tab_by_index(count)
        self.root.update()
        self.assertEqual(len(notebook.tabs()), count)
        self.assertLess(plus.winfo_x(), before)

    def test_drag_reorders_tabs_without_treating_close_icon_as_a_handle(self):
        named = self.app.new_tab(title="Моя книга", custom_title=True)
        third = self.app.new_tab()
        self.root.update()
        notebook = self.app.notebook

        def tab_point(task, element):
            index = notebook.index(task.frame)
            for x in range(notebook.winfo_width()):
                y = notebook.new_tab_button.winfo_height() // 2
                if element in notebook.identify(x, y) and notebook.index(f"@{x},{y}") == index:
                    return {"x": x, "y": y}
            self.fail(f"No {element} hit point for tab {index}")

        def drag(start, end):
            notebook.event_generate("<ButtonPress-1>", **start)
            notebook.event_generate("<B1-Motion>", state=0x0100, **end)
            notebook.event_generate("<ButtonRelease-1>", **end)
            self.root.update()

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.multiple(
                app_workspace_save, WORKSPACE_FILE=directory / "workspace.json",
                WORKSPACE_TEXT_DIR=directory / "tabs", WORKSPACE_TEXT_HISTORY_DIR=directory / "history",
            ), patch.object(app_tabs, "WORKSPACE_TEXT_DIR", directory / "tabs"):
                start = tab_point(self.task, "label")
                end = tab_point(third, "label")
                original_order = notebook.tabs()
                notebook.event_generate("<ButtonPress-1>", **start)
                notebook.event_generate("<B1-Motion>", state=0x0100, x=start["x"] + 2, y=start["y"])
                self.assertFalse(any(isinstance(child, tk.Toplevel) for child in notebook.winfo_children()))

                first_move = {"x": end["x"], "y": 100}
                notebook.event_generate("<B1-Motion>", state=0x0100, **first_move)
                self.root.update()
                previews = [child for child in notebook.winfo_children() if isinstance(child, tk.Toplevel)]
                self.assertEqual(len(previews), 1)
                preview = previews[0]
                self.assertTrue(preview.winfo_viewable())
                self.assertEqual(preview.winfo_children()[0].cget("text"), self.task.tab_title())
                first_position = (preview.winfo_rootx(), preview.winfo_rooty())

                notebook.event_generate("<B1-Motion>", state=0x0100, **end)
                self.root.update()
                self.assertEqual(preview.winfo_rootx() - first_position[0], end["x"] - first_move["x"])
                self.assertEqual(preview.winfo_rooty() - first_position[1], end["y"] - first_move["y"])
                self.assertEqual(notebook.tabs(), original_order)
                notebook.event_generate("<ButtonRelease-1>", **end)
                self.root.update()
                self.assertFalse(preview.winfo_exists())
                self.assertEqual(notebook.tabs(), tuple(str(task.frame) for task in (named, third, self.task)))
                self.assertIs(self.app.current_tab(), self.task)
                self.assertEqual([task.tab_title() for task in (named, third, self.task)],
                                 ["Моя книга", "Вкладка 1", "Вкладка 2"])
                self.assertTrue(self.app.save_workspace())
                manifest = read_workspace_manifest(directory / "workspace.json")
                self.assertEqual([row["workspace_id"] for row in manifest["tabs"]],
                                 [task.workspace_id for task in (named, third, self.task)])
                self.assertEqual(manifest["selected_index"], 2)

                before = notebook.tabs()
                drag(tab_point(named, "close"), tab_point(third, "label"))
                drag(tab_point(named, "label"), {"x": 10, "y": 100})
                self.assertEqual(notebook.tabs(), before)
                self.assertFalse(any(isinstance(child, tk.Toplevel) for child in notebook.winfo_children()))

                close = tab_point(named, "close")
                notebook.event_generate("<ButtonPress-1>", **close)
                notebook.event_generate("<ButtonRelease-1>", **close)
                self.assertIsNone(self.app._task_by_workspace_id(named.workspace_id))

                self.root.update()
                notebook.event_generate("<ButtonPress-1>", **tab_point(third, "label"))
                notebook.event_generate("<B1-Motion>", state=0x0100, x=120, y=100)
                self.root.update()
                preview, = (child for child in notebook.winfo_children() if isinstance(child, tk.Toplevel))
                notebook.pack_forget()
                self.root.update()
                self.assertFalse(preview.winfo_exists())

    def test_native_sliders_keep_the_shared_integer_value(self):
        second = self.app.new_tab()
        self.task.rate_scale.set(7.4)
        self.assertEqual(self.app.global_rate_var.get(), 7)
        self.assertEqual(second.rate_scale.get(), 7)
        self.app.global_pitch_var.set(-12)
        self.assertEqual(self.task.pitch_scale.get(), -12)
        self.assertEqual(second.pitch_scale.get(), -12)

    def test_plus_does_not_cover_tabs_with_long_titles_in_narrow_window(self):
        for n in range(6):
            self.app.new_tab(title=f"Длинное название вкладки номер {n} с текстом", custom_title=True)
        self.resize(900)
        notebook = self.app.notebook
        plus = notebook.new_tab_button
        with self.assertRaises(tk.TclError):
            notebook.index(f"@{plus.winfo_x()},{plus.winfo_height() // 2}")

    def test_white_layout_collapses_and_reopens_without_losing_editor(self):
        self.assertEqual(self.task.text_box.cget("background").lower(), "#ffffff")
        self.assertTrue(self.task.sidebar.winfo_ismapped())
        self.assertGreater(self.task.sidebar.winfo_rootx(), self.task.text_box.winfo_rootx())
        self.task.copy_hotkey_var.set("F8")
        self.resize(900)
        self.assertFalse(self.task.sidebar.winfo_ismapped())
        self.assertTrue(self.task.sidebar_hotkey.winfo_ismapped())
        self.assertEqual(self.root.getvar(self.task.sidebar_hotkey.cget("textvariable")), "F8")
        self.assertTrue(self.task.text_box.winfo_ismapped())
        self.assertTrue(self.task.start_button.winfo_ismapped())
        self.task.sidebar_button.invoke()
        self.root.update()
        self.assertTrue(self.task.sidebar.winfo_ismapped())
        self.assertTrue(self.task.stop_preview_button.winfo_ismapped())
        self.assertEqual(self.task.text_box.get("1.0", "end-1c"), "Первое предложение. Второе предложение.")

    def test_edge_privacy_notice_stays_visible_with_the_sidebar_collapsed(self):
        self.task.voice_var.set("Microsoft Edge — Светлана (интернет)")
        self.resize(900)
        self.assertTrue(self.task.edge_privacy_note.winfo_ismapped())
        self.assertIn("Microsoft", self.task.edge_privacy_note.cget("text"))
        self.task.voice_var.set("Silero — Ксения (без интернета)")
        self.root.update()
        self.assertFalse(self.task.edge_privacy_note.winfo_ismapped())

    def test_capture_switches_change_the_original_tab_settings(self):
        capture = self.task.sidebar_content.winfo_children()[1]
        switches = [child for child in capture.winfo_children() if isinstance(child, ttk.Checkbutton)]
        self.assertEqual(len(switches), 2)
        for switch, variable in zip(switches, (self.task.auto_read_hotkey_var, self.task.delete_read_text_var)):
            self.assertEqual(switch.cget("style"), "Switch.Card.TCheckbutton")
            initial = variable.get()
            switch.invoke()
            self.assertIs(variable.get(), not initial)
            self.assertEqual(switch.instate(["selected"]), not initial)

    def test_about_tab_opens_the_configured_public_links_on_click(self):
        sections = self.app.section_notebook
        sections.select(2)
        self.root.update()
        page = self.root.nametowidget(sections.select())
        card = page.winfo_children()[0]
        links = [
            child for frame in card.winfo_children() if isinstance(frame, ttk.Frame)
            for child in frame.winfo_children() if isinstance(child, ttk.Button)
            and child.cget("style") in {"Contact.TButton", "Social.TButton"}
        ]
        self.assertEqual(len(links), 12)
        with patch("tts_app.about_ui.webbrowser.open") as open_url:
            for link in links:
                link.invoke()
            self.assertIn(("https://dkoles.com/",), [call.args for call in open_url.call_args_list])

    def test_mp3_settings_apply_and_persist_without_buttons(self):
        sections = self.app.section_notebook
        self.assertEqual(sections.tab(1, "text"), "Настройки")
        sections.select(1)
        self.resize(900)
        page = self.root.nametowidget(sections.select())
        groups, = page.winfo_children()
        general, _texts, _engines = groups.winfo_children()
        mp3, _startup = general.winfo_children()
        children = mp3.winfo_children()
        fields = [widget for widget in children if isinstance(widget, ttk.Combobox)]
        self.assertEqual([field.get() for field in fields], ["96 кбит/с", "Авто", "44 100 Гц"])
        saved = tk.BooleanVar(master=self.root, value=False)
        save_settings = self.app.save_settings

        def save_and_signal():
            save_settings()
            saved.set(True)

        timeout = self.root.after(2000, lambda: saved.set(False))
        with patch.object(self.app, "schedule_settings_save", AppAudioSettingsMixin.schedule_settings_save.__get__(self.app)), \
                patch.object(self.app, "save_settings", save_and_signal):
            for combo, choice in zip(fields, (2, 2, 2)):
                combo.current(choice)
                combo.event_generate("<<ComboboxSelected>>")
            self.root.wait_variable(saved)
        self.root.after_cancel(timeout)
        self.assertTrue(saved.get(), "The normal settings autosave should have run")
        self.assertIsNone(self.root.grab_current())
        self.app.settings = load_settings()
        self.assertEqual({key: self.app.settings[key] for key in ("bitrate", "mp3_channels", "mp3_sample_rate_hz")},
                         {"bitrate": "128k", "mp3_channels": 2, "mp3_sample_rate_hz": 48000})
        sections.select(0)
        self.root.update()
        self.assertEqual(self.task.text_box.get("1.0", "end-1c"), "Первое предложение. Второе предложение.")
        sections.select(1)
        self.root.update()
        self.assertEqual([field.current() for field in fields], [2, 2, 2])
        self.assertEqual(self.app.settings["bitrate"], "128k")

    def test_editor_folder_quiet_and_backup_preferences_survive_settings_reload(self):
        second = self.app.new_tab()
        self.task.text_box.tag_add("preview_current", "1.0", "1.6")
        tabs = self.app.section_notebook
        tabs.select(1)
        self.root.update()
        groups, = self.root.nametowidget(tabs.select()).winfo_children()
        general, texts, _engines = groups.winfo_children()
        mp3 = general.winfo_children()[0]
        folder_buttons = next(widget for widget in mp3.winfo_children() if isinstance(widget, ttk.Frame))
        configured = str(ROOT / "tmp")
        with patch("tts_app.mp3_settings_ui.filedialog.askdirectory", return_value=configured) as dialog:
            folder_buttons.winfo_children()[0].invoke()
        self.assertTrue(dialog.call_args.kwargs["mustexist"])
        system = general.winfo_children()[1]
        notifications = system.winfo_children()[0]
        next(widget for widget in notifications.winfo_children() if isinstance(widget, ttk.Checkbutton)).invoke()
        groups.select(texts)
        self.root.update()
        editor, backups = texts.winfo_children()
        next(widget for widget in editor.winfo_children() if isinstance(widget, ttk.Combobox)).set("22")
        next(widget for widget in editor.winfo_children() if isinstance(widget, ttk.Checkbutton)).invoke()
        for toggle in (widget for widget in backups.winfo_children() if isinstance(widget, ttk.Checkbutton)):
            toggle.invoke()
        days = next(widget for widget in backups.winfo_children() if isinstance(widget, ttk.Spinbox))
        days.set("15")
        with patch("tts_app.mp3_settings_ui.messagebox.askyesno", side_effect=(False, True)) as confirm:
            days.event_generate("<FocusOut>")
            self.assertNotIn("text_backup_retention_days", self.app.settings)
            self.assertEqual(days.get(), "30")
            days.set("15")
            days.event_generate("<FocusOut>")
        self.assertEqual(confirm.call_count, 2)
        self.assertEqual(days.get(), "15")
        days.set("0")
        days.event_generate("<FocusOut>")
        self.assertEqual(days.get(), "15")

        for task in (self.task, second, self.app.new_tab()):
            current_font = font.nametofont(task.text_box.cget("font"), root=self.root)
            self.assertEqual(current_font.cget("size"), 22)
            self.assertEqual(task.text_box.tag_cget("preview_current", "background"), "")
        self.assertTrue(self.task.text_box.tag_ranges("preview_current"))
        self.assertTrue(self.app.workspace_text_enabled, "The current session keeps its startup mode")
        self.app.save_settings()
        updated = load_settings()
        expected = {
            "editor_font_size": 22, "highlight_current_sentence": False, "mp3_output_dir": configured,
            "quiet_mp3_notifications": True, "save_workspace_text": False, "archive_text_versions": False,
            "text_backup_retention_days": 15,
        }
        self.assertEqual({key: updated[key] for key in expected}, expected)

    def test_settings_tab_autostart_calls_the_existing_windows_handler(self):
        sections = self.app.section_notebook
        self.assertEqual(sections.tab(1, "text"), "Настройки")
        sections.select(1)
        self.root.update()
        page = self.root.nametowidget(sections.select())
        groups, = page.winfo_children()
        general, _texts, _engines = groups.winfo_children()
        _mp3, system = general.winfo_children()
        _notifications, startup = system.winfo_children()
        toggle = next(widget for widget in startup.winfo_children() if isinstance(widget, ttk.Checkbutton))
        self.assertEqual(toggle.cget("text"), "Автозапуск с Windows")
        if sys.platform == "win32":
            with patch("tts_app.app_recovery.set_windows_autostart", return_value="fixture.exe") as write_registry:
                toggle.invoke()
                write_registry.assert_called_once_with(True)
                self.assertTrue(self.app.settings["autostart_windows"])
                toggle.invoke()
                write_registry.assert_called_with(False)
                self.assertFalse(self.app.settings["autostart_windows"])
        else:
            self.assertTrue(toggle.instate(["disabled"]))

    def test_engines_page_is_inside_settings_and_keeps_applied_mp3_choices(self):
        sections = self.app.section_notebook
        sections.select(1)
        self.resize(900)
        page = self.root.nametowidget(sections.select())
        groups, = page.winfo_children()
        self.assertEqual(
            [groups.tab(tab, "text") for tab in groups.tabs()],
            ["Общие", "Текст и резервные копии", "Голосовые движки"],
        )
        general, _texts, engines = groups.winfo_children()
        manager = self.app.engine_manager
        self.assertIs(engines, manager.frame)
        mp3 = general.winfo_children()[0]
        bitrate = next(widget for widget in mp3.winfo_children() if isinstance(widget, ttk.Combobox))
        bitrate.current(2)
        bitrate.event_generate("<<ComboboxSelected>>")

        groups.select(engines)
        self.root.update()
        self.assertTrue(manager.frame.winfo_ismapped())
        self.assertTrue(all(button.winfo_ismapped() for button in manager.buttons))
        self.assertTrue(manager.cancel.winfo_ismapped())
        self.assertGreater(manager.cancel.winfo_height(), 1)

        groups.select(general)
        self.root.update()
        self.assertEqual(bitrate.current(), 2)
        self.assertEqual(self.app.settings["bitrate"], "128k")
        self.assertIs(self.app.engine_manager, manager)

    def test_single_play_button_follows_pause_resume_stop_and_finish(self):
        task = self.task
        self.assertIn("Читать", task.preview_button.cget("text"))
        task.apply_preview_state("running")
        self.assertIn("Пауза", task.preview_button.cget("text"))
        task.preview_thread = SimpleNamespace(is_alive=lambda: True)
        task.save_current_preview_bookmark = Mock()
        task._bookmark_is_valid = Mock(return_value=True)
        task.preview_bookmark = {"sentence_start_offset": 0, "previous_sentence_start_offset": 0}
        task.preview_button.invoke()
        self.assertTrue(task.preview_pause_event.is_set())
        self.assertIn("Продолжить", task.preview_button.cget("text"))
        task.preview_button.invoke()
        self.assertTrue(task.preview_stop_event.is_set())
        self.assertTrue(task.preview_button.instate(["disabled"]))
        task.apply_preview_state("running")
        self.assertTrue(task.preview_button.instate(["disabled"]))
        task.preview_thread = None
        task.preview_keep_bookmark_after_stop = False
        task.preview_finished_ui(stopped=True)
        self.assertIn("Читать", task.preview_button.cget("text"))
        self.assertTrue(task.stop_preview_button.instate(["disabled"]))

    def test_saved_pause_keeps_resume_and_a_separate_restart(self):
        text = "Первое предложение. Второе предложение."
        task = self.app.new_tab(restored_text=text, restored_preview_bookmark={
            "char_offset": 0, "text_chars": len(text), "text_sha256": sha256(text.encode()).hexdigest(),
        })
        self.assertIn("Продолжить", task.preview_button.cget("text"))
        self.assertEqual(task.editor_menu.entrycget(0, "state"), "normal")
        self.assertFalse(task.preview_cursor_button.instate(["disabled"]))
        self.assertFalse(task.stop_preview_button.instate(["disabled"]))

    def test_mp3_completion_returns_the_saved_pause_to_reading_the_remaining_text(self):
        text = "Первая часть."
        task = self.app.new_tab(restored_text=text, restored_preview_bookmark={
            "char_offset": 0, "text_chars": len(text), "text_sha256": sha256(text.encode()).hexdigest(),
        })
        self.assertTrue(task.track_mp3_source_snapshot("fixture", text))
        task.text_box.insert("end-1c", "Следующая часть.")
        with patch.object(self.app, "save_workspace"):
            self.assertTrue(task.remove_completed_mp3_source("fixture")["removed"])
        self.assertEqual(task._full_text(), "Следующая часть.")
        self.assertIsNone(task.preview_bookmark)
        self.assertIn("Читать", task.preview_button.cget("text"))

    def test_mp3_progress_and_cancel_visibility_track_job(self):
        task = self.task
        self.assertFalse(task.progress.winfo_ismapped())
        self.assertFalse(task.cancel_button.winfo_ismapped())
        task._set_mp3_controls(active=True)
        self.root.update()
        self.assertTrue(task.progress.winfo_ismapped())
        self.assertTrue(task.cancel_button.winfo_ismapped())
        self.assertFalse(task.start_button.winfo_ismapped())
        task.set_job_idle()
        self.root.update()
        self.assertFalse(task.progress.winfo_ismapped())
        self.assertTrue(task.start_button.winfo_ismapped())


if __name__ == "__main__":
    unittest.main()
