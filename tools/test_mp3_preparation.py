"""Synthetic checks for preparing MP3 without blocking the Tk event loop."""
from pathlib import Path
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import unittest
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tts_app import task_conversion_controls as controls


class TestMp3Preparation(unittest.TestCase):
    def setUp(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        task = self.task = controls.TaskConversionControlsMixin()
        task.text_box = tk.Text(root)
        task.text_box.insert("1.0", "Текст до выбора имени. 🧪")
        task.app = SimpleNamespace(
            output_is_used_by_other_task=Mock(return_value=False),
            visible_tab_index=Mock(return_value=1),
            shutdown_in_progress=False, logger=Mock(), log_ui_error=Mock(), events=Queue(),
        )
        task.task_id = "fixture"
        task.workspace_id = "fixture-workspace"
        task.current_run_id = task.worker = None
        task.current_mp3_text_backup_path = None
        task.cancel_event = Event()
        task.voice_var = tk.StringVar(root, "Synthetic")
        task.rate_var = task.pitch_var = tk.IntVar(root, 0)
        task.volume_var = tk.IntVar(root, 100)
        task.bitrate_var = tk.StringVar(root, "96k")
        task.tab_title = Mock(return_value="Synthetic")
        task.mp3_source_sha256 = "synthetic-hash"
        task.track_mp3_source_snapshot = Mock(return_value=True)
        task.discard_mp3_source_snapshot = Mock()
        task.stop_preview = Mock()
        task.progress = {}
        task.start_button = Mock()
        task.cancel_button = Mock()
        task.status_var = tk.StringVar(root)
        task._job_worker = Mock()
        self.enterContext(patch.object(controls, "mp3_target_identity", return_value=None))
        self.enterContext(patch.object(controls.messagebox, "showerror"))

        def choose_output():
            task.text_box.insert("end-1c", "\nДобавлено в диалоге.")
            return Path("synthetic.mp3")

        task.ask_output_path = Mock(side_effect=choose_output)

    def test_backup_and_preparation_use_immutable_snapshot_off_tk(self):
        started, release = Event(), Event()
        backed_up = []
        main_thread = get_ident()
        backup_path = Path("synthetic.txt")
        originals = "Текст до выбора имени. 🧪\nДобавлено в диалоге."

        def make_backup(_run_id, _title, text):
            backed_up.append((get_ident(), text))
            started.set()
            if get_ident() != main_thread and not release.wait(3):
                raise TimeoutError("test did not release the backup")
            return backup_path

        with patch.object(controls, "create_mp3_text_backup", side_effect=make_backup):
            self.task.start_job()
            try:
                self.assertTrue(started.wait(2))
                self.assertNotEqual(backed_up[0][0], main_thread)
                self.task.text_box.insert("end-1c", "\nБудущий MP3.")
                self.task._job_worker.assert_not_called()
            finally:
                release.set()
                self.task.worker.join(timeout=3)
        self.assertFalse(self.task.worker.is_alive())
        self.assertEqual(backed_up[0][1], originals)
        self.assertEqual(self.task._job_worker.call_args.args[1], originals)
        self.assertEqual(self.task.current_mp3_text_backup_path, backup_path)

    def test_failed_backup_does_not_start_synthesis_or_change_text(self):
        with patch.object(controls, "create_mp3_text_backup", side_effect=PermissionError):
            self.task.start_job()
            self.assertIsNotNone(self.task.worker, "Backup must run on the preparation worker")
            self.task.worker.join(timeout=3)
        self.assertFalse(self.task.worker.is_alive())
        self.task._job_worker.assert_not_called()
        _, kind, payload = self.task.app.events.get_nowait()
        self.assertEqual(kind, "job_error")
        self.assertEqual(payload["run_id"], self.task.current_run_id)
        self.assertIn("Добавлено в диалоге.", self.task.text_box.get("1.0", "end-1c"))

    def test_cancel_during_backup_stops_before_synthesis(self):
        entered, release = Event(), Event()

        def make_backup(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release the backup")
            return Path("synthetic.txt")

        with patch.object(controls, "create_mp3_text_backup", side_effect=make_backup):
            self.task.start_job()
            try:
                self.assertTrue(entered.wait(2))
                self.task.cancel_job()
            finally:
                release.set()
                self.task.worker.join(timeout=3)
        self.assertFalse(self.task.worker.is_alive())
        self.task._job_worker.assert_not_called()
        _, kind, payload = self.task.app.events.get_nowait()
        self.assertEqual(kind, "cancelled")
        self.assertEqual(payload["run_id"], self.task.current_run_id)

    def test_destination_identity_belongs_to_file_seen_before_confirmation(self):
        original, later = (1, 2, 3), (4, 5, 6)
        controls.mp3_target_identity.return_value = original

        def confirm(*args):
            controls.mp3_target_identity.return_value = later
            return True

        with patch.object(controls.messagebox, "askyesno", side_effect=confirm), patch.object(
            controls, "create_mp3_text_backup", return_value=Path("synthetic.txt"),
        ):
            self.task.start_job()
            self.task.worker.join(timeout=3)

        self.assertFalse(self.task.worker.is_alive())
        self.assertEqual(self.task._job_worker.call_args.kwargs["approved_output_identity"], original)


if __name__ == "__main__":
    unittest.main()
