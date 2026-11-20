"""Exercise closing with synthetic tabs and isolated workspace files."""
from __future__ import annotations

import sys
import os
import tempfile
import time
import unittest
import gzip
import tkinter as tk
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tts_app import app as app_entry, app_audio_settings, app_shutdown, app_tabs, app_workspace_init, app_workspace_save, runtime_storage
from tts_app.runtime_storage import DEFAULT_SETTINGS, atomic_write_json, read_workspace_manifest


class App(app_shutdown.AppShutdownMixin, app_workspace_save.AppWorkspaceSaveMixin):
    def __init__(self):
        self.root = Mock()
        self.root.after.return_value = "after-1"
        self.notebook = Mock()
        self.notebook.tabs.return_value = ["frame"]
        self.notebook.index.return_value = 0
        self.tab = Mock(
            frame="frame", workspace_id="tab", workspace_dirty=True,
            worker=None, preview_thread=None,
        )
        self.tab.conversion_running.return_value = False
        self.tab.workspace_record.return_value = {"id": "tab"}
        self.tab.text_box.get.return_value = "новая строка\nnew line"
        self.tabs = {"tab": self.tab}
        self.workspace_after_id = None
        self.workspace_save_due_perf = None
        self.workspace_save_failure_count = 0
        self.shutdown_in_progress = False
        self.engine_manager = SimpleNamespace(thread=None, busy=False, close=Mock(), stop=Mock())
        self.tab_hotkey_monitors = {"tab": object()}
        self._stop_tab_hotkey_monitor = Mock()
        self.save_settings = Mock()
        self.logger = Mock()
        self.ui_watchdog = Mock()
        self.ui_watchdog.operation.side_effect = lambda *args, **kwargs: nullcontext()
        self.exception_hooks = Mock()


class CloseTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="tts сохранение "))
        base = Path(directory)
        self.tab_dir = base / "Вкладки"
        self.tab_dir.mkdir()
        self.tab_path = self.tab_dir / "tab.txt"
        self.tab_path.write_text("previous text", encoding="utf-8")
        self.manifest = base / "workspace.json"
        self.recovery_manifest = base / "workspace.recovery.json"
        self.history = base / "Текст" / "История вкладок"
        self.enterContext(patch.multiple(
            app_workspace_save, WORKSPACE_TEXT_DIR=self.tab_dir, WORKSPACE_FILE=self.manifest,
            WORKSPACE_TEXT_HISTORY_DIR=self.history, WORKSPACE_RECOVERY_FILE=self.recovery_manifest, create=True,
        ))
        self.enterContext(patch.object(
            app_workspace_init, "WORKSPACE_RECOVERY_FILE", self.recovery_manifest, create=True,
        ))
        self.dialog = self.enterContext(patch.object(app_shutdown.messagebox, "showerror"))
        self.app = App()

    def test_restart_waits_for_tasks_and_requires_both_saves(self):
        self.app.tab.conversion_running.return_value = True
        with patch.object(app_shutdown.messagebox, "showwarning") as warning:
            self.app.restart_for_language()
            warning.assert_called_once()
        self.app.root.destroy.assert_not_called()
        self.app.save_settings.assert_not_called()
        self.app.tab.conversion_running.return_value = False

        self.app.save_settings.return_value = False
        self.app.restart_for_language()
        self.app.root.destroy.assert_not_called()
        self.assertFalse(self.app.shutdown_in_progress)
        self.assertFalse(getattr(self.app, "restart_requested", False))

        self.app.save_settings.return_value = True
        self.app.restart_for_language()
        self.app.root.destroy.assert_called_once()
        self.assertTrue(self.app.restart_requested)
        self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "новая строка\nnew line")

    def test_restart_with_text_saving_off_requires_confirmation(self):
        self.app.workspace_text_enabled = False
        self.app.tab.text_box.compare.return_value = True
        with patch.object(app_shutdown.messagebox, "askyesno", return_value=False) as confirmation:
            self.app.restart_for_language()
            confirmation.assert_called_once()
        self.app.root.destroy.assert_not_called()

    def test_main_releases_the_mutex_before_launching_the_new_instance(self):
        calls = []
        lock = object()
        with patch.object(app_entry, "acquire_single_instance", return_value=(lock, False)), \
                patch.object(app_entry.tk, "Tk"), \
                patch.object(app_entry, "TTSApp", return_value=SimpleNamespace(restart_requested=True)), \
                patch.object(app_entry, "release_single_instance", side_effect=lambda value: calls.append(value)), \
                patch.object(app_entry, "_relaunch", side_effect=lambda: calls.append("new instance")):
            app_entry.main()
        self.assertEqual(calls, [lock, "new instance"])

    def test_relaunch_uses_the_same_source_or_frozen_executable(self):
        project = self.tab_dir.parent
        with patch.object(app_entry, "app_code_dir", return_value=project), \
                patch.object(app_entry.subprocess, "Popen") as launch:
            app_entry._relaunch()
            self.assertEqual(launch.call_args.args[0], [sys.executable, str(project / "ZeTer Voice.py")])
            self.assertIsNone(launch.call_args.kwargs["env"])
            with patch.object(sys, "frozen", True, create=True):
                app_entry._relaunch()
            self.assertEqual(launch.call_args.args[0], [sys.executable])
            self.assertEqual(launch.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_tabs_only_session_preserves_saved_text_and_bookmark_for_the_next_enabled_start(self):
        legacy_path = self.tab_dir / "старый текст.txt"
        legacy_path.write_bytes("Первое сохранение".encode("utf-8"))
        second_path = self.tab_dir / "second.txt"
        second_path.write_bytes(b"Second saved text")
        bookmark = {"char_offset": 1, "snippet": "Сохранённая позиция"}
        atomic_write_json(self.manifest, {"tabs": [
            {"workspace_id": "tab", "title": "Первая", "text_file": legacy_path.name, "preview_bookmark": bookmark},
            {"workspace_id": "second", "title": "Вторая", "text_file": second_path.name},
        ], "selected_index": 1})

        class Frame(str):
            def destroy(self):
                pass

        def open_workspace(text_enabled):
            app = App()
            app.workspace_text_enabled = text_enabled
            app.settings = dict(DEFAULT_SETTINGS)
            app.tabs = {}
            app.renumber_default_tabs = Mock()
            app.notebook.tabs.side_effect = lambda: tuple(app.tabs)
            selection = [None]

            def select(frame=None):
                if frame is not None:
                    selection[0] = frame
                return selection[0]

            app.notebook.select.side_effect = select
            app.notebook.index.side_effect = lambda frame: list(app.tabs).index(frame)

            def new_tab(**data):
                uid = data.get("workspace_id") or "new"
                task = Mock(frame=Frame(uid), workspace_id=uid)
                task.conversion_running.return_value = False
                task.text_box.get.return_value = data.get("restored_text", "")
                task.workspace_record.return_value = {
                    "workspace_id": uid, "title": data.get("title"),
                    "text_file": f"{uid}.txt", "preview_bookmark": data.get("restored_preview_bookmark"),
                }
                app.tabs[uid] = task
                return task

            app.new_tab = Mock(side_effect=new_tab)
            with patch.multiple(app_workspace_init, WORKSPACE_TEXT_DIR=self.tab_dir, WORKSPACE_FILE=self.manifest):
                app_workspace_init.AppWorkspaceInitMixin._restore_workspace(app)
            return app

        session = open_workspace(False)
        self.assertEqual(list(session.tabs), ["tab", "second"])
        self.assertEqual(session.notebook.select(), "second")
        self.assertTrue(all(task.text_box.get() == "" for task in session.tabs.values()))
        self.assertIsNone(session.new_tab.call_args_list[0].kwargs["restored_preview_bookmark"])
        session.tabs["tab"].text_box.get.return_value = "Временный текст"
        session.new_tab(workspace_id="new", title="Новая вкладка", restored_text="Другой временный текст")
        self.assertTrue(session.save_workspace())
        session.settings["save_workspace_text"] = True
        session._start_workspace_autosave()
        session._workspace_write[0].result(timeout=3)
        session._finish_workspace_write()
        saved = read_workspace_manifest(self.manifest)
        self.assertEqual(saved["selected_index"], 1)
        self.assertEqual(saved["tabs"][0]["text_file"], legacy_path.name)
        self.assertEqual(saved["tabs"][0]["preview_bookmark"], bookmark)
        self.assertNotIn("text_file", saved["tabs"][2])
        self.assertFalse((self.tab_dir / "new.txt").exists())
        self.assertEqual(legacy_path.read_bytes(), "Первое сохранение".encode("utf-8"))
        self.assertEqual(second_path.read_bytes(), b"Second saved text")

        resumed = open_workspace(True)
        self.assertEqual(resumed.tabs["tab"].text_box.get(), "Первое сохранение")
        self.assertEqual(resumed.tabs["second"].text_box.get(), "Second saved text")
        self.assertEqual(resumed.tabs["new"].text_box.get(), "")
        self.assertEqual(resumed.new_tab.call_args_list[0].kwargs["restored_preview_bookmark"], bookmark)
        self.assertEqual(resumed.notebook.select(), "second")

        session.tab_hotkey_monitors = {}
        session.task_for_tab_index = Mock(return_value=session.tabs["second"])
        session.visible_tab_index = Mock(return_value=2)
        session.tabs["second"].task_id = "second"
        session.tabs["second"]._full_text.return_value = ""
        session.notebook.select("tab")
        with patch.object(app_workspace_save, "archive_workspace_text", side_effect=(False, PermissionError("locked"))) as archiver:
            app_tabs.AppTabsMixin.close_tab_by_index(session, 1)
        self.assertEqual(archiver.call_args.args[1], "Second saved text")
        self.assertIn("second", session.tabs)
        session.notebook.forget.assert_not_called()
        with patch.object(app_tabs, "WORKSPACE_TEXT_DIR", self.tab_dir):
            app_tabs.AppTabsMixin.close_tab_by_index(session, 1)
        self.assertEqual(second_path.read_bytes(), b"Second saved text")
        self.assertEqual([record["workspace_id"] for record in read_workspace_manifest(self.manifest)["tabs"]], ["tab", "new"])
        archive, = self.history.glob("*.txt.gz")
        self.assertEqual(gzip.decompress(archive.read_bytes()), b"Second saved text")

    def test_disabling_saved_versions_does_not_disable_protective_archiving(self):
        self.app.settings = {"archive_text_versions": False}
        self.app.tab.text_box.get.return_value = "замена"
        self.assertTrue(self.app.save_workspace())
        self.assertEqual(self.tab_path.read_bytes(), "замена".encode("utf-8"))
        self.assertEqual(list(self.history.glob("*.txt.gz")), [])
        self.app.tab._full_text.return_value = "замена"
        self.assertTrue(self.app._archive_tab_before_overwrite(self.app.tab, "clear"))
        archive, = self.history.glob("*.txt.gz")
        self.assertEqual(gzip.decompress(archive.read_bytes()), "замена".encode("utf-8"))

    def test_shutdown_preserves_current_text_in_two_tabs_and_reopens_exactly(self):
        before = tk.Tk()
        before.withdraw()
        values = {"tab": "Первый абзац 🧪\n\nПоследняя строка", "second": "日本語\nSecond tab"}
        tabs = {}
        for tab_id, value in values.items():
            task = Mock(frame=tab_id, workspace_id=tab_id, workspace_dirty=False,
                        worker=None, preview_thread=None)
            task.conversion_running.return_value = False
            task.text_box = tk.Text(before)
            task.text_box.insert("1.0", value)
            task.workspace_record.return_value = {"workspace_id": tab_id, "text_file": f"{tab_id}.txt"}
            tabs[tab_id] = task
        self.app.tabs = tabs
        self.app.notebook.tabs.return_value = list(tabs)
        self.app.root.destroy.side_effect = before.destroy
        self.app.on_close()
        self.app.root.destroy.assert_called_once()
        self.assertEqual([record["workspace_id"] for record in read_workspace_manifest(self.manifest)["tabs"]],
                         list(values))
        new_root = tk.Tk()
        new_root.withdraw()
        self.addCleanup(new_root.destroy)
        reopened = App()
        reopened.tabs = {}
        reopened.settings = dict(DEFAULT_SETTINGS)
        reopened.renumber_default_tabs = Mock()

        def new_tab(**kwargs):
            tab_id = kwargs["workspace_id"]
            widget = tk.Text(new_root)
            widget.insert("1.0", kwargs["restored_text"])
            tab = SimpleNamespace(frame=tab_id, text_box=widget)
            reopened.tabs[tab_id] = tab
            return tab

        reopened.new_tab = new_tab
        with patch.multiple(app_workspace_init, WORKSPACE_TEXT_DIR=self.tab_dir, WORKSPACE_FILE=self.manifest):
            app_workspace_init.AppWorkspaceInitMixin._restore_workspace(reopened)
        self.assertEqual({tab_id: tab.text_box.get("1.0", "end-1c") for tab_id, tab in reopened.tabs.items()},
                         values)
        events = [call.args[0] for call in self.app.logger.event.call_args_list]
        self.assertIn("workspace_saved_on_close", events)

    def test_replacing_saved_text_keeps_exact_previous_version(self):
        self.app.tab.workspace_dirty = True
        self.app.tab.text_box.get.return_value = "previous text and new tail"
        self.assertTrue(self.app.save_workspace())
        self.assertEqual(list(self.history.glob("*.txt.gz")), [])
        self.app.tab.workspace_dirty = True
        self.app.tab.text_box.get.return_value = ""
        self.assertTrue(self.app.save_workspace())
        (archive,) = self.history.glob("*.txt.gz")
        self.assertEqual(gzip.decompress(archive.read_bytes()).decode("utf-8"), "previous text and new tail")
        self.assertEqual(self.tab_path.read_bytes(), b"")

    def test_archive_failure_keeps_previous_file_and_prevents_program_close(self):
        self.app.tab.text_box.get.return_value = ""
        with patch.object(app_workspace_save, "archive_workspace_text", side_effect=PermissionError("archive locked")):
            self.app.on_close()
        self.app.root.destroy.assert_not_called()
        self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "previous text")
        self.assertTrue(self.app.tab.workspace_dirty)

    def test_failed_save_keeps_window_and_dirty_text_until_retry(self):
        with patch.object(app_workspace_save, "atomic_write_text", side_effect=PermissionError("locked")):
            self.app.on_close()
        self.app.root.destroy.assert_not_called()
        self.app.engine_manager.close.assert_not_called()
        self.app._stop_tab_hotkey_monitor.assert_not_called()
        self.app.logger.close.assert_not_called()
        self.dialog.assert_called_once()
        self.assertFalse(self.app.shutdown_in_progress)
        self.assertTrue(self.app.tab.workspace_dirty)
        self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "previous text")

        self.app.on_close()
        self.app.root.destroy.assert_called_once()
        self.app.logger.close.assert_called_once_with(status="closed")
        self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "новая строка\nnew line")
        self.assertTrue(self.manifest.is_file())

    def test_manifest_failure_during_final_save_keeps_window_after_worker(self):
        thread = Mock()
        thread.is_alive.return_value = True
        self.app.tab.worker = thread
        self.app.on_close()
        self.app.root.destroy.assert_not_called()
        self.assertTrue(self.app.shutdown_in_progress)
        self.app.engine_manager.stop.set.assert_called_once()

        self.app.tab.text_box.get.return_value = "added while closing"
        self.app.tab.workspace_dirty = True
        thread.is_alive.return_value = False
        with patch.object(app_workspace_save, "atomic_write_json", side_effect=PermissionError("locked")):
            self.app._poll_shutdown()
        self.app.root.destroy.assert_not_called()
        self.app.engine_manager.close.assert_not_called()
        self.assertFalse(self.app.shutdown_in_progress)
        self.dialog.assert_called_once()
        self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "added while closing")
        self.app.on_close()
        self.app.root.destroy.assert_called_once()

    def test_unreadable_tab_retains_original_and_saves_new_capture_separately(self):
        atomic_write_json(self.manifest, {"tabs": [{
            "workspace_id": "tab", "text_file": "tab.txt", "title": "Книга",
            "copy_hotkey": "F8",
        }]})
        self.app.settings = dict(DEFAULT_SETTINGS)
        self.app.renumber_default_tabs = Mock()
        self.app.schedule_workspace_save = Mock()
        indexed = []

        def new_tab(**kwargs):
            workspace_id = kwargs.get("workspace_id") or "new-tab"
            task = Mock(frame=workspace_id, workspace_id=workspace_id,
                        workspace_dirty=not bool(kwargs.get("workspace_id")))
            task.text_box.edit_modified.return_value = False
            task.text_box.get.return_value = kwargs.get("restored_text", "")
            task.workspace_record.return_value = {
                "workspace_id": workspace_id, "text_file": f"{workspace_id}.txt",
                "title": kwargs.get("title"),
                "copy_hotkey": kwargs["restored_settings"]["copy_hotkey"],
            }
            self.app.tabs[workspace_id] = task
            indexed.append(task.frame)
            return task

        self.app.new_tab = new_tab
        self.app.notebook.tabs.side_effect = lambda: indexed
        original_read = Path.read_bytes

        def read_while_locked(path):
            if path == self.tab_path:
                raise PermissionError("locked during startup")
            return original_read(path)

        def reopen():
            self.app.tabs.clear()
            indexed.clear()
            app_workspace_init.AppWorkspaceInitMixin._restore_workspace(self.app)

        with patch.multiple(app_workspace_init, WORKSPACE_TEXT_DIR=self.tab_dir,
                            WORKSPACE_FILE=self.manifest), patch.object(
                                app_workspace_init.messagebox, "showwarning"):
            with patch.object(Path, "read_bytes", read_while_locked):
                reopen()
            (new,) = self.app.tabs.values()
            new.text_box.get.return_value = "новый захват 🧪"
            new.workspace_dirty = True
            self.assertTrue(self.app.save_workspace())
            self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "previous text")
            self.assertEqual(new.workspace_record()["copy_hotkey"], "F8")
            self.assertEqual((self.tab_dir / "new-tab.txt").read_text(encoding="utf-8"),
                             "новый захват 🧪")

            with patch.object(Path, "read_bytes", read_while_locked):
                reopen()
            self.assertEqual(list(self.app.tabs), ["new-tab"])
            self.assertTrue(self.app.save_workspace())
            reopen()
            self.assertEqual(self.app.tabs["tab"].text_box.get(), "previous text")
            self.assertEqual(self.app.tabs["new-tab"].text_box.get(), "новый захват 🧪")
            self.assertEqual(self.app.tabs["tab"].workspace_record()["copy_hotkey"], "Нет")

    def test_editor_change_does_not_postpone_scheduled_capture_save(self):
        self.app.schedule_workspace_save(delay_ms=500, keep_earliest=True)
        due = self.app.workspace_save_due_perf
        self.app.schedule_workspace_save()
        self.assertEqual(self.app.workspace_save_due_perf, due)
        self.app.root.after.assert_called_once()

    def test_unreadable_manifest_preserves_previous_tab_records_on_save_retry(self):
        old_record = {"workspace_id": "tab", "text_file": "tab.txt", "title": "Книга"}
        atomic_write_json(self.manifest, {"tabs": [old_record]})
        original_manifest = self.manifest.read_bytes()
        self.app.tabs.clear()
        self.app.settings = dict(DEFAULT_SETTINGS)
        self.app.renumber_default_tabs = Mock()
        self.app.tab.workspace_id = "new-tab"
        self.app.tab.workspace_record.return_value = {"workspace_id": "new-tab"}
        self.app.new_tab = Mock(side_effect=lambda **kwargs: self.app.tabs.update(new=self.app.tab))
        read_text = Path.read_text

        def locked_read(path, *args, **kwargs):
            if path == self.manifest:
                raise PermissionError("workspace list locked")
            return read_text(path, *args, **kwargs)

        with patch.object(app_workspace_init, "WORKSPACE_FILE", self.manifest):
            with patch.object(Path, "read_text", locked_read):
                app_workspace_init.AppWorkspaceInitMixin._restore_workspace(self.app)
                self.assertTrue(self.app.save_workspace())
                self.assertEqual(self.manifest.read_bytes(), original_manifest)
                self.assertEqual(read_workspace_manifest(self.recovery_manifest)["tabs"], [{"workspace_id": "new-tab"}])

        self.assertTrue(self.app.save_workspace())
        self.assertFalse(self.recovery_manifest.exists())
        self.assertIn(old_record, read_workspace_manifest(self.manifest)["tabs"])
        self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "previous text")
        self.assertEqual((self.tab_dir / "new-tab.txt").read_text(encoding="utf-8"),
                         "новая строка\nnew line")

    def test_malformed_workspace_can_close_and_keeps_recovery_manifest(self):
        broken = b'{"tabs": ['
        self.manifest.write_bytes(broken)
        self.app._workspace_manifest_needs_read = True
        self.app.tab.workspace_id = "recovered"
        self.app.tab.workspace_record.return_value = {"workspace_id": "recovered", "text_file": "recovered.txt"}

        self.app.on_close()

        self.app.root.destroy.assert_called_once()
        self.assertEqual(self.manifest.read_bytes(), broken)
        self.assertEqual(read_workspace_manifest(self.recovery_manifest)["tabs"],
                         [{"workspace_id": "recovered", "text_file": "recovered.txt"}])
        self.assertEqual((self.tab_dir / "recovered.txt").read_text(encoding="utf-8"), "новая строка\nnew line")

    def test_locked_stale_recovery_does_not_reopen_tabs_when_primary_is_unreadable(self):
        self.app._workspace_recovery_id = "absorbed"
        self.app.tab.workspace_record.return_value = {"workspace_id": "tab"}
        stale = {"workspace_id": "obsolete", "text_file": "obsolete.txt"}
        (self.tab_dir / stale["text_file"]).write_text("already closed", encoding="utf-8")
        atomic_write_json(self.recovery_manifest, {"recovery_id": "absorbed", "tabs": [stale]})
        original_unlink = Path.unlink

        def locked_unlink(path, *args, **kwargs):
            if path == self.recovery_manifest:
                raise PermissionError("recovery file locked")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", locked_unlink):
            self.assertTrue(self.app.save_workspace())
        self.assertTrue(self.recovery_manifest.exists())

        self.app.tabs.clear()
        self.app.settings = dict(DEFAULT_SETTINGS)
        self.app.renumber_default_tabs = Mock()
        self.app.new_tab = Mock()
        original_read = Path.read_text

        def locked_read(path, *args, **kwargs):
            if path == self.manifest:
                raise PermissionError("workspace file locked")
            return original_read(path, *args, **kwargs)

        with patch.object(app_workspace_init, "WORKSPACE_FILE", self.manifest), \
                patch.object(app_workspace_init, "WORKSPACE_TEXT_DIR", self.tab_dir), \
                patch.object(Path, "read_text", locked_read):
            app_workspace_init.AppWorkspaceInitMixin._restore_workspace(self.app)
        self.app.new_tab.assert_called_once_with(save_workspace=False)

    def test_failed_settings_write_offers_retry_before_destroying_window(self):
        path = self.manifest.with_name("settings.json")
        atomic_write_json(path, {"bitrate": "96k"})
        self.app.root.geometry.return_value = "1200x820"
        self.app.autostart_windows_var = Mock()
        self.app.autostart_windows_var.get.return_value = False
        self.app.settings_after_id = "pending-save"
        self.app.settings = {**DEFAULT_SETTINGS, "bitrate": "192k"}
        self.app.save_settings = app_audio_settings.AppAudioSettingsMixin.save_settings.__get__(self.app)
        with patch.object(app_shutdown.messagebox, "askyesno", return_value=False) as choose, \
                patch.object(app_audio_settings, "SETTINGS_FILE", path):
            with patch.object(app_audio_settings, "atomic_write_json", side_effect=PermissionError("settings locked")):
                self.app.on_close()
            self.app.root.destroy.assert_not_called()
            choose.assert_called_once()
            self.assertFalse(self.app.shutdown_in_progress)
            self.assertIn("96k", path.read_text(encoding="utf-8"))
            self.app.on_close()
        self.app.root.destroy.assert_called_once()
        self.assertIn("192k", path.read_text(encoding="utf-8"))

    def test_startup_read_failure_never_reaches_cleanup_or_writes(self):
        root = Mock()
        with patch.object(app_workspace_init, "load_settings", side_effect=PermissionError("settings locked")), \
                patch.object(app_workspace_init.messagebox, "askretrycancel", return_value=False) as choose, \
                patch.object(app_workspace_init, "ProblemLogger") as logger, \
                patch.object(app_workspace_init, "migrate_text_storage") as migrate:
            app_workspace_init.AppWorkspaceInitMixin(root)
        root.destroy.assert_called_once()
        choose.assert_called_once()
        logger.assert_not_called()
        migrate.assert_not_called()
        root.after_idle.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows sharing modes")
    def test_windows_file_handle_blocks_close_until_released(self):
        try:
            import win32con
            import win32file
        except ImportError:
            self.skipTest("pywin32 is not installed")
        handle = win32file.CreateFile(
            str(self.tab_path), win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            None, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None,
        )
        try:
            self.app.on_close()
            self.app.root.destroy.assert_not_called()
            self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "previous text")
            error = self.app.logger.error.call_args.kwargs
            self.assertEqual(error["stage"], "save_workspace")
            self.assertIn(error["context"]["winerror"], {5, 32, 33})
        finally:
            handle.Close()

        self.app.on_close()
        self.app.root.destroy.assert_called_once()
        self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "новая строка\nnew line")


class TextStorageTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="tts перенос ")))
        self.settings = self.base / "Настройки программы"
        self.settings.mkdir()
        self.text_root = self.base / "Текст"
        self.paths = {
            "WORKSPACE_TEXT_DIR": (self.settings / "Вкладки", self.text_root / "Вкладки"),
            "MP3_TEXT_BACKUPS_DIR": (self.base / "Бэкапы текста MP3", self.text_root / "Бэкапы текста MP3"),
            "RECOVERY_DIR": (self.base / "Незавершённые задачи", self.text_root / "Незавершённые задачи"),
        }
        self.enterContext(patch.multiple(
            runtime_storage, STORAGE_BASE_DIR=self.base, SETTINGS_DIR=self.settings, TEXT_STORAGE_DIR=self.text_root,
            **{name: pair[1] for name, pair in self.paths.items()},
        ))

    def test_migration_preserves_bytes_and_runs_once(self):
        for source, _ in self.paths.values():
            source.mkdir()
            (source / "источник.txt").write_bytes(b"example\r\n")
        self.assertEqual(runtime_storage.migrate_text_storage(), 3)
        self.assertEqual(runtime_storage.migrate_text_storage(), 0)
        for source, target in self.paths.values():
            self.assertFalse(source.exists())
            self.assertEqual((target / "источник.txt").read_bytes(), b"example\r\n")

    def test_conflict_leaves_every_existing_directory_in_place(self):
        self.text_root.mkdir()
        for source, _ in self.paths.values():
            source.mkdir()
        self.paths["RECOVERY_DIR"][1].mkdir()
        with self.assertRaises(FileExistsError):
            runtime_storage.migrate_text_storage()
        for source, _ in self.paths.values():
            self.assertTrue(source.is_dir())
        self.assertFalse(self.paths["WORKSPACE_TEXT_DIR"][1].exists())

    def test_mp3_backup_expiration_leaves_foreign_files(self):
        now = 1_800_000_000
        cutoff = now - 30 * 86400
        directory = self.paths["MP3_TEXT_BACKUPS_DIR"][1]
        directory.mkdir(parents=True)
        expired = directory / "mp3_text_20261119_125421_123456_ab12_Книга.txt"
        fresh = directory / "mp3_text_20261119_125421_123457_ab12_Книга.txt"
        manual = directory / "mp3_text_моя_копия.txt"
        invalid_date = directory / "mp3_text_99999999_999999_999999_x_manual.txt"
        for path, timestamp in ((expired, cutoff), (fresh, cutoff + 1),
                                (manual, cutoff - 10), (invalid_date, cutoff - 10)):
            path.write_bytes(b"example")
            os.utime(path, (timestamp, timestamp))
        with patch.object(time, "time", return_value=now):
            self.assertEqual(runtime_storage.cleanup_old_mp3_text_backups(), (1, 0))
        self.assertFalse(expired.exists())
        self.assertTrue(fresh.is_file())
        self.assertTrue(manual.is_file())
        self.assertTrue(invalid_date.is_file())

    def test_custom_mp3_backup_expiration_keeps_an_active_backup(self):
        now = 1_800_000_000
        directory = self.paths["MP3_TEXT_BACKUPS_DIR"][1]
        directory.mkdir(parents=True)
        active = directory / "mp3_text_20261119_125421_123456_ab12_Активный.txt"
        expired = directory / "mp3_text_20261119_125421_123457_ab12_Старый.txt"
        fresh = directory / "mp3_text_20261119_125421_123458_ab12_Свежий.txt"
        for path, seconds in ((active, 3 * 86400), (expired, 2 * 86400), (fresh, 86400)):
            path.write_bytes(b"example")
            os.utime(path, (now - seconds, now - seconds))
        with patch.object(time, "time", return_value=now):
            self.assertEqual(runtime_storage.cleanup_old_mp3_text_backups(2, protected_paths=frozenset({active})), (1, 0))
        self.assertEqual(set(directory.iterdir()), {active, fresh})

    def test_invalid_retention_values_keep_the_default_age(self):
        for malformed in (0, -1, False, True, "14", [], 50_000):
            with self.subTest(value=malformed):
                normalized = runtime_storage.normalize_settings({"text_backup_retention_days": malformed})
                self.assertEqual(normalized["text_backup_retention_days"], 30)


if __name__ == "__main__":
    unittest.main()
