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
        self.service._mouse_hook_id = 1
        self.service._wake_reader = Mock()
        self.service._wake_keyboard = Mock()
        self.service._send_ctrl_c_to_active_window = Mock(return_value=True)
        self.service._copy_input_blocker = Mock(return_value=None)
        self.service._clipboard_open_by = Mock(return_value="no_window_reported")
        self.service._process_name_from_pid = lambda pid: "source.exe"
        self.sequence, self.pid, self.hwnd = 10, 901234, 901235
        self.focus = None
        self.service._clipboard_sequence_number = lambda: self.sequence
        self.service._clipboard_owner_pid = lambda: self.pid
        self.service._foreground_capture_context = lambda: {
            "source_hwnd": self.hwnd, "source_pid": self.pid, "source_focus": self.focus or self.hwnd,
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
        self.assertEqual(self.results[0][1]["blocked_by_capture_id"], self.results[1][1]["capture_id"])
        self.assertEqual(self.results[0][1]["blocked_by_capture_stage"], "waiting_clipboard")

    def test_new_window_capture_can_proceed_if_previous_copy_was_never_sent(self):
        self.register("F8")
        self.register("F9")
        self.assertTrue(self.request(0x77))
        self.hwnd += 1
        self.pid += 1
        self.assertTrue(self.request(0x78))
        self.service._send_pending_copy()
        self.service._send_pending_copy()  # A second posted wakeup must not send twice.
        self.snapshot_update("Новый источник 🧪\r\n")
        self.assertEqual(self.delivered(), [
            ("F8", None, "source_window_changed"),
            ("F9", "Новый источник 🧪\r\n", None),
        ])
        self.service._send_ctrl_c_to_active_window.assert_called_once()

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

    def capture_while_source_publishes(self, *, advance_on_open=False, change_owner=False,
                                      change_focus=False, opener_kind="source", first_open_busy=True,
                                      advance_during_owner_sample=False, change_sample_owner=False):
        text = "Тестовый фрагмент 🧪\r\n"
        raw = ctypes.create_string_buffer(text.encode("utf-16-le") + b"\x00\x00")
        writer = self.hwnd + 10
        owner = writer

        def publish_update():
            nonlocal owner
            self.sequence += 3
            if change_owner:
                owner = writer + 2  # Another clipboard owner in the same process.
            if change_focus:
                self.focus = self.hwnd + 1

        def open_clipboard(_handle):
            if first_open_busy and user.OpenClipboard.call_count == 1:
                return False
            if advance_on_open:
                publish_update()
            return True

        def get_owner():
            nonlocal owner
            current = owner
            if user.GetClipboardOwner.call_count == 1 and advance_during_owner_sample:
                self.sequence += 2
                if change_sample_owner:
                    owner = writer + 2
            return current

        user = SimpleNamespace(
            OpenClipboard=Mock(side_effect=open_clipboard),
            GetClipboardOwner=Mock(side_effect=get_owner),
            GetOpenClipboardWindow=Mock(return_value={"source": writer, "other": writer + 1, "unknown": None}[opener_kind]),
            IsClipboardFormatAvailable=Mock(return_value=True),
            GetClipboardData=Mock(return_value=1),
            CloseClipboard=Mock(),
        )
        kernel = SimpleNamespace(
            GlobalSize=Mock(return_value=ctypes.sizeof(raw)),
            GlobalLock=Mock(return_value=ctypes.addressof(raw)),
            GlobalUnlock=Mock(),
        )
        self.service._window_pid = lambda _window: self.pid
        self.service._clipboard_owner_pid = lambda: self.pid
        self.service._read_unicode_snapshot = WindowsCopyCapture._read_unicode_snapshot.__get__(self.service)
        self.register("F8")
        self.request(0x77)
        self.service._send_pending_copy()
        with patch.object(ctypes, "windll", SimpleNamespace(user32=user, kernel32=kernel)):
            self.sequence += 1
            self.service._wait_for_clipboard_change()
            if first_open_busy:
                if not advance_on_open:
                    publish_update()
                self.service._wait_for_clipboard_change(clipboard_notification=True)
            self.service._wait_for_clipboard_change()
            outcomes = self.delivered()
        self.service._send_ctrl_c_to_active_window.assert_called_once()
        return text, user, outcomes

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_first_snapshot_open_accepts_new_version_from_same_owner_window(self):
        text, user, outcomes = self.capture_while_source_publishes(
            advance_on_open=True, first_open_busy=False,
        )
        self.assertEqual(outcomes, [("F8", text, None)])
        user.GetClipboardData.assert_called_once_with(13)
        self.assertEqual(self.results[0][1]["clipboard_sequence_after"], 14)
        self.assertTrue(self.results[0][1]["clipboard_sequence_advanced_at_open"])

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_publication_advances_during_owner_sample_before_first_open(self):
        text, user, outcomes = self.capture_while_source_publishes(
            advance_on_open=True, first_open_busy=False, advance_during_owner_sample=True,
        )
        self.assertEqual(outcomes, [("F8", text, None)])
        user.GetClipboardData.assert_called_once_with(13)
        evidence = self.results[0][1]
        self.assertEqual(evidence["clipboard_sequence_observed"], 11)
        self.assertEqual(evidence["clipboard_sequence_before_open"], 13)
        self.assertEqual(evidence["clipboard_sequence_after"], 16)
        self.assertTrue(evidence["clipboard_sequence_advanced_at_open"])

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_first_snapshot_rejects_owner_changed_during_sampling(self):
        _, user, outcomes = self.capture_while_source_publishes(
            first_open_busy=False, advance_during_owner_sample=True, change_sample_owner=True,
        )
        self.assertEqual(outcomes, [("F8", None, "clipboard_superseded")])
        user.GetClipboardData.assert_not_called()
        self.assertEqual(self.results[0][1]["clipboard_owner_sample"], "owner_window_changed")

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_first_snapshot_open_rejects_new_owner_and_next_f8_works(self):
        _, user, outcomes = self.capture_while_source_publishes(
            advance_on_open=True, first_open_busy=False, change_owner=True,
        )
        self.assertEqual(outcomes, [("F8", None, "clipboard_superseded")])
        user.GetClipboardData.assert_not_called()
        self.service._read_unicode_snapshot = lambda *args, **kwargs: self.snapshot
        self.assertTrue(self.request(0x77))
        self.service._send_pending_copy()
        self.snapshot_update("Следующий фрагмент")
        self.assertEqual(self.delivered()[-1], ("F8", "Следующий фрагмент", None))

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_first_snapshot_open_rejects_changed_source_focus(self):
        _, user, outcomes = self.capture_while_source_publishes(
            advance_on_open=True, first_open_busy=False, change_focus=True,
        )
        self.assertEqual(outcomes, [("F8", None, "clipboard_superseded")])
        user.GetClipboardData.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_locked_source_can_finish_publishing_before_next_poll(self):
        text, user, outcomes = self.capture_while_source_publishes()
        self.assertEqual(outcomes, [("F8", text, None)])
        user.GetClipboardData.assert_called_once_with(13)
        self.assertEqual(self.results[0][1]["clipboard_sequence_after"], 14)
        self.assertTrue(self.results[0][1]["clipboard_sequence_advanced_after_lock"])

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_locked_source_can_finish_publishing_between_poll_and_open(self):
        text, user, outcomes = self.capture_while_source_publishes(advance_on_open=True)
        self.assertEqual(outcomes, [("F8", text, None)])
        user.GetClipboardData.assert_called_once_with(13)
        self.assertEqual(self.results[0][1]["clipboard_sequence_after"], 14)

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_locked_source_recovery_rejects_replacement_even_from_same_process(self):
        _, user, outcomes = self.capture_while_source_publishes(change_owner=True)
        self.assertEqual(outcomes, [("F8", None, "clipboard_superseded")])
        user.GetClipboardData.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_locked_source_recovery_rejects_a_focus_change(self):
        _, user, outcomes = self.capture_while_source_publishes(change_focus=True)
        self.assertEqual(outcomes, [("F8", None, "clipboard_superseded")])
        user.GetClipboardData.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_busy_clipboard_can_finish_with_same_owner_when_opener_is_unknown(self):
        text, user, outcomes = self.capture_while_source_publishes(opener_kind="unknown")
        self.assertEqual(outcomes, [("F8", text, None)])
        user.GetClipboardData.assert_called_once_with(13)
        evidence = self.results[0][1]
        self.assertEqual(evidence["clipboard_sequence_after"], 14)
        self.assertEqual(evidence["read_attempts"], 2)
        self.assertEqual(evidence["clipboard_open_busy_count"], 1)
        self.assertEqual(evidence["clipboard_busy_opener"], "unknown")
        self.assertFalse(evidence["clipboard_source_lock_confirmed"])
        self.assertTrue(evidence["clipboard_sequence_advanced_after_busy"])

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_busy_clipboard_can_advance_during_next_open_with_unrelated_opener(self):
        text, user, outcomes = self.capture_while_source_publishes(
            opener_kind="other", advance_on_open=True,
        )
        self.assertEqual(outcomes, [("F8", text, None)])
        user.GetClipboardData.assert_called_once_with(13)
        self.assertTrue(self.results[0][1]["clipboard_sequence_advanced_after_busy"])
        self.assertEqual(self.results[0][1]["clipboard_busy_opener"], "other_window")

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_busy_clipboard_rejects_replacement_even_from_same_process(self):
        _, user, outcomes = self.capture_while_source_publishes(opener_kind="other", change_owner=True)
        self.assertEqual(outcomes, [("F8", None, "clipboard_superseded")])
        user.GetClipboardData.assert_not_called()
        evidence = self.results[0][1]
        self.assertEqual(evidence["clipboard_source_check"], "owner_window_changed")
        self.assertEqual(evidence["clipboard_sequence_before"], 10)
        self.assertEqual(evidence["clipboard_sequence_observed"], 11)
        self.assertEqual(evidence["clipboard_sequence_latest"], 14)

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes boundary")
    def test_busy_clipboard_rejects_source_focus_change(self):
        _, user, outcomes = self.capture_while_source_publishes(opener_kind="unknown", change_focus=True)
        self.assertEqual(outcomes, [("F8", None, "clipboard_superseded")])
        user.GetClipboardData.assert_not_called()
        self.assertEqual(self.results[0][1]["clipboard_source_check"], "source_focus_changed")

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

    def test_f8_waits_for_mouse_release_before_sending_one_copy(self):
        self.register("F8")
        self.service._copy_input_blocker = Mock(return_value="mouse")
        self.assertTrue(self.request(0x77))
        self.service._send_pending_copy()
        self.service._send_ctrl_c_to_active_window.assert_not_called()
        self.assertEqual(self.service._pending["context"]["capture_stage"], "waiting_input_release")
        self.service._wait_for_clipboard_change()
        self.assertEqual(self.service._wake_keyboard.call_count, 1)
        self.service._copy_input_blocker.return_value = None
        self.service._wait_for_clipboard_change()
        self.assertEqual(self.service._wake_keyboard.call_count, 2)
        self.service._send_pending_copy()
        self.snapshot_update("Выделение завершено")
        self.assertEqual(self.delivered(), [("F8", "Выделение завершено", None)])
        self.service._send_ctrl_c_to_active_window.assert_called_once()

    def test_custom_chord_copies_two_sources_to_the_same_tab(self):
        self.register("Ctrl+F9")
        for text in ("Пример из одного окна", "A different window 🧪\r\n"):
            self.pid += 1
            self.hwnd += 1
            self.assertTrue(self.service._request_capture(0x78, {"CTRL"}, native=False))
            self.service._copy_input_blocker.return_value = "modifier"
            self.service._send_pending_copy()
            self.service._copy_input_blocker.return_value = None
            self.service._wait_for_clipboard_change()
            self.service._send_pending_copy()
            self.snapshot_update(text)
        self.assertEqual(self.delivered(), [
            ("Ctrl+F9", "Пример из одного окна", None),
            ("Ctrl+F9", "A different window 🧪\r\n", None),
        ])
        self.assertEqual(self.service._send_ctrl_c_to_active_window.call_count, 2)

    def start_physical_f8(self):
        self.register("F8")
        pressed, suppressed = set(), set()
        self.assertTrue(self.service._dispatch_keyboard(
            0x77, is_down=True, flags=0, pressed=pressed, suppressed=suppressed, from_hook=True,
        ))
        self.service._send_pending_copy()
        self.assertTrue(self.service._dispatch_keyboard(
            0x77, is_down=False, flags=0, pressed=pressed, suppressed=suppressed, from_hook=True,
        ))
        return pressed, suppressed

    def test_f8_retries_no_response_once_without_a_second_press(self):
        self.start_physical_f8()
        self.service._pending["deadline"] = 0
        self.service._wait_for_clipboard_change()
        self.service._send_pending_copy()
        self.snapshot_update("Повторная команда\r\nЮникод 🧪")
        self.snapshot_update("Запоздалая публикация")
        self.assertEqual(self.delivered(), [("F8", "Повторная команда\r\nЮникод 🧪", None)])
        self.assertEqual(self.results[0][1]["capture_id"], 1)
        self.assertEqual(self.results[0][1]["capture_attempt"], 2)
        self.assertEqual(self.service._send_ctrl_c_to_active_window.call_count, 2)

    def test_telegram_f8_retries_promptly_even_if_cursor_moves(self):
        self.service._process_name_from_pid = lambda pid: "Telegram.exe"
        started = time.perf_counter()
        with patch("tts_app.windows_hotkey_capture.time.perf_counter", return_value=started) as clock:
            self.start_physical_f8()
            self.service._observe_copy_mouse_message(0x0200)  # WM_MOUSEMOVE
            clock.return_value = started + 0.2
            self.service._wait_for_clipboard_change()
            self.service._send_pending_copy()
            self.service._send_ctrl_c_to_active_window.assert_called_once()
            clock.return_value = started + 0.5
            self.service._wait_for_clipboard_change()
            self.service._send_pending_copy()
            self.snapshot_update("Текст из Telegram")
        self.assertEqual(self.delivered(), [("F8", "Текст из Telegram", None)])
        self.assertEqual(self.results[0][1]["capture_attempt"], 2)
        self.assertEqual(self.results[0][1]["capture_retry_mode"], "automatic_early")
        self.assertEqual(self.results[0][1]["capture_retry_ms"], 500)
        self.assertIs(self.results[0][1]["capture_retry_dispatched"], True)
        self.assertEqual(self.service._send_ctrl_c_to_active_window.call_count, 2)

    def test_telegram_f8_preserves_first_response_when_retry_was_already_posted(self):
        self.service._process_name_from_pid = lambda pid: "Telegram.exe"
        started = time.perf_counter()
        with patch("tts_app.windows_hotkey_capture.time.perf_counter", return_value=started) as clock:
            self.start_physical_f8()
            clock.return_value = started + 0.5
            self.service._wait_for_clipboard_change()
            self.assertEqual(self.service._wake_keyboard.call_count, 2)
            self.sequence += 1
            self.service._send_pending_copy()
            self.snapshot = self.sequence, "Первый ответ успел", None
            self.service._wait_for_clipboard_change()
        self.assertEqual(self.delivered(), [("F8", "Первый ответ успел", None)])
        self.service._send_ctrl_c_to_active_window.assert_called_once()

    def test_telegram_f8_does_not_retry_after_mouse_button_or_wheel(self):
        for message, reason in ((0x0201, "mouse_button"), (0x020A, "mouse_wheel")):
            with self.subTest(reason=reason):
                self.setUp()
                self.service._process_name_from_pid = lambda pid: "Telegram.exe"
                self.start_physical_f8()
                self.service._observe_copy_mouse_message(message)
                self.service._pending["deadline"] = 0
                self.service._wait_for_clipboard_change()
                self.service._send_pending_copy()
                self.assertEqual(self.delivered(), [("F8", None, "clipboard_timeout")])
                self.assertEqual(self.results[0][1]["capture_retry_skipped"], reason)
                self.service._send_ctrl_c_to_active_window.assert_called_once()

    def test_telegram_f8_does_not_retry_without_mouse_guard(self):
        self.service._mouse_hook_id = None
        self.service._process_name_from_pid = lambda pid: "Telegram.exe"
        self.start_physical_f8()
        self.service._pending["deadline"] = 0
        self.service._wait_for_clipboard_change()
        self.service._send_pending_copy()
        self.assertEqual(self.delivered(), [("F8", None, "clipboard_timeout")])
        self.assertEqual(self.results[0][1]["capture_retry_skipped"], "input_guard_unavailable")
        self.assertFalse(self.results[0][1]["capture_mouse_guard"])
        self.service._send_ctrl_c_to_active_window.assert_called_once()

    def test_f8_cancels_auto_retry_if_input_or_source_changes(self):
        for change in ("input", "focus", "clipboard"):
            with self.subTest(change=change):
                self.setUp()
                self.start_physical_f8()
                if change == "input":
                    self.service._observe_copy_mouse_message(0x0201)
                elif change == "focus":
                    self.focus = self.hwnd + 1
                else:
                    self.sequence += 1
                self.service._pending["deadline"] = 0
                self.service._wait_for_clipboard_change()
                self.service._send_pending_copy()
                self.assertEqual(self.delivered(), [("F8", None, "clipboard_timeout")])
                self.service._send_ctrl_c_to_active_window.assert_called_once()

    def test_f8_auto_retry_expires_without_sending_a_third_copy(self):
        self.start_physical_f8()
        for _ in range(2):
            self.assertIsNotNone(self.service._pending)
            self.service._pending["deadline"] = 0
            self.service._wait_for_clipboard_change()
            self.service._send_pending_copy()
        self.assertEqual(self.delivered(), [("F8", None, "clipboard_timeout")])
        self.assertEqual(self.results[0][1]["capture_attempt"], 2)
        self.assertEqual(self.service._send_ctrl_c_to_active_window.call_count, 2)

    def test_f8_rechecks_other_keys_before_dispatching_auto_retry(self):
        pressed, suppressed = self.start_physical_f8()
        self.service._pending["deadline"] = 0
        self.service._wait_for_clipboard_change()
        self.service._dispatch_keyboard(
            0x28, is_down=False, flags=0, pressed=pressed, suppressed=suppressed, from_hook=True,
        )
        self.service._send_pending_copy()
        self.assertEqual(self.delivered(), [("F8", None, "selection_may_have_changed")])
        self.service._send_ctrl_c_to_active_window.assert_called_once_with()

    def test_f8_first_response_prevents_auto_retry(self):
        self.start_physical_f8()
        self.snapshot_update("Первый ответ")
        self.service._wait_for_clipboard_change()
        self.assertEqual(self.delivered(), [("F8", "Первый ответ", None)])
        self.service._send_ctrl_c_to_active_window.assert_called_once_with()

    def repeat_f8_during_no_response(self):
        self.register("F8")
        self.request(0x77)
        self.service._send_pending_copy()
        keys = set(), set()
        for down in (True, False):
            self.assertTrue(self.service._dispatch_keyboard(
                0x77, is_down=down, flags=0, pressed=keys[0], suppressed=keys[1], from_hook=True,
            ))

    def test_repeated_f8_is_kept_until_no_response_deadline(self):
        self.repeat_f8_during_no_response()
        self.service._send_ctrl_c_to_active_window.assert_called_once()
        self.service._pending["deadline"] = 0
        self.service._wait_for_clipboard_change()
        self.service._send_pending_copy()
        self.snapshot_update("Текст после повторного F8")
        self.assertEqual(self.delivered(), [("F8", "Текст после повторного F8", None)])
        self.assertEqual(self.results[0][1]["capture_retry_of"], 1)
        self.assertEqual(self.service._send_ctrl_c_to_active_window.call_count, 2)

    def test_repeated_f8_is_cancelled_after_other_input(self):
        self.repeat_f8_during_no_response()
        self.service._observe_copy_mouse_message(0x020A)
        self.service._pending["deadline"] = 0
        self.service._wait_for_clipboard_change()
        self.service._send_pending_copy()
        self.assertEqual(self.delivered(), [("F8", None, "clipboard_timeout")])
        self.assertEqual(self.results[0][1]["capture_retry_skipped"], "mouse_wheel")
        self.assertEqual(self.results[0][1]["capture_repeat_id"], 2)
        self.assertEqual(self.results[0][1]["clipboard_sequence_latest"], 10)
        self.assertIs(self.results[0][1]["clipboard_sequence_changed"], False)
        self.service._send_ctrl_c_to_active_window.assert_called_once()

    def test_repeated_f8_is_cancelled_if_source_changes_after_promotion(self):
        self.repeat_f8_during_no_response()
        self.service._pending["deadline"] = 0
        self.service._wait_for_clipboard_change()
        self.hwnd += 1
        self.service._send_pending_copy()
        self.assertEqual(self.delivered(), [("F8", None, "source_window_changed")])
        self.service._send_ctrl_c_to_active_window.assert_called_once()

    def test_repeated_f8_needs_no_replay_after_original_response(self):
        self.repeat_f8_during_no_response()
        self.snapshot_update("Первый запрос выполнился")
        self.service._send_pending_copy()
        self.assertEqual(self.delivered(), [("F8", "Первый запрос выполнился", None)])
        self.service._send_ctrl_c_to_active_window.assert_called_once()

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
