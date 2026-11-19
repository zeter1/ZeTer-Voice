"""Exercise closing with synthetic tabs and isolated workspace files."""
from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tts_app import app_shutdown, app_workspace_init, app_workspace_save
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
        self.engine_manager = SimpleNamespace(thread=None, close=Mock(), stop=Mock())
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
        self.enterContext(patch.multiple(
            app_workspace_save, WORKSPACE_TEXT_DIR=self.tab_dir, WORKSPACE_FILE=self.manifest,
        ))
        self.dialog = self.enterContext(patch.object(app_shutdown.messagebox, "showerror"))
        self.app = App()

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
                self.assertFalse(self.app.save_workspace())
                self.assertEqual(self.manifest.read_bytes(), original_manifest)

        self.assertTrue(self.app.save_workspace())
        self.assertIn(old_record, read_workspace_manifest(self.manifest)["tabs"])
        self.assertEqual(self.tab_path.read_text(encoding="utf-8"), "previous text")
        self.assertEqual((self.tab_dir / "new-tab.txt").read_text(encoding="utf-8"),
                         "новая строка\nnew line")

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


if __name__ == "__main__":
    unittest.main()
