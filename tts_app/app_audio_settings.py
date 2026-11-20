from __future__ import annotations

from .voice_catalog import available_extra_voice_names

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import ClosableNotebook
from .task_tab import TaskTab
from .task_ui_state import VOICES_LOADING_STATUS

class AppAudioSettingsMixin:
    def _load_voices_async(self) -> None:
        self._voice_load_generation = getattr(self, "_voice_load_generation", 0) + 1
        generation = str(self._voice_load_generation)
        current = self.current_tab()
        if current:
            current.status_var.set(VOICES_LOADING_STATUS)

        def worker() -> None:
            voices = []
            failures = []
            try:
                voices.extend(get_sapi_voices())
            except Exception as exc:
                failures.append(("voices_error", {"exc": exc, "traceback": traceback.format_exc()}))
            try:
                voices.extend(available_extra_voice_names())
            except Exception as exc:
                failures.append(("optional_voices_error", {"exc": exc, "traceback": traceback.format_exc()}))
            self.events.put((generation, "voices_loaded", voices))
            for kind, error in failures:
                self.events.put((generation, kind, error))

        threading.Thread(
            target=worker,
            name="voice-discovery",
            daemon=True,
        ).start()

    def _dispatch_voice_discovery(self, generation: str, kind: str, payload) -> None:
        if generation != str(self._voice_load_generation):
            return
        if kind == "voices_loaded":
            self.voices = list(payload)
            current = self.current_tab()
            self.logger.event(
                "voices_loaded", count=len(self.voices),
                voices_hash=hashlib.sha256("\n".join(self.voices).encode("utf-8")).hexdigest()[:16],
                selected_voice=current.voice_var.get().strip() if current else self.settings.get("voice", ""),
            )
            for task in self.tabs.values():
                task.apply_voice_list(self.voices)
            return

        exc = payload["exc"]
        self.logger.error(
            task_id="app", stage="load_sapi_voices" if kind == "voices_error" else "load_optional_voices",
            exc=exc, traceback_text=payload["traceback"], context={},
        )
        if kind == "voices_error" and not self.shutdown_in_progress:
            messagebox.showerror(APP_TITLE, f"Не удалось получить список SAPI-голосов.\n\n{exc}")

    def _load_audio_outputs_async(self) -> None:
        # Only one SAPI enumeration worker at a time. Bluetooth reconnection
        # checks must not create overlapping COM calls.
        if getattr(self, "_audio_output_scan_running", False):
            return
        self._audio_output_scan_running = True

        def worker() -> None:
            try:
                outputs, current = get_sapi_audio_outputs()
                self.events.put(
                    (
                        "",
                        "audio_outputs_loaded",
                        {
                            "outputs": outputs,
                            "current": current,
                        },
                    )
                )
            except Exception as exc:
                self.events.put(
                    (
                        "",
                        "audio_outputs_error",
                        {
                            "exc": exc,
                            "traceback": traceback.format_exc(),
                        },
                    )
                )

        threading.Thread(
            target=worker,
            name="audio-output-discovery",
            daemon=True,
        ).start()

    def refresh_audio_outputs(self) -> None:
        self.logger.event("audio_outputs_refresh_requested")
        self._load_audio_outputs_async()

    def _cancel_audio_output_recheck(self) -> None:
        pending = getattr(self, "_audio_recheck_after_id", None)
        self._audio_recheck_after_id = None
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass

    def _schedule_audio_output_recheck(self) -> None:
        # Poll ONLY while the saved concrete device is missing. Keep the user's
        # chosen output and never fall back to an arbitrary/default endpoint.
        if (self.shutdown_in_progress
                or getattr(self, "_audio_recheck_after_id", None) is not None):
            return
        self._audio_recheck_after_id = self.root.after(
            12000, self._recheck_missing_audio_output,
        )

    def _retry_audio_output_discovery(self) -> None:
        self._audio_discovery_retry_after_id = None
        if not self.shutdown_in_progress:
            self._load_audio_outputs_async()

    def _recheck_missing_audio_output(self) -> None:
        self._audio_recheck_after_id = None
        if self.shutdown_in_progress:
            return
        selected = (
            self.global_audio_output_var.get().strip()
            or DEFAULT_AUDIO_OUTPUT_LABEL
        )
        if selected == DEFAULT_AUDIO_OUTPUT_LABEL:
            return
        resolved, _ = resolve_audio_output_description(
            selected, self.audio_outputs,
        )
        if resolved is None:
            self._load_audio_outputs_async()

    def stop_other_previews(self, active: TaskTab) -> None:
        for task in list(self.tabs.values()):
            if task is not active:
                task.stop_preview(
                    preserve_bookmark=True,
                    reason="another_tab_started_preview",
                )

    def output_is_used_by_other_task(
        self,
        active: TaskTab,
        output_path: Path,
    ) -> bool:
        try:
            wanted = os.path.normcase(
                os.path.abspath(str(output_path))
            )
        except Exception:
            wanted = str(output_path)

        for task in self.tabs.values():
            if task is active or not task.conversion_running():
                continue

            if task.current_output_path is None:
                continue

            try:
                other_path = os.path.normcase(
                    os.path.abspath(str(task.current_output_path))
                )
            except Exception:
                other_path = str(task.current_output_path)

            if other_path == wanted:
                return True

        return False

    def open_logs(self) -> None:
        self._open_folder_safe(
            LOGS_DIR,
            "open_logs_folder",
        )

    def open_settings(self) -> None:
        self._open_folder_safe(
            SETTINGS_DIR,
            "open_settings_folder",
        )

    def _open_folder_safe(
        self,
        path: Path,
        stage: str,
    ) -> None:
        try:
            open_folder(path)
        except Exception as exc:
            self.log_ui_error(
                task_id=(
                    self.current_tab().task_id
                    if self.current_tab()
                    else "app"
                ),
                stage=stage,
                exc=exc,
                context={"path": str(path)},
            )
            messagebox.showerror(
                APP_TITLE,
                f"Не удалось открыть папку:\n{path}\n\n{exc}",
            )

    def _on_global_rate_changed(self, *_args) -> None:
        if self._global_rate_syncing:
            return

        try:
            rate = clamp_int(
                self.global_rate_var.get(),
                RATE_MIN,
                RATE_MAX,
                0,
            )
        except tk.TclError:
            return

        # Normalize out-of-range/manually typed values without recursive work.
        try:
            current = int(self.global_rate_var.get())
        except Exception:
            current = rate

        if current != rate:
            self._global_rate_syncing = True
            try:
                self.global_rate_var.set(rate)
            finally:
                self._global_rate_syncing = False

        self.settings["rate"] = rate

        # The sliders have local display variables. Keep all of them in sync
        # with the single whole-application integer setting.
        for task in list(self.tabs.values()):
            task.rate_value_var.set(str(rate))

        self.logger.event(
            "global_rate_changed",
            rate=rate,
        )
        self.schedule_settings_save()
        self.schedule_workspace_save()

    def _on_global_pitch_changed(self, *_args) -> None:
        if self._global_pitch_syncing:
            return

        try:
            pitch = clamp_int(
                self.global_pitch_var.get(),
                PITCH_MIN,
                PITCH_MAX,
                0,
            )
        except tk.TclError:
            return

        # Normalize out-of-range/manually typed values without recursive work.
        try:
            current = int(self.global_pitch_var.get())
        except Exception:
            current = pitch

        if current != pitch:
            self._global_pitch_syncing = True
            try:
                self.global_pitch_var.set(pitch)
            finally:
                self._global_pitch_syncing = False

        self.settings["pitch"] = pitch

        # The sliders have local display variables and a shared integer setting.
        for task in list(self.tabs.values()):
            task.pitch_value_var.set(str(pitch))

        self.schedule_settings_save()
        self.schedule_workspace_save()

    def _on_global_audio_output_changed(self, *_args) -> None:
        selected = (
            self.global_audio_output_var.get().strip()
            or DEFAULT_AUDIO_OUTPUT_LABEL
        )
        self.settings["audio_output"] = selected
        # Cancel obsolete probes when the user deliberately changes output.
        if selected == DEFAULT_AUDIO_OUTPUT_LABEL:
            self._audio_output_missing = False
            self._cancel_audio_output_recheck()
        else:
            resolved, _ = resolve_audio_output_description(
                selected, self.audio_outputs,
            )
            self._audio_output_missing = resolved is None
            if self._audio_output_missing:
                self._schedule_audio_output_recheck()
            else:
                self._cancel_audio_output_recheck()
        self.logger.event(
            "audio_output_selected",
            audio_output=selected,
        )
        self.schedule_settings_save()
        self.schedule_workspace_save()

    def schedule_settings_save(
        self,
        source_tab: TaskTab | None = None,
    ) -> None:
        if source_tab is not None:
            self.settings["voice"] = source_tab.voice_var.get().strip()
            self.settings["rate"] = clamp_int(
                self.global_rate_var.get(),
                RATE_MIN,
                RATE_MAX,
                0,
            )
            self.settings["pitch"] = clamp_int(
                self.global_pitch_var.get(),
                PITCH_MIN,
                PITCH_MAX,
                0,
            )
            self.settings["audio_output"] = (
                self.global_audio_output_var.get().strip()
                or DEFAULT_AUDIO_OUTPUT_LABEL
            )
            self.settings["volume"] = clamp_int(
                source_tab.volume_var.get(),
                0,
                100,
                100,
            )

        if self.settings_after_id:
            try:
                self.root.after_cancel(
                    self.settings_after_id
                )
            except Exception:
                pass

        self.settings_after_id = self.root.after(
            400,
            self.save_settings,
        )

    def save_settings(self) -> bool:
        if self.settings_after_id is not None:
            self.root.after_cancel(self.settings_after_id)
        self.settings_after_id = None

        try:
            self.settings["window_geometry"] = self.root.geometry()

            # Since 4.0 hotkeys live in workspace.json PER TAB. Keep old global
            # setting disabled so an old value can never leak into a new tab.
            self.settings["global_copy_enabled"] = False

            self.settings["autostart_windows"] = bool(
                self.autostart_windows_var.get()
            )
            atomic_write_json(
                SETTINGS_FILE,
                normalize_settings(self.settings),
            )
        except Exception as exc:
            self.logger.event(
                "settings_save_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            if not self.shutdown_in_progress and not getattr(self, "_settings_save_failure_reported", False):
                self._settings_save_failure_reported = True
                messagebox.showerror(
                    APP_TITLE,
                    "Не удалось записать настройки. Проверьте доступ к папке настроек. "
                    "Программа попробует сохранить их при следующем изменении или закрытии.",
                    parent=self.root,
                )
            return False
        self._settings_save_failure_reported = False
        return True

    def log_ui_error(
        self,
        *,
        task_id: str,
        stage: str,
        exc: BaseException,
        context: dict,
    ) -> None:
        self.logger.error(
            task_id=task_id,
            stage=stage,
            exc=exc,
            traceback_text=traceback.format_exc(),
            context=context,
        )
