"""Synthetic capture and paste checks using actual Tk text indices."""
from __future__ import annotations

import hashlib
import sys
import threading
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tts_app.task_capture import TaskCaptureMixin
from tts_app.task_file_io import TaskFileIoMixin
from tts_app.task_preview_delete import TaskPreviewDeleteMixin
from tts_app.task_preview_player import TaskPreviewPlayerMixin
from tts_app.task_preview_state import TaskPreviewStateMixin
from tts_app.runtime_shortcuts import handle_text_ctrl_shortcut
from tts_app import task_text_performance
from tts_app.task_text_performance import TaskTextPerformanceMixin
from tts_app.task_ui_status import TaskUiStatusMixin


class CaptureTask(
    TaskCaptureMixin,
    TaskFileIoMixin,
    TaskPreviewDeleteMixin,
    TaskPreviewPlayerMixin,
    TaskPreviewStateMixin,
    TaskTextPerformanceMixin,
    TaskUiStatusMixin,
):
    def __init__(self, root, text):
        self.task_id = "synthetic-task"
        self.workspace_id = "synthetic-tab"
        self.text_box = tk.Text(root, undo=True)
        self.horizontal_scrollbar = tk.Scrollbar(root)
        self._word_wrap_var = tk.BooleanVar(root, True)
        self.auto_read_hotkey_var = tk.BooleanVar(root, False)
        self.delete_read_text_var = tk.BooleanVar(root, False)
        self.status_var = tk.StringVar(root)
        self.queue_status_var = tk.StringVar(root)
        self.app = SimpleNamespace(
            root=SimpleNamespace(after=Mock(return_value="pending"), clipboard_get=Mock()),
            schedule_workspace_save=Mock(),
            logger=Mock(),
        )
        self.queue_status_after_id = None
        self._text_state_last_log_perf = 0.0
        self.session_captured_chars = 0
        self.session_read_chars = self.session_read_segments = 0
        self.session_deleted_chars = self.session_deleted_segments = 0
        self.preview_is_paused = False
        self.preview_bookmark = self.preview_thread = self.worker = None
        self.current_preview_run_id = self.current_run_id = self.mp3_source_run_id = None
        self.preview_snapshot_end_mark = "preview-end"
        self.mp3_source_end_mark = "mp3-end"
        self.mp3_source_separator_pending = False
        self._start_preview_worker = Mock()
        self.full_reads = 0
        self.configure_text_view_for_content(text, origin="fixture")
        self.text_box.insert("1.0", text)
        self.text_box.edit_modified(False)

    def _full_text(self):
        self.full_reads += 1
        return super()._full_text()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_large_capture_burst_needs_no_whole_widget_read_until_status(self):
        original = ("Первый абзац. Text 🧪\n" * 60_000) + "last line 🙂"
        task = CaptureTask(self.root, original)
        added = "  Latin text\r\n\r\n日本語 🙂\n"
        following = "Следующая строка!"
        self.assertTrue(task.append_external_selected_text(added))
        self.assertTrue(task.append_external_selected_text(following))
        self.assertEqual(task.full_reads, 0)
        expected = original + "\n" + added + following
        self.assertEqual(task.text_box.get("1.0", "end-1c"), expected)

        task.app.root.after.assert_called_once()
        task.app.root.after.call_args.args[1]()
        self.assertEqual(task.full_reads, 1)
        expected_hash = hashlib.sha256(expected.encode("utf-8")).hexdigest()[:16]
        state_events = [
            call.kwargs for call in task.app.logger.event.call_args_list
            if call.args[0] == "text_state"
        ]
        self.assertEqual(len(state_events), 1)
        self.assertEqual(state_events[0]["text_sha256_16"], expected_hash)
        self.assertEqual(state_events[0]["text_chars"], len(expected))

    def test_fresh_auto_read_receives_exact_suffix_and_anchor(self):
        original = "Предыдущий текст 🙂"
        task = CaptureTask(self.root, original)
        task.auto_read_hotkey_var.set(True)
        text = "  First 🧪\n\nВторая строка \r\n"
        self.assertTrue(task.append_external_selected_text(text))
        arguments = task._start_preview_worker.call_args.kwargs
        self.assertEqual(arguments["raw_text"], text)
        self.assertEqual(task.text_box.get(arguments["preview_base_index"], "end-1c"), text)
        self.assertEqual(task.full_reads, 0)

    def test_player_starts_captured_suffix_without_rereading_full_widget(self):
        original = "Existing text 🧪" * 250
        task = CaptureTask(self.root, original)
        del task._start_preview_worker
        task.auto_read_hotkey_var.set(True)
        task.voice_var = tk.StringVar(self.root, "Synthetic")
        task.audio_output_var = tk.StringVar(self.root)
        task.rate_var = task.pitch_var = tk.IntVar(self.root, 0)
        task.volume_var = tk.IntVar(self.root, 100)
        task.app.stop_other_previews = Mock()
        task.preview_stop_event = threading.Event()
        task.preview_pause_event = threading.Event()
        task.preview_base_mark = "preview-start"
        task._set_playback_controls = Mock()
        added = "First line 😀\nВторая строка."
        worker = Mock()
        with patch("tts_app.task_preview_player.threading.Thread", return_value=worker):
            self.assertTrue(task.append_external_selected_text(added))
        worker.start.assert_called_once()
        self.assertEqual(task.current_preview_base_text_offset, len(original) + 1)
        self.assertEqual(task.current_preview_snapshot_end, len(original + "\n" + added))
        self.assertEqual(task.text_box.get(task.preview_base_mark, task.preview_snapshot_end_mark), added)
        self.assertEqual(task.full_reads, 0)

    def test_nonverbal_capture_remains_in_text_without_starting_preview(self):
        task = CaptureTask(self.root, "Old")
        task.auto_read_hotkey_var.set(True)
        self.assertTrue(task.append_external_selected_text("🧪\n…"))
        self.assertEqual(task.text_box.get("1.0", "end-1c"), "Old\n🧪\n…")
        task._start_preview_worker.assert_not_called()

    def test_preview_extension_uses_python_offsets_with_emoji(self):
        original = "Активное чтение 🙂"
        task = CaptureTask(self.root, original)
        task.auto_read_hotkey_var.set(True)
        task.preview_thread = SimpleNamespace(is_alive=lambda: True)
        task.current_preview_follow_end = False
        added = "Следующая 🧪\nLast."
        self.assertTrue(task.append_external_selected_text(added))
        self.assertEqual(task.current_preview_range_end, len(original + "\n" + added))
        self.assertEqual(task.text_box.get("1.0", task.preview_snapshot_end_mark), original)
        self.assertEqual(task.full_reads, 1)
        task._start_preview_worker.assert_not_called()

    def test_running_preview_skips_nonverbal_tail_until_readable_text_arrives(self):
        task = CaptureTask(self.root, "Already reading 🙂")
        task.auto_read_hotkey_var.set(True)
        task.preview_thread = SimpleNamespace(is_alive=lambda: True)
        task.current_preview_follow_end = True
        task.text_box.mark_set(task.preview_snapshot_end_mark, "end-1c")
        task.text_box.mark_gravity(task.preview_snapshot_end_mark, "left")
        self.assertTrue(task.append_external_selected_text("🧪\n…"))
        self.assertIsNone(task._unread_appended_tail())
        self.assertEqual(task.full_reads, 0)

        self.assertTrue(task.append_external_selected_text("Now readable."))
        tail, _ = task._unread_appended_tail()
        self.assertEqual(tail, "\n🧪\n…\nNow readable.")
        self.assertEqual(task.full_reads, 1)

    def test_saved_preview_bookmark_remains_valid_after_append(self):
        original = "Абзац 🙂. Чтение на паузе."
        task = CaptureTask(self.root, original)
        task.auto_read_hotkey_var.set(True)
        task.preview_is_paused = True
        task.preview_bookmark = {
            "char_offset": 10,
            "text_chars": len(original),
            "text_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        }
        self.assertTrue(task.append_external_selected_text("Next 😀."))
        self.assertTrue(task._bookmark_is_valid())
        self.assertEqual(task.preview_bookmark["end_offset"], len(original + "\nNext 😀."))
        task._start_preview_worker.assert_not_called()

    def test_hotkey_tail_remains_outside_mp3_source_mark(self):
        original = "Текущий MP3 🧪"
        task = CaptureTask(self.root, original)
        task.current_run_id = task.mp3_source_run_id = "synthetic-mp3"
        task.text_box.mark_set(task.mp3_source_end_mark, "end-1c")
        task.text_box.mark_gravity(task.mp3_source_end_mark, "left")
        added = "  English\nСледующий абзац 🙂\n"
        self.assertTrue(task.append_external_selected_text(added))
        self.assertTrue(task.mp3_source_separator_pending)
        self.assertEqual(task.text_box.get("1.0", task.mp3_source_end_mark), original)
        self.assertEqual(task.text_box.get(task.mp3_source_end_mark, "end-1c"), "\n" + added)
        self.assertEqual(task.full_reads, 0)

    def test_caret_paste_detects_long_joined_line_and_preserves_selection_order(self):
        original = "A" * 12_000 + "replace" + "B" * 7000
        task = CaptureTask(self.root, original)
        task.text_box.tag_add("sel", "1.12000", "1.12007")
        added = "🧪" + "X" * 1500
        task.app.root.clipboard_get.return_value = added
        replace = task.text_box.replace
        wrap_before_insert = []

        def record_wrap(start, end, text):
            wrap_before_insert.append(task.text_box.cget("wrap"))
            return replace(start, end, text)

        task.text_box.replace = record_wrap
        task.paste_text_from_clipboard()
        self.assertEqual(wrap_before_insert, ["none"])
        self.assertEqual(task.text_box.get("1.0", "end-1c"), "A" * 12_000 + added + "B" * 7000)
        self.assertEqual(task.text_box.get("1.12000", "insert"), added)
        self.assertEqual(task.full_reads, 0)

    def test_metrics_preserve_sentence_counts_with_long_punctuation_runs(self):
        task = CaptureTask(self.root, "..." * 10_000 + "word!?!\nNext…")
        self.assertEqual(task._text_metrics()["sentences"], 2)

    def test_button_paste_scrolls_to_start_of_long_line_without_moving_caret(self):
        task = CaptureTask(self.root, "Heading\n\nTail")
        task.text_box.mark_set("insert", "2.0")
        added = "я" * 60_000 + "🧪"
        task.app.root.clipboard_get.return_value = added
        viewport = []
        task.text_box.see = lambda index: viewport.append(task.text_box.index(index))

        task.paste_text_from_clipboard()

        self.assertEqual(viewport, ["2.0"])
        self.assertEqual(task.text_box.get("2.0", "insert"), added)
        self.assertEqual(task.text_box.get("1.0", "end-1c"), f"Heading\n{added}\nTail")
        event = task.app.logger.event.call_args
        self.assertEqual(event.args[0], "clipboard_text_pasted")
        self.assertEqual(event.kwargs["source"], "paste_button")
        self.assertEqual(event.kwargs["chars"], len(added))
        self.assertNotIn(added, repr(event))

    def test_missing_clipboard_is_logged_for_button_and_editor(self):
        task = CaptureTask(self.root, "Existing text")
        task.app.root.clipboard_get.side_effect = tk.TclError("PRIVATE_CLIPBOARD_ERROR")
        self.assertEqual(task.paste_text_from_editor(), "break")
        with patch("tts_app.task_capture.messagebox.showwarning"):
            task.paste_text_from_clipboard()
        events = task.app.logger.event.call_args_list
        self.assertEqual([event.args[0] for event in events], ["text_paste_rejected"] * 2)
        self.assertEqual([event.kwargs["source"] for event in events], ["editor_paste", "paste_button"])
        self.assertTrue(all(event.kwargs["stage"] == "clipboard_read" for event in events))
        self.assertNotIn("PRIVATE_CLIPBOARD_ERROR", repr(events))
        self.assertEqual(task.text_box.get("1.0", "end-1c"), "Existing text")

    def test_disabled_editor_declines_capture_instead_of_reporting_success(self):
        task = CaptureTask(self.root, "Existing text")
        task.text_box.configure(state="disabled")
        self.assertFalse(task.append_external_selected_text("Never inserted"))
        self.assertFalse(task.app.logger.event.called)
        self.assertEqual(task.text_box.get("1.0", "end-1c"), "Existing text")

    def test_ctrl_v_prepares_joined_line_and_replaces_selection_as_one_undo(self):
        left, right = "А" * 12_000, "Б" * 9_000
        original = f"{left}replace\nacross{right}"
        task = CaptureTask(self.root, original)
        box = task.text_box
        box.pack()
        self.root.deiconify()
        self.root.update_idletasks()
        box.edit_reset()
        box.tag_add("sel", "1.12000", "2.6")
        box.mark_set("insert", "2.6")
        task.app.root.clipboard_get.return_value = "🧪Добрый день"
        box.bind("<<Paste>>", task.paste_text_from_editor)
        wrap_at_edit = []
        replace = box.replace

        def record_wrap(start, end, text):
            wrap_at_edit.append(box.cget("wrap"))
            return replace(start, end, text)

        box.replace = record_wrap
        self.assertEqual(handle_text_ctrl_shortcut(box, SimpleNamespace(keycode=86)), "break")

        self.assertEqual(wrap_at_edit, ["none"])
        self.assertEqual(box.get("1.0", "end-1c"), left + "🧪Добрый день" + right)
        self.assertEqual(box.get("1.12000", "insert"), "🧪Добрый день")
        box.edit_undo()
        self.assertEqual(box.get("1.0", "end-1c"), original)
        box.edit_redo()
        self.assertEqual(box.get("1.0", "end-1c"), left + "🧪Добрый день" + right)

    def test_large_queue_count_bounds_sentence_scan_and_keeps_exact_text_metrics(self):
        text = "Текст статьи 🧪. Следующее предложение!\n" * 50_000
        task = CaptureTask(self.root, text)
        matcher = Mock(wraps=task_text_performance._SENTENCE_END)

        with patch.object(task_text_performance, "_SENTENCE_END", matcher):
            metrics = task._text_metrics(include_hash=True)

        self.assertEqual(metrics["chars"], len(text))
        self.assertEqual(metrics["sha16"], hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
        self.assertLess(abs(metrics["sentences"] - 100_000), 2000)
        self.assertLessEqual(sum(len(call.args[0]) for call in matcher.finditer.call_args_list), 200_000)


if __name__ == "__main__":
    unittest.main()
