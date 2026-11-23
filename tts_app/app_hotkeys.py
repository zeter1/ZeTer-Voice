from __future__ import annotations

from .i18n import tr
from .conversion_notifications import ConversionNotifications
from .hotkey_capture_guidance import CAPTURE_FAILURE_GUIDANCE, CAPTURE_TIMEOUT_GUIDANCE, CLIPBOARD_SOURCE_CHECK_DETAIL

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *


CAPTURE_LOG_FIELDS = (
    "capture_id", "capture_protocol", "capture_stage", "copy_kind",
    "source_process", "source_pid", "copy_latency_ms", "dispatch_ms", "send_input_accepted", "delivery_ms",
    "clipboard_notified", "clipboard_sequence_changed", "clipboard_update_ms",
    "clipboard_owner_pid", "clipboard_unicode_available", "read_attempts",
    "clipboard_conflict", "clipboard_source_lock_confirmed", "clipboard_sequence_advanced_after_lock",
    "clipboard_sequence_advanced_at_open", "clipboard_sequence_advanced_after_busy",
    "clipboard_sequence_before", "clipboard_sequence_observed", "clipboard_sequence_latest",
    "clipboard_open_busy_count", "clipboard_busy_opener", "clipboard_source_check",
    "clipboard_sequence_before_open", "clipboard_owner_sample",
    "clipboard_open_at_dispatch", "clipboard_open_at_retry", "clipboard_open_at_timeout",
    "blocked_by_capture_id", "blocked_by_capture_stage",
    "copy_waited_for_input", "capture_mouse_guard", "capture_attempt", "capture_retry_of", "capture_retry_skipped",
    "capture_repeat_id", "capture_retry_mode", "capture_retry_dispatched", "capture_retry_ms",
)


class AppHotkeysMixin:
    def _normalized_tab_hotkey(
        self,
        value: str,
    ) -> str:
        raw = str(value or "").strip()
        if (
            not raw
            or raw.casefold()
            == TAB_HOTKEY_NONE_LABEL.casefold()
        ):
            return TAB_HOTKEY_NONE_LABEL

        parsed = GlobalCopyHotkeyMonitor.parse_hotkey(
            raw
        )
        return str(parsed["text"])

    def _task_hotkey_owner(
        self,
        normalized_hotkey: str,
        *,
        exclude_workspace_id: str | None = None,
    ) -> TaskTab | None:
        wanted = str(normalized_hotkey or "").strip()
        if (
            not wanted
            or wanted == TAB_HOTKEY_NONE_LABEL
        ):
            return None
        parsed = GlobalCopyHotkeyMonitor.parse_hotkey(wanted)

        for task in self.tabs.values():
            if (
                exclude_workspace_id
                and task.workspace_id
                == exclude_workspace_id
            ):
                continue
            current = task.applied_copy_hotkey
            if current and current != TAB_HOTKEY_NONE_LABEL:
                current_parsed = GlobalCopyHotkeyMonitor.parse_hotkey(current)
                if (current_parsed["vks"] == parsed["vks"]
                        and current_parsed["modifiers"] == parsed["modifiers"]):
                    return task
        return None

    def _stop_tab_hotkey_monitor(
        self,
        workspace_id: str,
    ) -> None:
        monitor = self.tab_hotkey_monitors.pop(
            workspace_id,
            None,
        )
        if monitor is not None:
            try:
                monitor.stop()
            except Exception:
                pass

    def apply_tab_copy_hotkey(
        self,
        task: TaskTab,
        *,
        silent: bool = False,
        save_workspace: bool = True,
    ) -> bool:
        """
        Bind one unique global hotkey to one exact tab.

        Routing never depends on which tab is currently visible.  "Нет" removes
        the binding. Aliases such as Й+Ц and Q+W normalize to the same physical
        keys, so they are also treated as duplicates.
        """
        previous = (
            task.applied_copy_hotkey
            or TAB_HOTKEY_NONE_LABEL
        )

        try:
            normalized = self._normalized_tab_hotkey(
                task.copy_hotkey_var.get()
            )
        except Exception as exc:
            task.copy_hotkey_var.set(previous)
            task.copy_hotkey_status_var.set(
                tr("Ошибка: {0}", exc)
            )
            self.logger.event(
                "tab_copy_hotkey_invalid",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                requested_hotkey=task.copy_hotkey_var.get(),
                error=f"{type(exc).__name__}: {exc}",
            )
            if not silent:
                messagebox.showerror(
                    tr("Горячая клавиша"),
                    str(exc),
                )
            return False

        if normalized == TAB_HOTKEY_NONE_LABEL:
            self._stop_tab_hotkey_monitor(
                task.workspace_id
            )
            task.applied_copy_hotkey = (
                TAB_HOTKEY_NONE_LABEL
            )
            task.copy_hotkey_var.set(
                TAB_HOTKEY_NONE_LABEL
            )
            task.copy_hotkey_status_var.set(
                tr("Горячая клавиша не назначена.")
            )
            self.logger.event(
                "tab_copy_hotkey_cleared",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                tab_title=task.tab_title(),
            )
            if save_workspace:
                self.schedule_workspace_save()
            return True

        unsafe = GlobalCopyHotkeyMonitor.unsafe_hotkey_reason(normalized)
        if unsafe:
            if silent:
                # Keep legacy assignments in the workspace so they can be reassigned.
                task.applied_copy_hotkey = task.copy_hotkey_var.get()
                task.copy_hotkey_status_var.set(tr("Неактивна. {0}", unsafe))
                return True
            task.copy_hotkey_var.set(previous)
            task.copy_hotkey_status_var.set(unsafe)
            messagebox.showwarning(tr("Горячая клавиша"), unsafe)
            return False

        owner = self._task_hotkey_owner(
            normalized,
            exclude_workspace_id=task.workspace_id,
        )
        if owner is not None:
            task.copy_hotkey_var.set(previous)
            message = (
                tr("Горячая клавиша {0} уже назначена вкладке «{1}». Выберите другую клавишу.", normalized, owner.tab_title())
            )
            task.copy_hotkey_status_var.set(
                tr("{0} уже занята другой вкладкой.", normalized)
            )
            self.logger.event(
                "tab_copy_hotkey_conflict",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                requested_hotkey=normalized,
                owner_task_id=owner.task_id,
                owner_tab_id=owner.workspace_id,
                owner_tab_title=owner.tab_title(),
            )
            if not silent:
                messagebox.showwarning(
                    tr("Горячая клавиша уже используется"),
                    message,
                )
            return False

        # Nothing to rebuild when the same binding is simply re-applied.
        existing_monitor = self.tab_hotkey_monitors.get(
            task.workspace_id
        )
        if (
            previous == normalized
            and existing_monitor is not None
            and existing_monitor.enabled
        ):
            task.copy_hotkey_var.set(normalized)
            task.copy_hotkey_status_var.set(
                tr("{0} → только эта вкладка", normalized)
            )
            return True

        # Validate first, then replace the old monitor.
        old_monitor = existing_monitor
        self._stop_tab_hotkey_monitor(
            task.workspace_id
        )

        try:
            monitor = GlobalCopyHotkeyMonitor(
                self.root,
                lambda capture_info, workspace_id=task.workspace_id:
                    self._on_tab_copy_hotkey(
                        workspace_id,
                        capture_info,
                    ),
                hotkey=normalized,
            )
            monitor.enable()
            self.tab_hotkey_monitors[
                task.workspace_id
            ] = monitor

        except Exception as exc:
            # Best effort: restore the previous valid assignment.
            if (
                old_monitor is not None
                and previous
                != TAB_HOTKEY_NONE_LABEL
            ):
                try:
                    restored_monitor = (
                        GlobalCopyHotkeyMonitor(
                            self.root,
                            lambda capture_info, workspace_id=task.workspace_id:
                                self._on_tab_copy_hotkey(
                                    workspace_id,
                                    capture_info,
                                ),
                            hotkey=previous,
                        )
                    )
                    restored_monitor.enable()
                    self.tab_hotkey_monitors[
                        task.workspace_id
                    ] = restored_monitor
                except Exception:
                    previous = TAB_HOTKEY_NONE_LABEL

            task.applied_copy_hotkey = previous
            task.copy_hotkey_var.set(previous)
            task.copy_hotkey_status_var.set(
                tr("Не удалось включить {0}.", normalized)
            )
            self.logger.event(
                "tab_copy_hotkey_enable_failed",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                requested_hotkey=normalized,
                error=f"{type(exc).__name__}: {exc}",
            )
            if not silent:
                messagebox.showerror(
                    tr("Горячая клавиша"),
                    tr("Не удалось включить горячую клавишу.\n\n{0}", exc),
                )
            return False

        task.applied_copy_hotkey = normalized
        task.copy_hotkey_var.set(normalized)
        task.copy_hotkey_status_var.set(
            tr("{0} → только эта вкладка", normalized)
        )

        self.logger.event(
            "tab_copy_hotkey_applied",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            tab_title=task.tab_title(),
            hotkey=normalized,
        )

        if save_workspace:
            self.schedule_workspace_save()
        return True

    def _activate_restored_tab_hotkeys(self) -> None:
        """
        Restore persisted per-tab bindings in visible tab order.

        If an old/corrupt workspace somehow contains duplicate bindings, the
        first tab keeps the key and later conflicting tabs are reset to "Нет".
        """
        for widget_name in self.notebook.tabs():
            task = None
            for candidate in self.tabs.values():
                if str(candidate.frame) == widget_name:
                    task = candidate
                    break
            if task is None:
                continue

            requested = str(
                task.copy_hotkey_var.get()
                or TAB_HOTKEY_NONE_LABEL
            )
            if not self.apply_tab_copy_hotkey(
                task,
                silent=True,
                save_workspace=False,
            ):
                task.applied_copy_hotkey = (
                    TAB_HOTKEY_NONE_LABEL
                )
                task.copy_hotkey_var.set(
                    TAB_HOTKEY_NONE_LABEL
                )
                task.copy_hotkey_status_var.set(
                    tr("Горячая клавиша не назначена.")
                )
                self.logger.event(
                    "restored_tab_hotkey_reset",
                    task_id=task.task_id,
                    tab_id=task.workspace_id,
                    requested_hotkey=requested,
                    reason="invalid_or_duplicate",
                )

        self.schedule_workspace_save()

    def _on_tab_copy_hotkey(
        self,
        workspace_id: str,
        capture_info: dict,
    ) -> None:
        if self.shutdown_in_progress:
            return
        task = self._task_by_workspace_id(workspace_id)
        monitor = self.tab_hotkey_monitors.get(workspace_id)
        if task is None or monitor is None or not monitor.enabled:
            return

        hotkey = monitor.get_hotkey()
        evidence = {key: capture_info[key] for key in CAPTURE_LOG_FIELDS if key in capture_info}
        evidence.update(task_id=task.task_id, tab_id=task.workspace_id, hotkey=hotkey)
        rejected = capture_info.get("capture_rejected")
        captured = capture_info.get("captured_text")
        if not rejected and (not isinstance(captured, str) or not captured.strip()):
            rejected = "clipboard_empty"
        if rejected:
            self._report_tab_capture_failure(task, rejected, evidence)
            return

        sequence = capture_info["clipboard_sequence_after"]
        if sequence is not None and sequence == getattr(monitor, "_last_appended_capture_sequence", None):
            self.logger.event(
                "global_copy_duplicate_skipped", clipboard_sequence=sequence, **evidence,
            )
            self._report_tab_capture_failure(
                task, "duplicate_snapshot",
                dict(evidence, capture_stage="duplicate_check", clipboard_sequence=sequence),
            )
            return

        preview_running = bool(task.preview_thread and task.preview_thread.is_alive())
        paused = bool(task.preview_is_paused)
        auto_read = bool(task.auto_read_hotkey_var.get())
        append_started = time.perf_counter()
        evidence["capture_stage"] = "append_text"
        try:
            appended = task.append_external_selected_text(captured)
        except (tk.TclError, TypeError, ValueError) as exc:
            self._report_tab_capture_failure(
                task, "append_exception",
                dict(evidence, exception_type=type(exc).__name__, chars=len(captured)),
            )
            raise
        if not appended:
            self._report_tab_capture_failure(
                task, "append_declined", dict(evidence, chars=len(captured)),
            )
            return
        monitor._last_appended_capture_sequence = sequence

        evidence["capture_stage"] = "appended"
        self.logger.event(
            "global_copy_text_captured", routing_mode="fixed_tab",
            chars=len(captured), clipboard_changed=True, clipboard_sequence=sequence,
            append_ms=round((time.perf_counter() - append_started) * 1000, 1),
            preview_running=preview_running, paused=paused, auto_read_hotkey_text=auto_read,
            **evidence,
        )

    def _report_tab_capture_failure(self, task: TaskTab, reason: str, evidence: dict) -> None:
        stage = evidence.get("capture_stage")
        guidance = CAPTURE_TIMEOUT_GUIDANCE.get(stage, reason) if reason == "clipboard_timeout" else reason
        if reason == "clipboard_superseded" and evidence.get("clipboard_conflict") in {
            "before_dispatch", "while_waiting_to_read", "at_snapshot_open",
        }:
            guidance = "clipboard_version_changed"
        detail, check, fix = CAPTURE_FAILURE_GUIDANCE.get(guidance, (
            "Не удалось захватить текст. Выделите его и повторите.",
            "Найди соседние события с тем же capture_id и проверь, на каком этапе захват остановился.",
            "Скопируйте пропущенный фрагмент вручную. Для разбора передайте Codex эту запись и соседние события из этой же сессии.",
        ))
        owner = "tts_app/windows_hotkey_capture.py"
        if reason == "clipboard_send_failed":
            owner = "tts_app/windows_hotkey_hook.py"
        elif reason == "duplicate_snapshot":
            owner = "tts_app/app_hotkeys.py"
        elif stage == "append_text":
            owner = "tts_app/task_capture.py"
        reason_detail = detail
        if reason == "clipboard_timeout" and stage == "waiting_input_release":
            reason_detail = "Кнопку мыши или клавишу-модификатор удерживали до конца ожидания. Команда Ctrl+C не отправлялась."
            check = "Проверь copy_waited_for_input. Повтори с тем же выделением, отпустив кнопку мыши и клавиши сочетания."
        if reason == "clipboard_timeout" and evidence.get("capture_retry_skipped"):
            check = "Проверь capture_retry_skipped и состояние исходного окна, ввода и буфера перед повторной командой."
        if guidance == "clipboard_version_changed":
            source_check = evidence.get("clipboard_source_check")
            if source_check == "owner_unconfirmed":
                source_check = evidence.get("clipboard_owner_sample", source_check)
            reason_detail = CLIPBOARD_SOURCE_CHECK_DETAIL.get(source_check, detail)
        if reason == "clipboard_empty":
            unicode_available = evidence.get("clipboard_unicode_available")
            if unicode_available is False:
                reason_detail = "Буфер обновился, но в нём не оказалось Unicode-текста."
            elif unicode_available is True:
                reason_detail = "В буфере был Unicode-текст, но он оказался пустым или содержал только пробельные символы."
        self.logger.event(
            "global_copy_capture_rejected", reason=reason, reason_detail=reason_detail,
            suggested_fix=fix, suggested_check=check, owner_module=owner, **evidence,
        )
        message = tr(detail)
        task.status_var.set(message)
        try:
            if not hasattr(self, "completion_notifications"):
                self.completion_notifications = ConversionNotifications(self.root)
            self.completion_notifications.show_capture_failure(
                evidence["hotkey"], message, source_process=evidence.get("source_process") or "",
                insertion_uncertain=reason == "append_exception",
            )
        except (OSError, tk.TclError) as exc:
            self.logger.event(
                "global_copy_notification_failed", capture_id=evidence.get("capture_id"),
                reason=reason, exception_type=type(exc).__name__,
            )

