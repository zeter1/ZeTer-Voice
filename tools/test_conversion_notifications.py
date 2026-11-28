from __future__ import annotations

import json
import queue
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# Import only synthetic source; storage is resolved from __file__, not cwd.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
MIRROR = tempfile.TemporaryDirectory(prefix="tts уведомления тест ", dir=SOURCE_ROOT)
ROOT = Path(MIRROR.name).resolve()
(ROOT / "tts_app").mkdir()
for source in (SOURCE_ROOT / "tts_app").glob("*.py"):
    shutil.copyfile(source, ROOT / "tts_app" / source.name)
sys.path.insert(0, str(ROOT))
from tts_app import runtime
from tts_app.app_event_dispatch import AppEventDispatchMixin
from tts_app.app_hotkeys import AppHotkeysMixin
from tts_app.conversion_notifications import ConversionNotifications, reveal_audio_file
from tts_app.problem_logger import ProblemLogger

assert runtime.STORAGE_BASE_DIR.resolve() == ROOT


class CaptureFailureTests(unittest.TestCase):
    def setUp(self):
        self.app = AppHotkeysMixin()
        self.task = types.SimpleNamespace(
            task_id="task", workspace_id="tab", status_var=Mock(),
            preview_thread=None, preview_is_paused=False,
            auto_read_hotkey_var=Mock(), append_external_selected_text=Mock(return_value=True),
        )
        self.app.tab_hotkey_monitors = {
            "tab": types.SimpleNamespace(enabled=True, get_hotkey=lambda: "F8"),
        }
        self.app._task_by_workspace_id = lambda workspace_id: self.task
        self.app.shutdown_in_progress = False
        self.app.logger = ProblemLogger()
        self.app.completion_notifications = Mock()

    def test_clipboard_timeout_popup_and_problem_logs_agree(self):
        self.app._on_tab_copy_hotkey("tab", {
            "capture_id": 7, "capture_rejected": "clipboard_timeout",
            "capture_stage": "clipboard_open_busy", "captured_text": "PRIVATE_FIXTURE_TEXT",
            "source_process": "test-editor.exe", "source_ping": "ok",
            "source_foreground": True, "source_focus_class": "Edit",
        })
        self.app.completion_notifications.show_capture_failure.assert_called_once()
        hotkey, message = self.app.completion_notifications.show_capture_failure.call_args.args[:2]
        self.assertEqual(hotkey, "F8")
        self.assertEqual(self.app.completion_notifications.show_capture_failure.call_args.kwargs["source_process"],
                         "test-editor.exe")
        self.assertIn("открыть буфер", message)
        self.task.status_var.set.assert_called_once_with(message)
        self.task.append_external_selected_text.assert_not_called()
        self.app.logger.close()
        for file in (self.app.logger.events_file, self.app.logger.ai_read_first_file):
            raw = file.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_FIXTURE_TEXT", raw)
            if file == self.app.logger.events_file:
                records = [json.loads(line) for line in raw.splitlines()]
            else:
                records = json.loads(raw)["recent_signals"]
            record = next(item for item in records if item["event"] == "global_copy_capture_rejected")
            self.assertEqual(record["reason"], "clipboard_timeout")
            self.assertEqual(record["capture_id"], 7)
            self.assertEqual(record["capture_stage"], "clipboard_open_busy")
            self.assertEqual(record["reason_detail"], message)
            self.assertEqual(record["source_ping"], "ok")
            self.assertIs(record["source_foreground"], True)
            self.assertEqual(record["source_focus_class"], "Edit")
            self.assertIn("clipboard_open_busy", record["suggested_check"])
            self.assertIn("менеджер буфера", record["suggested_fix"])
            self.assertEqual(record["owner_module"], "tts_app/windows_hotkey_capture.py")

    def test_timeout_reports_unresponsive_source_window(self):
        self.app.logger = Mock()
        self.app._on_tab_copy_hotkey("tab", {
            "capture_id": 9, "capture_rejected": "clipboard_timeout",
            "capture_stage": "waiting_clipboard", "source_ping": "timeout",
            "source_foreground": True, "source_focus_class": "Qt5152QWindowIcon",
        })
        record = self.app.logger.event.call_args.kwargs
        self.assertIn("не отвечает", record["reason_detail"])
        self.assertIn("source_ping", record["suggested_check"])
        self.assertEqual(record["source_focus_class"], "Qt5152QWindowIcon")
        message = self.app.completion_notifications.show_capture_failure.call_args.args[1]
        self.assertEqual(message, record["reason_detail"])
        self.task.append_external_selected_text.assert_not_called()

    def test_timeout_reports_answered_source_without_selection(self):
        self.app.logger = Mock()
        self.app._on_tab_copy_hotkey("tab", {
            "capture_id": 10, "capture_rejected": "clipboard_timeout",
            "capture_stage": "waiting_clipboard", "source_ping": "ok",
            "source_foreground": True, "source_focus_class": "",
        })
        record = self.app.logger.event.call_args.kwargs
        self.assertIn("не был выделен", record["reason_detail"])
        self.assertIn("source_foreground", record["suggested_check"])
        self.assertEqual(record["capture_id"], 10)

    def test_timeout_reports_source_that_lost_the_foreground(self):
        self.app.logger = Mock()
        self.app._on_tab_copy_hotkey("tab", {
            "capture_id": 11, "capture_rejected": "clipboard_timeout",
            "capture_stage": "waiting_clipboard", "source_ping": "ok",
            "source_foreground": False,
        })
        record = self.app.logger.event.call_args.kwargs
        self.assertIn("перестало быть активным", record["reason_detail"])

    def test_fix_depends_on_the_observed_stage(self):
        self.app.logger = Mock()
        for reason, stage, expected in (
            ("clipboard_timeout", "waiting_clipboard", "Ctrl+C"),
            ("clipboard_timeout", "reading_unicode", "короткий фрагмент"),
            ("clipboard_send_failed", "send_input", "от администратора"),
        ):
            with self.subTest(reason=reason, stage=stage):
                self.app._on_tab_copy_hotkey("tab", {"capture_rejected": reason, "capture_stage": stage})
                record = self.app.logger.event.call_args.kwargs
                self.assertIn(expected, record["suggested_fix"])

    def test_empty_clipboard_details_preserve_the_observed_format(self):
        self.app.logger = Mock()
        for available, expected in ((False, "не оказалось Unicode-текста"), (True, "пустым")):
            self.app._on_tab_copy_hotkey("tab", {
                "capture_rejected": "clipboard_empty", "clipboard_unicode_available": available,
            })
            self.assertIn(expected, self.app.logger.event.call_args.kwargs["reason_detail"])

    def test_editor_refusal_allows_retry_and_duplicate_reports_no_insertion(self):
        self.app.logger = Mock()
        self.task.append_external_selected_text.side_effect = (False, True)
        snapshot = {
            "capture_stage": "snapshot_ready", "captured_text": "synthetic",
            "clipboard_sequence_after": 42,
        }
        for _ in range(3):
            self.app._on_tab_copy_hotkey("tab", snapshot)
        self.assertEqual(self.task.append_external_selected_text.call_count, 2)
        first, second = self.app.completion_notifications.show_capture_failure.call_args_list
        self.assertIn("вкладка", first.args[1].lower())
        self.assertIn("уже добавлен", second.args[1].lower())
        self.assertEqual([call.args[0] for call in self.app.logger.event.call_args_list], [
            "global_copy_capture_rejected", "global_copy_text_captured",
            "global_copy_duplicate_skipped", "global_copy_capture_rejected",
        ])
        self.assertEqual(self.app.logger.event.call_args.kwargs["reason"], "duplicate_snapshot")

    def test_popup_error_keeps_original_capture_failure(self):
        logger = self.app.logger = Mock()
        self.app.completion_notifications.show_capture_failure.side_effect = OSError("synthetic")
        self.app._on_tab_copy_hotkey("tab", {
            "capture_id": 8, "capture_rejected": "clipboard_owner_changed",
        })
        self.task.status_var.set.assert_called_once()
        rejected, notification = logger.event.call_args_list
        self.assertEqual(rejected.args[0], "global_copy_capture_rejected")
        self.assertEqual(rejected.kwargs["capture_id"], 8)
        self.assertEqual(notification.args[0], "global_copy_notification_failed")
        self.assertEqual(notification.kwargs["capture_id"], 8)


class CompletionDispatchTests(unittest.TestCase):
    def test_completed_file_uses_persistent_notification_without_modal_dialog(self):
        output = ROOT / "Аудио с пробелом" / "Готовый файл.mp3"
        task = types.SimpleNamespace(
            current_mp3_text_backup_path=None,
            remove_completed_mp3_source=Mock(return_value={"removed": True}),
            set_job_idle=Mock(), status_var=Mock(),
        )
        app = types.SimpleNamespace(
            events=queue.Queue(), tabs={1: task},
            root=types.SimpleNamespace(after=Mock()),
            completion_notifications=Mock(), shutdown_in_progress=False, settings={},
            _process_events=Mock(),
        )
        app.events.put((1, "done", {"output": str(output), "run_id": "run-1"}))
        with patch("tts_app.app_event_dispatch.messagebox.showinfo") as modal:
            AppEventDispatchMixin._process_events(app)
        modal.assert_not_called()
        app.completion_notifications.show.assert_called_once()
        self.assertEqual(app.completion_notifications.show.call_args.args[0], output)
        task.remove_completed_mp3_source.assert_called_once_with("run-1")
        task.set_job_idle.assert_called_once()

    def test_quiet_completion_keeps_status_and_source_handling_without_a_popup(self):
        output = ROOT / "готово.mp3"
        task = types.SimpleNamespace(
            current_mp3_text_backup_path=None,
            remove_completed_mp3_source=Mock(return_value={"removed": True}),
            set_job_idle=Mock(), status_var=Mock(),
        )
        app = types.SimpleNamespace(
            events=queue.Queue(), tabs={1: task}, root=types.SimpleNamespace(after=Mock()),
            completion_notifications=Mock(), shutdown_in_progress=False,
            settings={"quiet_mp3_notifications": True}, _process_events=Mock(),
        )
        app.events.put((1, "done", {"output": str(output), "run_id": "quiet-1"}))
        AppEventDispatchMixin._process_events(app)
        app.completion_notifications.show.assert_not_called()
        self.assertIn(str(output), task.status_var.set.call_args.args[0])
        task.remove_completed_mp3_source.assert_called_once_with("quiet-1")
        task.set_job_idle.assert_called_once()

    def test_shutting_down_does_not_create_a_new_window(self):
        task = types.SimpleNamespace(
            current_mp3_text_backup_path=None,
            remove_completed_mp3_source=Mock(return_value={}),
            set_job_idle=Mock(), status_var=Mock(),
        )
        app = types.SimpleNamespace(
            events=queue.Queue(), tabs={1: task},
            root=types.SimpleNamespace(after=Mock()),
            completion_notifications=Mock(), shutdown_in_progress=True, settings={},
            _process_events=Mock(),
        )
        app.events.put((1, "done", {"output": str(ROOT / "done.mp3"), "run_id": "run-1"}))
        AppEventDispatchMixin._process_events(app)
        app.completion_notifications.show.assert_not_called()
        task.set_job_idle.assert_called_once()

    def test_reveal_selects_exact_unicode_file_without_shell(self):
        output = ROOT / "Аудио с пробелом" / "Готовый файл, 1.mp3"
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(b"synthetic output")
        with patch("tts_app.conversion_notifications.os.name", "nt"), \
             patch("tts_app.conversion_notifications.subprocess.Popen") as process:
            reveal_audio_file(output)
        process.assert_called_once_with(["explorer.exe", "/select,", str(output)])
        self.assertEqual(output.read_bytes(), b"synthetic output")

    def test_removed_file_opens_existing_folder_without_recreating_anything(self):
        output = ROOT / "Существующая папка" / "Удалённый файл.mp3"
        output.parent.mkdir(exist_ok=True)
        with patch("tts_app.conversion_notifications.os.name", "nt"), \
             patch("tts_app.conversion_notifications.os.startfile", create=True) as start:
            reveal_audio_file(output)
        start.assert_called_once_with(str(output.parent))
        missing = ROOT / "Исчезнувший диск" / "file.mp3"
        with self.assertRaises(FileNotFoundError):
            reveal_audio_file(missing)
        self.assertFalse(missing.parent.exists())


@unittest.skipUnless("--gui" in sys.argv, "real Tk smoke: run with --gui")
class NotificationTkTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tts_app.workspace_theme import apply_workspace_theme

        self.root = tk.Tk()
        self.root.withdraw()
        apply_workspace_theme(self.root)
        self.manager = ConversionNotifications(self.root)
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def buttons(self, parent):
        from tkinter import ttk
        result = []
        for child in parent.winfo_children():
            if isinstance(child, ttk.Button):
                result.append(child)
            result.extend(self.buttons(child))
        return result

    def test_burst_open_click_and_cross_keep_other_notifications(self):
        from tts_app.conversion_notifications import notification_work_area
        files = [ROOT / f"Готовая задача {index}.mp3" for index in range(4)]
        for output in files:
            self.manager.show(output, "Тестовое уведомление.")
        self.root.update()
        self.assertEqual(len(self.manager.windows), 4)
        self.assertFalse(self.root.tk.call("after", "info"))
        left, top, right, bottom = notification_work_area(self.root)
        rectangles = []
        for window in self.manager.windows:
            self.assertTrue(window.winfo_viewable())
            self.assertTrue(window.attributes("-topmost"))
            self.assertTrue(window.overrideredirect())
            x, y, w, h = window.winfo_rootx(), window.winfo_rooty(), window.winfo_width(), window.winfo_height()
            self.assertGreaterEqual(x, left)
            self.assertGreaterEqual(y, top)
            self.assertLessEqual(x + w, right)
            self.assertLessEqual(y + h, bottom)
            rectangles.append((x, y, x + w, y + h))
        for index, a in enumerate(rectangles):
            for b in rectangles[index + 1:]:
                self.assertTrue(a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

        first = self.manager.windows[0]
        with patch("tts_app.conversion_notifications.reveal_audio_file") as reveal:
            next(b for b in self.buttons(first) if b["text"] == "Показать файл").invoke()
        reveal.assert_called_once_with(files[0])
        self.assertEqual(len(self.manager.windows), 4)
        with patch("tts_app.conversion_notifications.reveal_audio_file", side_effect=FileNotFoundError("missing")), \
             patch("tts_app.conversion_notifications.messagebox.showerror") as error:
            next(b for b in self.buttons(first) if b["text"] == "Показать файл").invoke()
        error.assert_called_once()
        self.assertTrue(first.winfo_exists())
        self.root.tk.call(first.protocol("WM_DELETE_WINDOW"))
        self.assertTrue(first.winfo_exists())
        next(b for b in self.buttons(first) if b["text"] == "×").invoke()
        self.root.update()
        self.assertFalse(first.winfo_exists())
        self.assertEqual(len(self.manager.windows), 3)
        for window in list(self.manager.windows):
            self.manager.dismiss(window)
        self.assertEqual(self.manager.windows, [])
        self.manager.show(files[0])
        self.root.update()
        self.assertTrue(self.manager.windows[0].winfo_viewable())

    def test_long_display_path_keeps_file_button_inside_work_area(self):
        from tts_app.conversion_notifications import notification_work_area
        output = ROOT.joinpath(*(["Длинная папка с пробелом" * 5] * 12), "Имя файла " * 20 + ".mp3")
        self.manager.show(output, "Конвертированный текст удалён. Бэкап текста хранится 45 дней.")
        self.root.update()
        window = self.manager.windows[0]
        button = next(b for b in self.buttons(window) if b["text"] == "Показать файл")
        left, top, right, bottom = notification_work_area(self.root)
        self.assertGreaterEqual(window.winfo_rooty(), top)
        self.assertLessEqual(window.winfo_rooty() + window.winfo_height(), bottom)
        self.assertGreater(button.winfo_width(), 40)
        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), bottom)
        self.assertTrue(button.winfo_viewable())
    def test_minimize_and_shutdown_with_notifications(self):
        self.root.deiconify()
        self.root.update()
        self.manager.show(ROOT / "После сворачивания.mp3")
        self.root.iconify()
        self.root.update()
        self.assertTrue(self.manager.windows[0].winfo_viewable())
        self.manager.show(ROOT / "Завершилась, пока главное окно свёрнуто.mp3")
        self.root.update()
        self.assertTrue(self.manager.windows[1].winfo_viewable())
        self.root.deiconify()
        self.root.update()
        self.manager.show(ROOT / "Вторая задача.mp3")
        self.assertEqual(len(self.manager.windows), 3)
        # tearDown destroys the root with both independent windows still open.

    def test_worker_cannot_mutate_tk(self):
        errors = []
        def worker():
            try:
                self.manager.show(ROOT / "worker.mp3")
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.manager.windows, [])

    def test_capture_warning_stays_open_until_cross_during_mixed_outcomes(self):
        from tts_app.conversion_notifications import notification_work_area

        self.root.deiconify()
        self.root.update()
        self.root.iconify()
        self.root.update()
        self.manager.show(ROOT / "Готово.mp3")
        audio = self.manager.windows[0]
        task = types.SimpleNamespace(
            task_id="task", workspace_id="tab", status_var=Mock(),
            preview_thread=None, preview_is_paused=False, auto_read_hotkey_var=Mock(),
            append_external_selected_text=Mock(return_value=True),
        )
        monitor = types.SimpleNamespace(enabled=True, get_hotkey=lambda: "F8")
        app = AppHotkeysMixin()
        app.root, app.completion_notifications = self.root, self.manager
        app.shutdown_in_progress, app.logger = False, Mock()
        app._task_by_workspace_id = {"tab": task}.get
        app.tab_hotkey_monitors = {"tab": monitor}
        app._on_tab_copy_hotkey("tab", {
            "capture_id": 1, "capture_rejected": "clipboard_timeout", "source_process": "editor.exe",
        })
        warning = self.manager.windows[-1]
        app._on_tab_copy_hotkey("tab", {
            "capture_id": 2, "captured_text": "Fixture", "clipboard_sequence_after": 77,
            "source_process": "browser.exe",
        })
        self.assertTrue(warning.winfo_viewable())
        task.append_external_selected_text.assert_called_once_with("Fixture")

        monitor.get_hotkey = lambda: "Ctrl+C"
        app._on_tab_copy_hotkey("tab", {
            "capture_id": 3, "capture_rejected": "clipboard_read_failed", "source_process": "browser.exe",
        })
        self.root.update()
        self.assertEqual(self.manager.windows, [audio, warning])
        self.assertTrue(warning.winfo_viewable())
        self.assertTrue(warning.attributes("-topmost"))
        self.assertFalse(self.root.tk.call("after", "info"))
        self.assertTrue(self.root.state() == "iconic" or not self.root.winfo_viewable())
        _, _, right, bottom = notification_work_area(self.root)
        self.assertEqual(warning.winfo_rootx() + warning.winfo_width(), right - 12)
        self.assertEqual(warning.winfo_rooty() + warning.winfo_height(), bottom - 12)
        self.assertLess(audio.winfo_rooty() + audio.winfo_height(), warning.winfo_rooty())
        self.assertIn("Ctrl+C", warning.title())
        card = self.manager._capture_card
        self.assertEqual(card.heading["text"], "Текст не скопирован")
        self.assertEqual(card.hotkey["text"], "Ctrl+C")
        self.assertEqual(card.count["text"], "Сбоев: 2")
        self.assertEqual(card.source["text"], "browser.exe")
        self.assertIn("Не удалось прочитать", card.reason["text"])
        self.assertTrue(card.advice.winfo_viewable())
        next(button for button in self.buttons(warning) if button["text"] == "×").invoke()
        self.assertEqual(self.manager.windows, [audio])
        self.assertFalse(warning.winfo_exists())
        self.manager.show_capture_failure("Ctrl+C", "Проверьте выделение.")
        self.assertEqual(self.manager._capture_card.count["text"], "Сбоев: 1")

    def test_two_completion_events_create_two_independent_windows_on_tk_thread(self):
        def task():
            return types.SimpleNamespace(
                current_mp3_text_backup_path=None,
                remove_completed_mp3_source=Mock(return_value={"removed": True}),
                set_job_idle=Mock(), status_var=Mock(),
            )
        app = types.SimpleNamespace(
            root=self.root, events=queue.Queue(), tabs={1: task(), 2: task()},
            shutdown_in_progress=False, settings={}, _process_events=Mock(),
        )
        for index in (1, 2):
            app.events.put((index, "done", {"output": str(ROOT / f"Задача {index}.mp3"), "run_id": f"run-{index}"}))
        with patch("tts_app.app_event_dispatch.messagebox.showinfo") as modal:
            AppEventDispatchMixin._process_events(app)
        self.root.update()
        modal.assert_not_called()
        self.assertEqual(len(app.completion_notifications.windows), 2)
        self.assertEqual(app.completion_notifications.thread_id, threading.get_ident())
        self.assertIsNot(app.completion_notifications.windows[0], app.completion_notifications.windows[1])


if __name__ == "__main__":
    try:
        if "--gui" in sys.argv:
            sys.argv.remove("--gui")
        unittest.main()
    finally:
        MIRROR.cleanup()
