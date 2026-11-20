from __future__ import annotations

from .i18n import tr
from .engine_manager_ui import engine_operation_busy

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *

class AppShutdownMixin:
    def _alive_background_threads(self) -> list[str]:
        alive: list[str] = []
        manager = getattr(self, 'engine_manager', None)
        if manager and manager.thread and manager.thread.is_alive():
            alive.append(manager.thread.name)
        for task in self.tabs.values():
            if task.worker and task.worker.is_alive():
                alive.append(task.worker.name)
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            ):
                alive.append(task.preview_thread.name)
        return alive

    def _finalize_close(self, status: str) -> None:
        if self._workspace_write is not None and not self._workspace_write[0].done():
            self.root.after(100, self._poll_shutdown)
            return
        restarting = getattr(self, "_restart_on_close", False)
        if restarting and status != "closed":
            self.shutdown_in_progress = False
            self._warn_restart_busy()
            return
        if not self.save_workspace():
            self.logger.event("shutdown_workspace_save_failed")
            self.shutdown_in_progress = False
            messagebox.showerror(
                APP_TITLE,
                tr("Не удалось сохранить вкладки. Окно оставлено открытым, "
                "чтобы сохранить текст. Проверьте доступ к папке программы "
                "и попробуйте закрыть окно ещё раз."),
                parent=self.root,
            )
            return
        self.logger.event("workspace_saved_on_close", tabs=len(self.tabs), status=status)

        if not self.save_settings():
            if restarting:
                self.shutdown_in_progress = False
                messagebox.showerror(
                    APP_TITLE,
                    tr("Не удалось сохранить настройки. Перезапуск отменён."),
                    parent=self.root,
                )
                return
            close_without_settings = messagebox.askyesno(
                APP_TITLE,
                tr("Вкладки сохранены, но не удалось записать настройки. "
                "Выберите «Нет», чтобы оставить окно открытым и повторить сохранение.\n\n"
                "Закрыть программу без сохранения настроек?"),
                icon=messagebox.WARNING, default=messagebox.NO, parent=self.root,
            )
            if not close_without_settings:
                self.shutdown_in_progress = False
                return

        if status == "forced_shutdown":
            self.logger.event(
                "forced_shutdown",
                alive_threads=self._alive_background_threads(),
                grace_period_sec=SHUTDOWN_GRACE_SEC,
            )
        manager = getattr(self, "engine_manager", None)
        if manager:
            manager.close()
        cleanup_id = getattr(self, "_text_backup_cleanup_id", None)
        if cleanup_id:
            self.root.after_cancel(cleanup_id)
            self._text_backup_cleanup_id = None

        for workspace_id in list(
            self.tab_hotkey_monitors
        ):
            self._stop_tab_hotkey_monitor(
                workspace_id
            )
        GlobalCopyHotkeyMonitor.stop_shared(self.root)

        self.ui_watchdog.stop()
        self.logger.close(status=status)
        self.exception_hooks.uninstall()
        self.root.destroy()
        self.restart_requested = restarting

    def _poll_shutdown(self) -> None:
        if not self.shutdown_in_progress:
            return

        alive = self._alive_background_threads()
        if not alive:
            self._finalize_close("closed")
            return

        if time.perf_counter() >= self.shutdown_deadline:
            self._finalize_close("forced_shutdown")
            return

        self.root.after(100, self._poll_shutdown)

    def _warn_restart_busy(self) -> None:
        messagebox.showwarning(
            tr("Перезапустить программу"),
            tr("Сначала завершите чтение, создание MP3 или операцию с голосовым движком."),
            parent=self.root,
        )

    def restart_for_language(self) -> None:
        if self.shutdown_in_progress:
            return
        if not self.workspace_text_enabled and any(
            task.text_box.compare("end-1c", ">", "1.0") for task in self.tabs.values()
        ):
            if not messagebox.askyesno(
                tr("Перезапустить программу"),
                tr("Сохранение текста между запусками выключено. Текущий текст вкладок пропадёт при перезапуске. Продолжить?"),
                icon=messagebox.WARNING, default=messagebox.NO, parent=self.root,
            ):
                return
        self.on_close(restart=True)

    def on_close(self, *, restart: bool = False) -> None:
        if self.shutdown_in_progress:
            return

        running = [
            task
            for task in self.tabs.values()
            if task.conversion_running()
        ]

        if restart and (running or engine_operation_busy(self) or self._alive_background_threads()):
            self._warn_restart_busy()
            return

        if running:
            answer = messagebox.askyesno(
                tr("Идёт создание MP3"),
                tr(
                    "Сейчас создаётся MP3-задач: {0}.\n\nЕсли закрыть программу, текущая конвертация будет корректно остановлена. Исходный текст и его бэкап останутся, но текущий MP3 не будет считаться завершённым.\n\nЗакрыть программу и остановить создание MP3?\n\nВыберите «Нет», чтобы продолжить конвертацию.",
                    len(running),
                ),
                icon=messagebox.WARNING,
                default=messagebox.NO,
            )
            if not answer:
                return

        preview_running = [
            task
            for task in self.tabs.values()
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            )
        ]

        self.shutdown_in_progress = True
        self._restart_on_close = restart
        manager = getattr(self, 'engine_manager', None)
        if manager:
            manager.stop.set()
        self.shutdown_deadline = (
            time.perf_counter() + SHUTDOWN_GRACE_SEC
        )

        self.logger.event(
            "shutdown_requested",
            conversion_run_ids=[
                task.current_run_id
                for task in running
                if task.current_run_id
            ],
            conversion_tab_ids=[
                task.workspace_id for task in running
            ],
            conversion_ui_task_ids=[
                task.task_id for task in running
            ],
            preview_run_ids=[
                task.current_preview_run_id
                for task in preview_running
                if task.current_preview_run_id
            ],
            preview_tab_ids=[
                task.workspace_id
                for task in preview_running
            ],
            preview_ui_task_ids=[
                task.task_id for task in preview_running
            ],
            grace_period_sec=SHUTDOWN_GRACE_SEC,
        )

        for task in running:
            task.cancel_event.set()
            task.status_var.set(
                tr("Завершаю задачу перед закрытием программы…")
            )

        for task in self.tabs.values():
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            ):
                task.current_preview_finish_reason = "app_exit"
                self.logger.event(
                    "preview_interrupted_by_app_exit",
                    task_id=task.task_id,
                    tab_id=task.workspace_id,
                    preview_run_id=task.current_preview_run_id,
                    audio_output=task.audio_output_var.get().strip()
                    or DEFAULT_AUDIO_OUTPUT_LABEL,
                )
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            ):
                if not (
                    task.preview_pause_event.is_set()
                    and task._bookmark_is_valid()
                ):
                    task.save_current_preview_bookmark(
                        reason="app_exit",
                        absolute_offset=task.current_preview_segment_start,
                        word_length=max(1, task.current_preview_word_length),
                        force_save=False,
                    )
                task.preview_keep_bookmark_after_stop = True
            task.flush_read_delete_log(
                reason="app_exit",
                force=True,
            )
            task.log_text_state(
                "app_exit",
                force=True,
            )
            task.preview_stop_event.set()
            task.preview_pause_event.clear()

        if not self._alive_background_threads():
            self._finalize_close("closed")
            return

        # Preserve the current text before waiting for background work to stop.
        # The final save still checks the text and metadata before closing.
        if self._workspace_write is None:
            self.save_workspace()
        self.root.after(100, self._poll_shutdown)
