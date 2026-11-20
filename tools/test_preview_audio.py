from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import types
import unittest
import queue
import weakref
from pathlib import Path
from unittest.mock import Mock, patch

# __file__, not cwd, owns storage. Import only a source mirror: no user state.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
MIRROR = tempfile.TemporaryDirectory(prefix="tts preview тест ", dir=SOURCE_ROOT)
ROOT = Path(MIRROR.name).resolve()
(ROOT / "tts_app").mkdir()
for source in (SOURCE_ROOT / "tts_app").glob("*.py"):
    shutil.copyfile(source, ROOT / "tts_app" / source.name)
sys.path.insert(0, str(ROOT))
from tts_app import runtime
from tts_app.sapi_preview import preview_sapi_text
from tts_app.task_preview_player import TaskPreviewPlayerMixin
from tts_app.app_hotkeys import AppHotkeysMixin
from tts_app.sapi_preview_diagnostics import preview_diagnostic, PreviewPlaybackError
assert runtime.STORAGE_BASE_DIR.resolve() == ROOT


class FakeVoice:
    def __init__(self, mode="failed"):
        self.mode = mode
        self.step = 0
        self.sink = None
        self.closed = False
        self.spoken_count = 0
        self.AudioOutputStream = types.SimpleNamespace(Status=types.SimpleNamespace(
            CurrentDevicePosition=0, CurrentSeekPosition=0, State=0,
        ))

    @property
    def Status(self):
        failed = self.mode == "failed"
        return types.SimpleNamespace(
            CurrentStreamNumber=0 if failed else 1,
            LastStreamNumberQueued=1, RunningState=0 if failed else 2,
            LastHResult=-2147201015 if self.mode == "no_driver" else 0,
            InputWordPosition=0 if failed else self.step * 30,
            InputWordLength=0 if failed else 5, InputSentencePosition=0,
        )

    def Speak(self, text, flags):
        if not text:
            self.closed = True
            if self.mode == "cleanup_failure":
                raise RuntimeError("synthetic cleanup failure")
            return 2
        self.spoken_count += 1
        if self.sink and self.mode != "failed":
            self.sink.OnStartStream(1, 0)
        return 1

    def WaitUntilDone(self, timeout):
        if self.closed and self.mode == "cleanup_timeout":
            return False
        if self.closed or self.mode == "failed":
            return True
        self.step += 1
        if self.mode != "second_failed" or self.spoken_count == 1:
            self.AudioOutputStream.Status.CurrentDevicePosition = self.step * 100
        if self.sink:
            self.sink.OnWord(1, self.step * 100, 0, 5)
        if self.mode == "partial":
            if self.step == 1:
                return False
            raise RuntimeError("synthetic output failure")
        if self.sink:
            self.sink.OnEndStream(1, 200)
        return True

    def Resume(self):
        pass

    def Pause(self):
        pass


class PreviewTests(unittest.TestCase):
    def run_preview(self, voice, stop=None, choose_voice=None, watched_tokens=(), diagnostic=None):
        done = []
        teardown_observations = []
        pythoncom = types.ModuleType("pythoncom")
        pythoncom.CoInitialize = lambda: None
        def uninitialize():
            owner_frame = sys._getframe(1)
            teardown_observations.append((
                owner_frame.f_locals["voice"] is not None,
                owner_frame.f_locals["status"] is not None,
                any(token() is not None for token in watched_tokens),
            ))
        pythoncom.CoUninitialize = uninitialize
        pythoncom.PumpWaitingMessages = lambda: None
        client = types.ModuleType("win32com.client")
        client.Dispatch = lambda name: voice
        def with_events(obj, cls):
            obj.sink = cls()
            obj.sink.close = lambda: None
            return obj.sink
        client.WithEvents = with_events
        dynamic = types.ModuleType("win32com.client.dynamic")
        dynamic.Dispatch = lambda obj: obj
        client.dynamic = dynamic
        win32com = types.ModuleType("win32com")
        win32com.client = client
        modules = {"pythoncom": pythoncom, "win32com": win32com, "win32com.client": client,
                   "win32com.client.dynamic": dynamic}
        with patch.dict(sys.modules, modules), patch("tts_app.sapi_preview.set_voice_by_description", side_effect=choose_voice), patch(
            "tts_app.sapi_preview.set_audio_output_by_description", return_value="Fake JBL"
        ):
            try:
                result = preview_sapi_text(
                    "Первое предложение. Второе предложение.", "Fake Voice", "Fake JBL",
                    0, 0, 100, stop or threading.Event(), threading.Event(),
                    segment_done_callback=done.append,
                    diagnostic_callback=diagnostic,
                )
                error = None
            except Exception as exc:
                result, error = None, exc
        # Assertions live OUTSIDE the product boundary that preserves a primary
        # failure. A fake teardown assertion inside it could be swallowed.
        self.assertEqual(teardown_observations, [(False, False, False)], "COM reference survives teardown")
        return result, done, error

    def test_failed_async_does_not_acknowledge_or_delete_text(self):
        result, done, error = self.run_preview(FakeVoice())
        self.assertEqual(done, [], "failed async output falsely acknowledges read text")
        self.assertIsNone(result)
        self.assertIsNotNone(error)

    def test_healthy_stream_acknowledges_all_sentences(self):
        result, done, error = self.run_preview(FakeVoice("healthy"))
        self.assertIsNone(error)
        self.assertEqual([item["index"] for item in done], [1, 2])
        self.assertEqual(result["completed_chunks"], 2)

    def test_partial_failure_does_not_acknowledge_unfinished_stream(self):
        result, done, error = self.run_preview(FakeVoice("partial"))
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(done, [])

    def test_stop_before_stream_preserves_text(self):
        stop = threading.Event()
        stop.set()
        result, done, error = self.run_preview(FakeVoice("healthy"), stop)
        self.assertIsNone(error)
        self.assertTrue(result["stopped"])
        self.assertEqual(done, [])

    def test_unverified_ack_cannot_reach_ui_delete_boundary(self):
        tab = types.SimpleNamespace(
            session_read_segments=0, session_read_chars=0,
            current_preview_run_id="current",
            schedule_queue_status_refresh=lambda: None,
            delete_read_text_var=types.SimpleNamespace(get=lambda: True),
            _delete_pending_read_segment=lambda: self.fail("unverified text deleted"),
        )
        TaskPreviewPlayerMixin.handle_preview_segment_done(tab, {"text": "Unread", "preview_run_id": "current"})
        self.assertEqual(tab.session_read_segments, 0)

    def test_stale_ack_cannot_increment_current_read_counters(self):
        tab = types.SimpleNamespace(
            session_read_segments=0, session_read_chars=0,
            current_preview_run_id="current",
            schedule_queue_status_refresh=lambda: None,
            delete_read_text_var=types.SimpleNamespace(get=lambda: False),
        )
        TaskPreviewPlayerMixin.handle_preview_segment_done(tab, {
            "text": "Old", "preview_run_id": "old", "playback_verified": True,
        })
        self.assertEqual(tab.session_read_segments, 0)

    def test_reused_stream_id_does_not_verify_second_block_without_audio_delta(self):
        with patch("tts_app.sapi_preview.PREVIEW_SAPI_BLOCK_MAX_CHARS", 25):
            result, done, error = self.run_preview(FakeVoice("second_failed"))
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual([item["index"] for item in done], [1])

    def test_short_stream_finishing_between_polls_is_verified(self):
        result, done, error = self.run_preview(FakeVoice("healthy"))
        self.assertIsNone(error)
        self.assertTrue(all(item["playback_verified"] for item in done))
        tab = types.SimpleNamespace(
            session_read_segments=0, session_read_chars=0, current_preview_run_id="current",
            schedule_queue_status_refresh=lambda: None,
            delete_read_text_var=types.SimpleNamespace(get=lambda: False),
        )
        for item in done:
            TaskPreviewPlayerMixin.handle_preview_segment_done(tab, {**item, "preview_run_id": "current"})
        self.assertEqual(tab.session_read_segments, 2)
        self.assertEqual(tab.session_read_chars, 38)

    def test_completed_stream_does_not_purge_idle_audio_endpoint(self):
        voice = FakeVoice("cleanup_timeout")
        result, done, error = self.run_preview(voice)
        self.assertIsNone(error)
        self.assertEqual(len(done), 2)
        self.assertEqual(result["completed_chunks"], 2)
        self.assertFalse(voice.closed, "idle SAPI endpoint was unnecessarily purged")

    def test_cancel_still_purges_and_reports_cleanup_failure(self):
        stop = threading.Event()
        stop.set()
        result, done, error = self.run_preview(FakeVoice("cleanup_failure"), stop)
        self.assertIsNone(result)
        self.assertEqual(done, [])
        self.assertEqual(str(error), "synthetic cleanup failure")

    def test_cancel_cleanup_timeout_is_not_hidden(self):
        stop = threading.Event()
        stop.set()
        result, done, error = self.run_preview(FakeVoice("cleanup_timeout"), stop)
        self.assertIsNone(result)
        self.assertEqual(done, [])
        self.assertIsInstance(error, TimeoutError)

    def test_sapi_no_driver_error_keeps_unread_text(self):
        result, done, error = self.run_preview(FakeVoice("no_driver"))
        self.assertIsNone(result)
        self.assertEqual(done, [], "broken audio driver must not acknowledge spoken text")
        self.assertIsInstance(error, PreviewPlaybackError)
        self.assertIn("0x80045009", str(error))
        self.assertIn("аудиодрайвер", str(error))

    def test_helper_com_token_released_before_uninitialize(self):
        watched = []
        class Token:
            pass
        def fail_selection(voice, description):
            token = Token()
            watched.append(weakref.ref(token))
            raise RuntimeError("synthetic selection failure")
        result, done, error = self.run_preview(FakeVoice(), choose_voice=fail_selection, watched_tokens=watched)
        self.assertEqual(str(error), "synthetic selection failure")
        self.assertIn("fail_selection", error.preview_traceback_text)
        self.assertEqual(done, [])

    def test_lifetime_oracle_detects_traceback_release_disabled(self):
        watched = []
        class Token:
            pass
        def fail_selection(voice, description):
            token = Token()
            watched.append(weakref.ref(token))
            raise RuntimeError("synthetic selection failure")
        with patch("tts_app.sapi_preview.release_preview_exception_frames", return_value=""), self.assertRaises(
            AssertionError, msg="negative control did not detect leaked traceback token"
        ):
            self.run_preview(FakeVoice(), choose_voice=fail_selection, watched_tokens=watched)

    def test_callback_failure_without_console_cannot_replace_primary_error(self):
        def bad_callback(info):
            raise RuntimeError("synthetic diagnostic failure")
        with patch.object(sys, "stderr", None), patch.object(sys, "stdout", None):
            result, done, error = self.run_preview(FakeVoice(), diagnostic=bad_callback)
        self.assertIsInstance(error, PreviewPlaybackError)
        self.assertEqual(done, [])

    def test_worker_queue_exception_has_text_trace_but_no_live_frames(self):
        variable = types.SimpleNamespace(get=lambda: "Fake", set=lambda value: None)
        number = types.SimpleNamespace(get=lambda: 0)
        tab = types.SimpleNamespace(
            preview_thread=None, voice_var=variable, audio_output_var=variable,
            rate_var=number, pitch_var=number, volume_var=number,
            preview_stop_event=threading.Event(), preview_pause_event=threading.Event(),
            preview_base_mark="base", preview_snapshot_end_mark="end",
            text_box=types.SimpleNamespace(mark_set=lambda *args: None, mark_gravity=lambda *args: None),
            _text_offset_from_index=lambda index: 0, _full_text=lambda: "Unread",
            clear_saved_preview_bookmark=lambda **kwargs: None,
            clear_preview_highlight=lambda: None, log_text_state=lambda *args, **kwargs: None,
            _set_playback_controls=lambda mode: None, status_var=variable,
            task_id="test-task", workspace_id="test-tab",
            app=types.SimpleNamespace(stop_other_previews=lambda task: None, events=queue.Queue(),
                                      logger=types.SimpleNamespace(event=lambda *args, **kwargs: None)),
        )
        def failed_preview(**kwargs):
            try:
                raise ValueError("original selection evidence")
            except ValueError as cause:
                raise RuntimeError("primary speech failure") from cause
        def inline_thread(*, target, **kwargs):
            return types.SimpleNamespace(start=target)
        with patch("tts_app.task_preview_player.preview_sapi_text", side_effect=failed_preview), patch(
            "tts_app.task_preview_player.threading.Thread", side_effect=inline_thread
        ):
            TaskPreviewPlayerMixin._start_preview_worker(
                tab, raw_text="Unread", preview_base_index="1.0", source_mode="all", selected_text=False,
            )
        task_id, kind, payload = tab.app.events.get_nowait()
        self.assertEqual(kind, "preview_error")
        self.assertIsNone(payload["exc"].__traceback__)
        self.assertIsNone(payload["exc"].__cause__)
        self.assertIsNone(payload["exc"].__context__)
        self.assertIn("original selection evidence", payload["traceback"])


class HotkeyCaptureTests(unittest.TestCase):
    def make_capture(self, *, running=False, paused=False, return_app=False):
        appended, events, statuses = [], [], []
        clipboard = {"text": ""}
        stop_event, pause_event = threading.Event(), threading.Event()
        if paused:
            pause_event.set()

        def append(text):
            appended.append(text)
            return True

        tab = types.SimpleNamespace(
            task_id="capture-task", workspace_id="capture-tab",
            tab_title=lambda: "Capture test",
            status_var=types.SimpleNamespace(set=statuses.append),
            preview_thread=types.SimpleNamespace(is_alive=lambda: running),
            preview_is_paused=paused, preview_stop_event=stop_event,
            preview_pause_event=pause_event, preview_bookmark={"start_offset": 12},
            auto_read_hotkey_var=types.SimpleNamespace(get=lambda: True),
            append_external_selected_text=append,
        )
        monitor = types.SimpleNamespace(
            enabled=True, get_hotkey=lambda: "Ctrl+C",
            _clipboard_sequence_number=lambda: clipboard.get("sequence"),
            remember_text_and_check_duplicate=lambda text: False,
        )
        app = AppHotkeysMixin()
        vars(app).update(
            shutdown_in_progress=False,
            root=types.SimpleNamespace(clipboard_get=lambda: clipboard["text"]),
            _task_by_workspace_id=lambda workspace_id: tab,
            tab_hotkey_monitors={"capture-tab": monitor},
            logger=types.SimpleNamespace(event=lambda name, **fields: events.append((name, fields))),
            completion_notifications=Mock(),
        )
        def capture(text, sequence=None):
            clipboard["text"] = text
            clipboard["sequence"] = sequence
            AppHotkeysMixin._on_tab_copy_hotkey(app, "capture-tab", {
                "clipboard_changed": True, "source_process": "synthetic.exe",
                "copy_latency_ms": 120, "clipboard_sequence_after": sequence,
                "captured_text": text,
            })
        if return_app:
            return capture, tab, appended, events, app
        return capture, tab, appended, events

    def test_punctuation_only_capture_reaches_idle_tab(self):
        for text in (".", ". . .", ", , .", "…"):
            with self.subTest(text=text):
                capture, tab, appended, events = self.make_capture()
                capture(text)
                self.assertEqual(appended, [text])
                self.assertFalse(tab.preview_stop_event.is_set())

    def test_punctuation_during_reading_keeps_next_valid_capture(self):
        capture, tab, appended, events = self.make_capture(running=True)
        capture("Первый текст.")
        capture(". . .")
        capture("Следующий текст, 123.")
        self.assertEqual(appended, ["Первый текст.", ". . .", "Следующий текст, 123."])
        self.assertTrue(tab.preview_thread.is_alive())
        self.assertFalse(tab.preview_stop_event.is_set())
        self.assertFalse(tab.preview_pause_event.is_set())

    def test_punctuation_during_pause_keeps_bookmark_and_next_valid_capture(self):
        capture, tab, appended, events = self.make_capture(running=True, paused=True)
        capture(".")
        capture("Продолжение.")
        self.assertEqual(appended, [".", "Продолжение."])
        self.assertEqual(tab.preview_bookmark, {"start_offset": 12})
        self.assertTrue(tab.preview_is_paused)
        self.assertTrue(tab.preview_pause_event.is_set())
        self.assertFalse(tab.preview_stop_event.is_set())

    def test_words_numbers_and_line_boundaries_are_preserved(self):
        for text in ("Я", "7", "123,45.", "Русский — English текст.\n\n日本語\n🧪",
                     "  English only\r\nsecond line\r\n"):
            with self.subTest(text=text):
                capture, tab, appended, events = self.make_capture()
                capture(text)
                self.assertEqual(appended, [text])

    def test_blank_capture_still_does_not_enter_queue(self):
        capture, tab, appended, events = self.make_capture(running=True)
        capture("  \n\t ")
        self.assertEqual(appended, [])
        self.assertFalse(tab.preview_stop_event.is_set())

    def test_only_the_already_processed_clipboard_sequence_is_skipped(self):
        capture, tab, appended, events, app = self.make_capture(return_app=True)
        app.tab_hotkey_monitors["capture-tab"].remember_text_and_check_duplicate = (
            lambda text: text in appended
        )
        text = "Отдельное повторное выделение."
        capture(text, sequence=100)
        capture(text, sequence=100)
        capture(text, sequence=101)
        self.assertEqual(appended, [text, text])
        self.assertEqual(sum(name == "global_copy_duplicate_skipped" for name, _ in events), 1)

    def test_rejected_append_can_retry_the_same_clipboard_sequence(self):
        capture, tab, appended, events = self.make_capture()
        accept = tab.append_external_selected_text
        tab.append_external_selected_text = lambda text: False
        capture("Retry the same text.", sequence=42)
        self.assertFalse(any(name == "global_copy_text_captured" for name, _ in events))
        tab.append_external_selected_text = accept
        capture("Retry the same text.", sequence=42)
        self.assertEqual(appended, ["Retry the same text."])
        self.assertEqual(sum(name == "global_copy_text_captured" for name, _ in events), 1)

    def test_tk_consumes_snapshot_without_reading_newer_clipboard(self):
        capture, _, appended, _, app = self.make_capture(return_app=True)
        app.root.clipboard_get = lambda: self.fail("Tk must not reread the Windows clipboard")
        capture("Первый текст.", sequence=44)
        capture("Другой текст 🧪", sequence=45)
        self.assertEqual(appended, ["Первый текст.", "Другой текст 🧪"])

    def test_no_snapshot_reports_actionable_failure(self):
        _, tab, appended, events, app = self.make_capture(return_app=True)
        tab.status_var.set = status = Mock()
        AppHotkeysMixin._on_tab_copy_hotkey(app, "capture-tab", {
            "capture_rejected": "clipboard_timeout", "captured_text": None,
            "capture_id": 7, "capture_stage": "clipboard_open_busy",
            "read_attempts": 3, "source_process": "fixture.exe", "source_hwnd": 123,
            "untrusted_clipboard_field": "DO_NOT_LOG_THIS",
        })
        self.assertEqual(appended, [])
        self.assertFalse(any(name == "global_copy_text_captured" for name, _ in events))
        message = status.call_args.args[0].lower()
        self.assertIn("открыть буфер обмена", message)
        self.assertIn("нажмите горячую клавишу ещё раз", message)
        name, evidence = events[-1]
        self.assertEqual(name, "global_copy_capture_rejected")
        self.assertEqual(evidence["capture_id"], 7)
        self.assertEqual(evidence["capture_stage"], "clipboard_open_busy")
        self.assertEqual(evidence["read_attempts"], 3)
        self.assertEqual(evidence["source_process"], "fixture.exe")
        self.assertNotIn("source_hwnd", evidence)
        self.assertNotIn("captured_text", evidence)
        self.assertNotIn("DO_NOT_LOG_THIS", repr(evidence))

    def test_clipboard_conflict_reports_which_check_failed_without_text_or_handles(self):
        _, _, appended, events, app = self.make_capture(return_app=True)
        AppHotkeysMixin._on_tab_copy_hotkey(app, "capture-tab", {
            "capture_rejected": "clipboard_superseded", "captured_text": "PRIVATE_FIXTURE",
            "capture_id": 35, "capture_protocol": 4, "capture_stage": "checking_clipboard",
            "clipboard_conflict": "at_snapshot_open", "clipboard_source_check": "owner_unconfirmed",
            "clipboard_owner_sample": "owner_window_changed", "clipboard_sequence_before_open": 13,
            "clipboard_open_busy_count": 1, "clipboard_busy_opener": "unknown",
            "clipboard_source_owner_hwnd": 123, "clipboard_sequence_observed": 11, "clipboard_sequence_latest": 14,
        })
        self.assertEqual(appended, [])
        name, evidence = events[-1]
        self.assertEqual(name, "global_copy_capture_rejected")
        self.assertIn("изменилось окно-владелец", evidence["reason_detail"])
        self.assertIn("clipboard_source_check", evidence["suggested_check"])
        self.assertEqual(evidence["clipboard_source_check"], "owner_unconfirmed")
        self.assertEqual(evidence["clipboard_owner_sample"], "owner_window_changed")
        self.assertEqual(evidence["clipboard_sequence_before_open"], 13)
        self.assertEqual(evidence["clipboard_busy_opener"], "unknown")
        self.assertEqual(evidence["clipboard_sequence_latest"], 14)
        self.assertNotIn("clipboard_source_owner_hwnd", evidence)
        self.assertNotIn("PRIVATE_FIXTURE", repr(evidence))

if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        MIRROR.cleanup()
