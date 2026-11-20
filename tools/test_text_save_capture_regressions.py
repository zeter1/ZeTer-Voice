"""Regressions for Windows text, playback cleanup, autosave and MP3 publication."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch
import ctypes
import sys
import time
import tkinter as tk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tts_app import app_workspace_save, ffmpeg_tools
from tts_app.app_hotkeys import AppHotkeysMixin
from tts_app.runtime_storage import atomic_write_text
from tts_app.sapi_core import read_text_file
from tts_app.task_preview_delete import TaskPreviewDeleteMixin
from tts_app.task_preview_player import TaskPreviewPlayerMixin
from tts_app.text_processing import build_preview_segments
from tts_app.windows_hotkey_capture import WindowsCopyCapture
from tts_app.windows_hotkeys import GlobalCopyHotkeyMonitor


class Regressions(unittest.TestCase):
    def test_exact_crlf_backup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            atomic_write_text(path, "First\r\nSecond\r\n")
            self.assertEqual(path.read_bytes(), b"First\r\nSecond\r\n")

    def test_cp1251_prefers_legacy_text_to_bomless_utf16(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            sample = "\u041c\u0430\u043c\u0430 \u043c\u044b\u043b\u0430 \u0440\u0430\u043c\u0443.\n"
            for encoding in ("cp1251", "utf-8-sig", "utf-16"):
                with self.subTest(encoding=encoding):
                    path.write_bytes(sample.encode(encoding))
                    self.assertEqual(read_text_file(path), sample)

    def test_preview_keeps_mid_sentence_edits_and_appended_tail(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        sentence = "First sentence."
        info = dict(playback_verified=True, preview_run_id="preview", start=0, end=len(sentence),
                    text=sentence, raw_segment=sentence)
        for edit, expected in (("middle", "First NEW sentence."), ("tail", "\nUnread."), ("none", "")):
            with self.subTest(edit=edit):
                tab = TaskPreviewDeleteMixin()
                tab.text_box = editor = tk.Text(root)
                editor.insert("1.0", sentence)
                editor.mark_set("snapshot_end", "end-1c")
                editor.mark_gravity("snapshot_end", "left")
                tab.current_preview_run_id = "preview"
                tab.current_preview_base_index = "1.0"
                tab.preview_pending_delete_active = False
                tab.preview_pending_delete_start_mark = "start_mark"
                tab.preview_pending_delete_end_mark = "end_mark"
                tab.preview_snapshot_end_mark = "snapshot_end"
                tab.preview_deleted_chars = tab.session_read_segments = tab.session_read_chars = 0
                tab.delete_read_text_var = SimpleNamespace(get=lambda: True)
                tab.app = Mock()
                tab.schedule_queue_status_refresh = tab._accumulate_delete_log = Mock()
                if edit == "middle":
                    editor.insert("1.6", "NEW ")
                TaskPreviewPlayerMixin.handle_preview_segment_done(tab, info)
                if edit == "tail":
                    editor.insert("end-1c", "\nUnread.")
                tab.finalize_delete_read_text()
                self.assertEqual(editor.get("1.0", "end-1c"), expected)

    def test_mp3_publication_preserves_new_destination_and_candidate(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "book.mp3"
            candidate = Path(directory) / "staged.mp3"
            original = ffmpeg_tools.mp3_target_identity(target)
            target.write_bytes(b"unrelated")
            candidate.write_bytes(b"verified audio candidate")
            actual = ffmpeg_tools.publish_mp3(candidate, target, original)
            self.assertEqual(target.read_bytes(), b"unrelated")
            self.assertEqual(actual.read_bytes(), b"verified audio candidate")
            self.assertNotEqual(target, actual)
            candidate.write_bytes(b"approved replacement")
            actual = ffmpeg_tools.publish_mp3(candidate, target, ffmpeg_tools.mp3_target_identity(target))
            self.assertEqual(actual, target)
            self.assertEqual(target.read_bytes(), b"approved replacement")

    def test_autosave_writes_in_background_and_keeps_edits_for_final_save(self):
        from threading import get_ident
        main_thread = get_ident()
        release = Event()
        started = Event()
        seen = []
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        path_dir = self.enterContext(TemporaryDirectory())
        base = Path(path_dir)
        notebook = Mock()
        notebook.tabs.return_value = ["frame"]
        notebook.index.return_value = 0
        app = app_workspace_save.AppWorkspaceSaveMixin()
        editor = tk.Text(root)
        editor.insert("1.0", "old snapshot")
        tab = SimpleNamespace(frame="frame", workspace_id="tab", workspace_dirty=True,
                              text_box=editor, workspace_record=lambda: {"workspace_id": "tab"})
        app.root, app.notebook, app.tabs = root, notebook, {"tab": tab}
        app.shutdown_in_progress = False
        app.workspace_after_id = app.workspace_save_due_perf = None
        app.workspace_save_failure_count = 0
        app.logger = Mock()

        def write(path, text):
            seen.append(get_ident())
            started.set()
            if get_ident() != main_thread:
                self.assertTrue(release.wait(3))
            return atomic_write_text(path, text)

        with patch.multiple(app_workspace_save, WORKSPACE_TEXT_DIR=base / "tabs",
                            WORKSPACE_FILE=base / "workspace.json", atomic_write_text=write):
            try:
                app.schedule_workspace_save(delay_ms=0)
                root.update()
                self.assertTrue(started.wait(1))
                self.assertNotEqual(seen[0], main_thread)
                editor.insert("end-1c", " with new edit")
                tab.workspace_dirty = True
            finally:
                release.set()
            self.assertTrue(app.save_workspace())
        self.assertEqual((base / "tabs/tab.txt").read_text(encoding="utf-8"),
                         "old snapshot with new edit")

    def test_preview_segmentation_keeps_offsets(self):
        original = " Abc! Next\nThird? Last"
        self.assertEqual([(s["start"], s["end"], s["text"]) for s in build_preview_segments(original)],
                         [(1, 5, "Abc!"), (6, 10, "Next"), (11, 17, "Third?"), (18, 22, "Last")])


class CaptureRegressions(unittest.TestCase):
    def setUp(self):
        self.root = Mock()
        self.service = WindowsCopyCapture(self.root)
        self.service._wake_reader = Mock()
        self.service._wake_keyboard = Mock()
        self.service._send_ctrl_c_to_active_window = Mock(return_value=True)
        self.service._process_name_from_pid = lambda pid: "source.exe"
        self.sequence, self.pid, self.hwnd = 10, 901234, 901235
        self.service._clipboard_sequence_number = lambda: self.sequence
        self.service._clipboard_owner_pid = lambda: self.pid
        self.service._foreground_capture_context = lambda: {
            "source_hwnd": self.hwnd, "source_pid": self.pid, "source_focus": self.hwnd,
            "clipboard_sequence_before": self.sequence, "trigger_perf": time.perf_counter(),
        }
        self.snapshot = None
        self.service._read_unicode_snapshot = lambda context, observed_sequence, **kwargs: self.snapshot
        self.results = []

    def register(self, hotkey):
        monitor = GlobalCopyHotkeyMonitor(self.root, lambda info: self.results.append((hotkey, info)),
                                         hotkey=hotkey)
        monitor.enabled = True
        self.service.register(monitor)
        return monitor

    def request(self, key, *, native=False):
        return self.service._request_capture(key, {"CTRL"} if native else set(), native=native)

    def snapshot_update(self, text):
        self.sequence += 1
        self.snapshot = self.sequence, text, None
        self.service._wait_for_clipboard_change()
        self.snapshot = None

    def delivered(self):
        self.service._poll_events()
        return [(key, info["captured_text"], info["capture_rejected"]) for key, info in self.results]

    def test_rapid_native_copies_keep_unicode_snapshots_until_tk_can_deliver(self):
        self.register("Ctrl+C")
        self.assertFalse(self.request(ord("C"), native=True))
        self.snapshot_update("Первый 🧪\r\n")
        self.assertFalse(self.request(ord("C"), native=True))
        self.snapshot_update("第二\n")
        self.assertEqual(self.results, [])
        self.assertEqual(self.delivered(), [
            ("Ctrl+C", "Первый 🧪\r\n", None),
            ("Ctrl+C", "第二\n", None),
        ])

    def test_two_tabs_cannot_claim_one_clipboard_update(self):
        self.register("F8")
        self.register("F9")
        self.assertTrue(self.request(0x77))
        self.service._send_pending_copy()
        self.assertTrue(self.request(0x78))
        self.snapshot_update("Только первая вкладка")
        self.assertTrue(self.request(0x78))
        self.service._send_pending_copy()
        self.snapshot_update("Повтор во вторую")
        self.assertEqual(self.delivered(), [
            ("F9", None, "capture_busy"),
            ("F8", "Только первая вкладка", None),
            ("F9", "Повтор во вторую", None),
        ])
        self.assertEqual(self.service._send_ctrl_c_to_active_window.call_count, 2)

    def test_ambiguous_native_update_cannot_satisfy_pending_hotkey(self):
        self.register("Ctrl+C")
        self.register("F8")
        self.request(0x77)
        self.service._send_pending_copy()
        self.assertFalse(self.request(ord("C"), native=True))
        self.snapshot_update("Чей результат неизвестно")
        self.snapshot_update("Вторая незавершённая копия")
        self.assertTrue(self.request(0x77))
        self.service._send_pending_copy()
        self.snapshot_update("Новое выделение")
        self.assertEqual(self.delivered(), [
            ("F8", None, "clipboard_superseded"),
            ("Ctrl+C", None, "clipboard_superseded"),
            ("F8", "Новое выделение", None),
        ])
        self.assertEqual(self.service._send_ctrl_c_to_active_window.call_count, 2)

    def test_unassigned_native_copy_does_not_satisfy_custom_capture(self):
        self.register("F8")
        self.assertFalse(self.request(ord("C"), native=True))
        self.assertTrue(self.request(0x77))
        self.service._send_pending_copy()
        self.service._read_unicode_snapshot = Mock(side_effect=AssertionError("Unassigned Ctrl+C"))
        self.snapshot_update("Ручное копирование")
        self.assertEqual(self.delivered(), [("F8", None, "capture_busy")])
        self.service._send_ctrl_c_to_active_window.assert_not_called()

    def test_locked_clipboard_retries_same_version_and_rejects_replacement(self):
        self.register("Ctrl+C")
        self.request(ord("C"), native=True)
        self.sequence += 1
        self.service._wait_for_clipboard_change()
        self.snapshot = self.sequence, "Открыли тот же буфер", None
        self.service._wait_for_clipboard_change()
        self.assertEqual(self.delivered(), [("Ctrl+C", "Открыли тот же буфер", None)])
        self.request(ord("C"), native=True)
        self.snapshot = None
        self.sequence += 1
        self.service._wait_for_clipboard_change()
        self.snapshot_update("Содержимое заменили во время блокировки")
        self.assertEqual(self.delivered()[-1], ("Ctrl+C", None, "clipboard_superseded"))

    def test_hotkey_starts_fresh_after_timeout_without_manual_copy(self):
        self.register("F8")
        self.request(0x77)
        self.service._send_pending_copy()
        self.service._pending["deadline"] = 0
        self.service._wait_for_clipboard_change()
        self.request(0x77)
        self.service._send_pending_copy()
        self.service._wait_for_clipboard_change()
        self.assertEqual(self.delivered(), [("F8", None, "clipboard_timeout")])
        self.snapshot_update("Новое выделение")
        self.assertEqual(self.delivered()[-1], ("F8", "Новое выделение", None))
        self.assertEqual(self.service._send_ctrl_c_to_active_window.call_count, 2)

    def test_slow_source_gets_full_response_window_after_send(self):
        self.register("F8")
        now = time.perf_counter()
        with patch("tts_app.windows_hotkey_capture.time.perf_counter", return_value=now) as clock:
            self.request(0x77)
            clock.return_value = now + 0.5
            self.service._send_pending_copy()
            clock.return_value = now + 2.5
            self.snapshot_update("Медленный ответ")
            self.assertEqual(self.delivered(), [("F8", "Медленный ответ", None)])
            evidence = self.results[0][1]
            self.assertEqual(evidence["capture_stage"], "snapshot_ready")
            self.assertEqual(evidence["dispatch_ms"], 500)
            self.assertEqual(evidence["clipboard_update_ms"], 2500)
        self.service._send_ctrl_c_to_active_window.assert_called_once()

    def test_confirmed_clipboard_update_can_finish_reading_after_response_deadline(self):
        self.register("F8")
        now = time.perf_counter()
        with patch("tts_app.windows_hotkey_capture.time.perf_counter", return_value=now) as clock:
            self.request(0x77)
            self.service._send_pending_copy()
            clock.return_value = now + 2.9
            self.sequence += 1
            self.service._wait_for_clipboard_change()
            clock.return_value = now + 3.3
            self.snapshot = self.sequence, "Текст после освобождения буфера", None
            self.service._wait_for_clipboard_change()
            self.assertEqual(self.delivered(), [("F8", "Текст после освобождения буфера", None)])
            self.assertEqual(self.results[0][1]["read_attempts"], 2)

    def test_timeout_evidence_distinguishes_dispatch_and_source_response(self):
        self.register("F8")
        self.request(0x77)
        self.service._pending["deadline"] = 0
        self.delivered()
        self.request(0x77)
        self.service._send_pending_copy()
        self.service._pending["deadline"] = 0
        self.delivered()
        self.assertEqual([info["capture_stage"] for _, info in self.results],
                         ["dispatch", "waiting_clipboard"])
        self.assertEqual([info["capture_id"] for _, info in self.results], [1, 2])
        self.assertNotIn("dispatch_ms", self.results[0][1])
        self.assertIn("dispatch_ms", self.results[1][1])

    def test_failed_windows_message_does_not_wait_for_clipboard(self):
        self.register("F8")
        self.service._wake_keyboard.return_value = False
        self.assertTrue(self.request(0x77))
        self.assertIsNone(self.service._pending)
        self.assertEqual(self.delivered(), [("F8", None, "clipboard_dispatch_failed")])
        self.assertEqual(self.results[0][1]["capture_stage"], "dispatch")
        self.service._send_ctrl_c_to_active_window.assert_not_called()

    def test_update_after_timeout_while_idle_is_not_sent_to_another_tab(self):
        self.register("F8")
        self.register("F9")
        self.request(0x77)
        self.service._send_pending_copy()
        self.service._pending["deadline"] = 0
        self.service._wait_for_clipboard_change()
        self.snapshot_update("Просроченный текст")
        self.request(0x78)
        self.service._send_pending_copy()
        self.service._wait_for_clipboard_change()
        self.assertEqual(self.delivered(), [("F8", None, "clipboard_timeout")])
        self.snapshot_update("Новый текст")
        self.assertEqual(self.delivered()[-1], ("F9", "Новый текст", None))

    def test_unassigned_manual_copy_timeout_does_not_block_f8(self):
        self.register("F8")
        self.assertFalse(self.request(ord("C"), native=True))
        self.service._pending["deadline"] = 0
        self.assertTrue(self.request(0x77))
        self.service._send_pending_copy()
        self.snapshot_update("Выделение после пустого Ctrl+C")
        self.assertEqual(self.delivered(), [("F8", "Выделение после пустого Ctrl+C", None)])

    def test_tk_times_out_during_delayed_render_and_drops_late_result(self):
        started, release = Event(), Event()

        def blocked_read(context, observed_sequence, *, progress):
            progress("reading_unicode")
            started.set()
            if not release.wait(2):
                return None
            return observed_sequence, "Поздний снимок", None

        self.register("Ctrl+C")
        self.request(ord("C"), native=True)
        self.sequence += 1
        self.service._read_unicode_snapshot = blocked_read
        worker = Thread(target=self.service._wait_for_clipboard_change)
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            self.service._pending["deadline"] = 0
            self.assertEqual(self.delivered(), [("Ctrl+C", None, "clipboard_timeout")])
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.delivered(), [("Ctrl+C", None, "clipboard_timeout")])
        self.assertEqual(self.results[0][1]["capture_stage"], "reading_unicode")

    def test_foreground_change_aborts_copy_and_binding_replacement_drops_old_snapshot(self):
        monitor = self.register("F8")
        self.request(0x77)
        self.hwnd += 1
        self.service._send_pending_copy()
        self.service._send_ctrl_c_to_active_window.assert_not_called()
        self.assertEqual(self.delivered(), [("F8", None, "source_window_changed")])
        self.request(0x77)
        self.service._send_pending_copy()
        self.snapshot_update("Текст старого назначения")
        self.service.unregister(monitor)
        monitor._binding_generation += 1
        self.service.register(monitor)
        self.assertEqual(self.delivered(), [("F8", None, "source_window_changed")])

    def test_native_hook_passes_ctrl_c_and_ignores_injected_keys(self):
        pressed, suppressed = set(), set()
        dispatch = lambda key, down, flags=0: self.service._dispatch_keyboard(
            key, is_down=down, flags=flags, pressed=pressed, suppressed=suppressed,
        )
        self.register("Ctrl+C")
        self.assertFalse(dispatch(0xA2, True))
        self.assertFalse(dispatch(ord("C"), True, flags=0x10))
        self.assertIsNone(self.service._pending)
        self.assertFalse(dispatch(ord("C"), True))
        self.assertIsNotNone(self.service._pending)
        self.assertFalse(dispatch(ord("C"), False))
        self.assertFalse(dispatch(0xA2, False))

    def test_printable_chord_is_preserved_on_restore_and_can_be_reassigned(self):
        app = AppHotkeysMixin()
        app.tab_hotkey_monitors = {}
        task = SimpleNamespace(
            workspace_id="tab", task_id="task", applied_copy_hotkey="Нет",
            copy_hotkey_var=Mock(), copy_hotkey_status_var=Mock(), tab_title=lambda: "Test",
        )
        app.tabs = {"tab": task}
        app.logger = Mock()
        app.root = self.root
        app.schedule_workspace_save = Mock()
        task.copy_hotkey_var.get.return_value = "Й+Ц"
        self.assertTrue(app.apply_tab_copy_hotkey(task, silent=True, save_workspace=False))
        self.assertEqual(task.applied_copy_hotkey, "Й+Ц")
        self.assertIn("F8", task.copy_hotkey_status_var.set.call_args.args[0])
        self.assertFalse(app.tab_hotkey_monitors)
        task.copy_hotkey_var.get.return_value = "F8"
        self.root._global_copy_service = self.service
        with patch.object(GlobalCopyHotkeyMonitor, "enable", autospec=True, side_effect=lambda monitor: (
            setattr(monitor, "enabled", True)
        )):
            self.assertTrue(app.apply_tab_copy_hotkey(task, save_workspace=False))
        self.assertEqual(task.applied_copy_hotkey, "F8")

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_win32_snapshot_reads_bounded_utf16_under_clipboard_lock(self):
        text = "".join(f"{index:05d} Строка с уникальным номером 🧪 日本語\r\n"
                       for index in range(50_000)) + "Последняя строка."
        raw = ctypes.create_string_buffer(text.encode("utf-16-le") + b"\x00\x00\xff")
        calls = []
        user = SimpleNamespace(
            OpenClipboard=Mock(side_effect=lambda hwnd: calls.append("open") or True),
            IsClipboardFormatAvailable=Mock(return_value=True),
            GetClipboardData=Mock(return_value=1),
            CloseClipboard=Mock(side_effect=lambda: calls.append("close")),
        )
        kernel = SimpleNamespace(
            GlobalSize=Mock(return_value=ctypes.sizeof(raw)),
            GlobalLock=Mock(side_effect=lambda handle: calls.append("lock") or ctypes.addressof(raw)),
            GlobalUnlock=Mock(side_effect=lambda handle: calls.append("unlock")),
        )
        self.service._reader_hwnd = 1
        self.sequence = 11
        with patch.object(ctypes, "windll", SimpleNamespace(user32=user, kernel32=kernel)):
            result = WindowsCopyCapture._read_unicode_snapshot(self.service, {
                "source_pid": self.pid, "clipboard_sequence_before": 10,
            }, observed_sequence=11)
        self.assertEqual(result, (11, text, None))
        self.assertEqual(calls, ["open", "lock", "unlock", "close"])
        user.GetClipboardData.assert_called_once_with(13)


if __name__ == "__main__":
    unittest.main()
