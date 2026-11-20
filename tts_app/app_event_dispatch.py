from __future__ import annotations

from .i18n import tr

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *
from .conversion_notifications import ConversionNotifications

class AppEventDispatchMixin:
    def _process_events(self) -> None:
        # Limit each Tk callback by count and duration to prevent starvation.
        processed = 0
        batch_started = time.perf_counter()
        try:
            while True:
                if processed >= 80 or (
                    processed >= 8 and time.perf_counter() - batch_started >= 0.010
                ):
                    break
                task_id, kind, payload = self.events.get_nowait()
                processed += 1

                if kind in ("voices_loaded", "voices_error", "optional_voices_error"):
                    self._dispatch_voice_discovery(task_id, kind, payload)
                    continue

                if kind == "audio_outputs_loaded":
                    self._audio_output_scan_running = False
                    retry = getattr(self, "_audio_discovery_retry_after_id", None)
                    self._audio_discovery_retry_after_id = None
                    if retry is not None:
                        try:
                            self.root.after_cancel(retry)
                        except tk.TclError:
                            pass
                    info = payload  # type: ignore[assignment]
                    outputs = list(info.get("outputs") or [])
                    current = str(info.get("current") or "")

                    self.audio_outputs = outputs

                    selected = (
                        self.global_audio_output_var.get().strip()
                        or DEFAULT_AUDIO_OUTPUT_LABEL
                    )
                    resolved = None
                    match_mode = "default"

                    if selected != DEFAULT_AUDIO_OUTPUT_LABEL:
                        resolved, match_mode = (
                            resolve_audio_output_description(
                                selected,
                                outputs,
                            )
                        )
                        if resolved:
                            # Windows may have changed only the endpoint number.
                            # Rebind the preference to the current real description.
                            if resolved != selected:
                                self.global_audio_output_var.set(
                                    resolved
                                )
                            selected = resolved

                    # IMPORTANT: if a Bluetooth device is temporarily offline,
                    # keep its saved preference instead of silently replacing it
                    # with the Windows default device.
                    choices = [DEFAULT_AUDIO_OUTPUT_LABEL]
                    if (
                        selected != DEFAULT_AUDIO_OUTPUT_LABEL
                        and selected not in outputs
                    ):
                        choices.append(selected)
                    for item in outputs:
                        if item not in choices:
                            choices.append(item)

                    self.audio_output_choices = choices

                    was_missing = getattr(self, "_audio_output_missing", False)
                    is_missing = (
                        selected != DEFAULT_AUDIO_OUTPUT_LABEL
                        and resolved is None
                    )
                    self._audio_output_missing = is_missing
                    if is_missing:
                        self._schedule_audio_output_recheck()
                        if not was_missing:
                            self.logger.event(
                                "audio_output_unavailable",
                                selected_output=selected,
                                available_count=len(outputs),
                            )
                    else:
                        self._cancel_audio_output_recheck()
                        if was_missing:
                            self.logger.event(
                                "audio_output_reconnected",
                                selected_output=selected,
                                match_mode=match_mode,
                            )

                    for tab in list(self.tabs.values()):
                        tab.audio_output_combo.set_choices(self.audio_output_choices)

                    outputs_blob = "\n".join(outputs)
                    self.logger.event(
                        "audio_outputs_loaded",
                        count=len(outputs),
                        outputs_hash=hashlib.sha256(
                            outputs_blob.encode("utf-8")
                        ).hexdigest()[:16],
                        sapi_current_output=current,
                        selected_output=selected,
                        preferred_output_available=bool(
                            selected
                            == DEFAULT_AUDIO_OUTPUT_LABEL
                            or resolved is not None
                        ),
                        preferred_output_match_mode=match_mode,
                    )
                    continue

                if kind == "audio_outputs_error":
                    self._audio_output_scan_running = False
                    info = payload  # type: ignore[assignment]
                    exc = info["exc"]
                    self.logger.error(
                        task_id="app",
                        stage="load_audio_outputs",
                        exc=exc,
                        traceback_text=info["traceback"],
                        context={},
                    )
                    # If discovery failed while a saved Bluetooth device is
                    # missing, retry later without blocking the Tk event loop.
                    chosen = (self.global_audio_output_var.get().strip()
                              or DEFAULT_AUDIO_OUTPUT_LABEL)
                    if chosen != DEFAULT_AUDIO_OUTPUT_LABEL:
                        self._audio_output_missing = True
                        self._schedule_audio_output_recheck()
                    elif (not self.shutdown_in_progress
                          and getattr(self, "_audio_discovery_retry_after_id", None) is None):
                        self._audio_discovery_retry_after_id = self.root.after(
                            12000, self._retry_audio_output_discovery,
                        )
                    continue

                task = self.tabs.get(task_id)
                if task is None:
                    continue

                if kind == "progress":
                    info = payload  # type: ignore[assignment]
                    task.progress["value"] = float(info["pct"])
                    task.status_var.set(str(info["text"]))

                elif kind == "disk_space_warning":
                    info = payload  # type: ignore[assignment]
                    forecast = info.get("forecast") or {}
                    decision_event = info.get("decision_event")
                    decision = info.get("decision")
                    try:
                        continue_job = False
                        if not self.shutdown_in_progress:
                            continue_job = messagebox.askyesno(
                                APP_TITLE,
                                tr(
                                    "Прогноз свободного места показывает риск нехватки диска.\n\nВременные WAV: примерно {0}\nИтоговый MP3: примерно {1}\nСвободно во временной папке: {2}\nСвободно в папке MP3: {3}\n\nПродолжить несмотря на риск?",
                                    format_bytes(forecast.get('estimated_temp_wav_bytes')), format_bytes(forecast.get('estimated_mp3_bytes')), format_bytes(forecast.get('temp_free_bytes')), format_bytes(forecast.get('output_free_bytes')),
                                ),
                            )
                        if isinstance(decision, dict):
                            decision["continue"] = bool(
                                continue_job
                            )
                        self.logger.event(
                            "disk_space_warning_decision",
                            task_id=info.get("run_id"),
                            run_id=info.get("run_id"),
                            tab_id=task.workspace_id,
                            continue_job=bool(continue_job),
                        )
                    finally:
                        if isinstance(
                            decision_event,
                            threading.Event,
                        ):
                            decision_event.set()

                elif kind == "preview_segment":
                    info = payload  # type: ignore[assignment]
                    task.show_preview_segment(
                        base_index=str(info["base_index"]),
                        start=int(info["start"]),
                        end=int(info["end"]),
                        index=int(info["index"]),
                        total=int(info["total"]),
                        text=str(info["text"]),
                    )

                elif kind == "preview_segment_done":
                    info = payload  # type: ignore[assignment]
                    task.handle_preview_segment_done(info)

                elif kind == "preview_state":
                    info = payload  # type: ignore[assignment]
                    task.apply_preview_state(str(info.get("state") or ""))

                elif kind == "preview_checkpoint":
                    info = payload  # type: ignore[assignment]
                    task.apply_preview_checkpoint(info)

                elif kind == "done":
                    info = payload  # type: ignore[assignment]
                    run_id = str(info.get("run_id") or "")
                    backup_path = (
                        task.current_mp3_text_backup_path
                    )
                    removal = task.remove_completed_mp3_source(
                        run_id
                    )
                    task.set_job_idle()

                    remaining_chars = int(
                        removal.get("remaining_chars") or 0
                    )
                    if removal.get("removed"):
                        task.status_var.set(
                            tr("Готово: {0} — конвертированный текст удалён.", info['output'])
                        )
                        text_note = (
                            tr(
                                "\n\nКонвертированный текст удалён из вкладки.\nДобавленный позже текст сохранён: {0:,} символов.",
                                remaining_chars,
                            )
                        ).replace(",", " ")
                    else:
                        task.status_var.set(
                            tr("Готово: {0} — исходный текст сохранён.", info['output'])
                        )
                        text_note = (
                            tr("\n\nИсходный текст не удалён: его снимок "
                            "изменился или не удалось безопасно подтвердить "
                            "границы. Новый текст также сохранён.")
                        )

                    if not self.shutdown_in_progress and not self.settings.get("quiet_mp3_notifications"):
                        if not hasattr(self, "completion_notifications"):
                            self.completion_notifications = ConversionNotifications(self.root)
                        backup_note = tr("\nРезервная копия текста сохранена.") if backup_path else ""
                        self.completion_notifications.show(
                            Path(info["output"]), text_note.strip() + backup_note,
                        )

                elif kind == "cancelled":
                    info = payload  # type: ignore[assignment]
                    task.discard_mp3_source_snapshot(
                        str(info.get("run_id") or "") or None,
                        reason="cancelled",
                    )
                    task.set_job_idle()
                    task.status_var.set(tr("Остановлено."))
                    if not self.shutdown_in_progress:
                        messagebox.showinfo(
                            APP_TITLE,
                            str(info["message"]),
                        )

                elif kind == "job_error":
                    info = payload  # type: ignore[assignment]
                    task.discard_mp3_source_snapshot(
                        str(info.get("run_id") or "") or None,
                        reason="job_error",
                    )
                    task.set_job_idle()
                    task.status_var.set(tr("Ошибка."))

                    diagnostic = ""
                    if info.get("error_log"):
                        diagnostic = (
                            tr("\n\nДиагностика сохранена:\n")
                            + str(info["error_log"])
                        )

                    recovery_note = ""
                    if info.get("recovery_preserved"):
                        recovery_note = (
                            tr("\n\nГотовые WAV сохранены для восстановления "
                            "после следующего запуска программы:\n")
                            + str(info.get("recovery_dir") or "")
                        )

                    if not self.shutdown_in_progress:
                        messagebox.showerror(
                            APP_TITLE,
                            tr(str(info.get("user_message") or info["exc"]))
                            + diagnostic
                            + recovery_note,
                        )

                elif kind == "preview_done":
                    info = payload if isinstance(payload, dict) else {}

                    finished_preview_run_id = info.get(
                        "preview_run_id"
                    )
                    stopped = bool(info.get("stopped"))

                    finish_reason = (
                        task.current_preview_finish_reason
                        or (
                            "completed"
                            if not stopped
                            else "other"
                        )
                    )
                    requested_audio_output = str(
                        info.get("requested_audio_output")
                        or ""
                    )
                    actual_audio_output = str(
                        info.get("actual_audio_output")
                        or ""
                    )

                    if (
                        requested_audio_output
                        and requested_audio_output
                        != DEFAULT_AUDIO_OUTPUT_LABEL
                        and actual_audio_output
                        and audio_output_descriptions_equivalent(
                            requested_audio_output,
                            actual_audio_output,
                        )
                        and self.global_audio_output_var.get().strip()
                        != actual_audio_output
                    ):
                        old_output = (
                            self.global_audio_output_var.get().strip()
                        )
                        self.global_audio_output_var.set(
                            actual_audio_output
                        )
                        self.logger.event(
                            "audio_output_preference_rebound",
                            previous_output=old_output,
                            actual_output=actual_audio_output,
                        )

                    self.logger.event(
                        "preview_finished",
                        task_id=task.task_id,
                        tab_id=task.workspace_id,
                        preview_run_id=finished_preview_run_id,
                        finish_reason=finish_reason,
                        requested_audio_output=requested_audio_output,
                        actual_audio_output=actual_audio_output,
                        chunks=info.get("chunks"),
                        completed_chunks=info.get("completed_chunks"),
                        sapi_stream_blocks=info.get(
                            "sapi_stream_blocks"
                        ),
                        stopped=info.get("stopped"),
                        paused_at_finish=info.get("paused_at_finish"),
                        duration_sec=info.get("duration_sec"),
                    )
                    task.flush_read_delete_log(
                        reason=finish_reason,
                        force=True,
                    )
                    task.log_text_state(
                        f"preview_finished_{finish_reason}",
                        force=True,
                    )

                    # preview_sapi_text has already returned and released COM.
                    # Clear the thread reference so another SAPI preview can
                    # safely be created in a fresh COM worker.
                    task.preview_thread = None

                    if task.preview_resume_previous_pending:
                        task.preview_resume_previous_pending = False
                        task.preview_keep_bookmark_after_stop = False
                        previous_run_id = (
                            task.preview_resume_from_run_id
                            or finished_preview_run_id
                        )

                        if task.resume_saved_preview():
                            self.logger.event(
                                "preview_resume_chain",
                                task_id=task.task_id,
                                tab_id=task.workspace_id,
                                previous_preview_run_id=previous_run_id,
                                next_preview_run_id=task.current_preview_run_id,
                                mode="rewind_previous_sentence",
                            )
                            task.preview_resume_from_run_id = None
                            continue

                        # If the bookmark unexpectedly became invalid, return the
                        # UI to a safe stopped state instead of hanging buttons.
                        task.preview_resume_from_run_id = None
                        task.preview_finished_ui(
                            stopped=True,
                        )
                        continue

                    if not stopped:
                        task.finalize_delete_read_text()

                    if (
                        not stopped
                        and task.continue_appended_tail_if_needed()
                    ):
                        continue

                    task.preview_finished_ui(
                        stopped=stopped,
                    )
                    task.current_preview_finish_reason = None

                elif kind == "preview_error":
                    info = payload  # type: ignore[assignment]
                    exc = info["exc"]

                    preview_context = info["context"]
                    self.logger.error(
                        task_id=str(
                            preview_context.get(
                                "preview_run_id"
                            )
                            or task.task_id
                        ),
                        stage="preview",
                        exc=exc,
                        traceback_text=info["traceback"],
                        context=preview_context,
                    )

                    # A disconnected endpoint may return between attempts.
                    # Refresh the real device list but never auto-switch output
                    # or replay an unverified block.
                    if isinstance(exc, AudioOutputUnavailableError) or "сейчас не найдено в Windows SAPI" in str(exc):
                        self._load_audio_outputs_async()

                    task.current_preview_finish_reason = "error"
                    self.logger.event(
                        "preview_finished",
                        task_id=task.task_id,
                        tab_id=task.workspace_id,
                        preview_run_id=preview_context.get(
                            "preview_run_id"
                        ),
                        finish_reason="error",
                        stopped=True,
                        duration_sec=None,
                    )
                    task.flush_read_delete_log(
                        reason="error",
                        force=True,
                    )
                    task.preview_keep_bookmark_after_stop = bool(
                        task.preview_bookmark
                    )
                    task.preview_finished_ui(stopped=True)
                    if not task.preview_bookmark:
                        task.clear_preview_highlight()
                    task.status_var.set(
                        tr("Ошибка воспроизведения. "
                        "Сохранённую позицию можно продолжить.")
                        if task.preview_bookmark
                        else tr("Ошибка воспроизведения.")
                    )

                    if not self.shutdown_in_progress:
                        messagebox.showerror(
                            APP_TITLE,
                            tr("Не удалось воспроизвести текст.\n\n{0}", audio_error_message(exc)),
                        )

        except queue.Empty:
            pass

        # A remaining backlog gets another short slice of the UI loop.
        self.root.after(1 if not self.events.empty() else 100, self._process_events)
