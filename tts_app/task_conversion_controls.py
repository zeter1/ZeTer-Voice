from __future__ import annotations

from .engine_manager_ui import engine_operation_busy
from .mp3_format import read_mp3_format

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskConversionControlsMixin:
    def ask_output_path(self) -> Path | None:
        kwargs = {}

        for key in ("mp3_output_dir", "last_output_dir"):
            folder = self.app.settings.get(key)
            try:
                if folder and Path(folder).is_dir():
                    kwargs["initialdir"] = folder
                    break
            except OSError:
                continue

        title = self.tab_title().strip()
        if title and not title.startswith("Задача "):
            safe_name = re.sub(r'[<>:"/\\|?*]+', "_", title).strip(" .")
            if safe_name:
                kwargs["initialfile"] = safe_name + ".mp3"

        chosen = filedialog.asksaveasfilename(
            title=f"Сохранить MP3 — {self.tab_title()}",
            defaultextension=".mp3",
            filetypes=[("MP3", "*.mp3")],
            confirmoverwrite=False,
            **kwargs,
        )
        if not chosen:
            return None

        output_path = Path(chosen)
        if output_path.suffix.lower() != ".mp3":
            output_path = output_path.with_suffix(".mp3")

        self.app.settings["last_output_dir"] = str(output_path.parent)
        self.app.schedule_settings_save(self)
        return output_path

    def start_job(self) -> None:
        if engine_operation_busy(self.app):
            messagebox.showinfo(APP_TITLE, 'Дождитесь завершения операции с движками или отмените её.')
            return
        if self.current_run_id or self.conversion_running():
            return

        if self.text_box.compare("1.0", "==", "end-1c"):
            messagebox.showwarning(
                APP_TITLE,
                "Вставьте текст.",
            )
            return

        voice = self.voice_var.get().strip()
        if not voice:
            messagebox.showwarning(APP_TITLE, "Выберите голос.")
            return

        output_path = self.ask_output_path()
        if output_path is None or self.app.shutdown_in_progress or self.current_run_id:
            return

        try:
            approved_output_identity = mp3_target_identity(output_path)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Не удалось проверить путь MP3.\n{exc}")
            return
        if approved_output_identity is not None and not messagebox.askyesno(
            APP_TITLE, f"Заменить существующий файл?\n{output_path}",
        ):
            return
        if self.app.shutdown_in_progress or self.current_run_id:
            return
        if self.app.output_is_used_by_other_task(self, output_path):
            messagebox.showwarning(
                APP_TITLE,
                "Другая вкладка уже создаёт MP3 по этому же пути.\n\n"
                "Выберите другое имя файла, чтобы задачи не перезаписали друг друга.",
            )
            return

        # Capture after both dialogs, immediately before tracking and backup.
        raw_text = self.text_box.get("1.0", "end-1c")
        if not raw_text or raw_text.isspace():
            messagebox.showwarning(
                APP_TITLE,
                "В тексте больше нет данных для создания MP3.",
            )
            return

        rate = clamp_int(self.rate_var.get(), RATE_MIN, RATE_MAX, 0)
        pitch = clamp_int(self.pitch_var.get(), PITCH_MIN, PITCH_MAX, 0)
        volume = clamp_int(self.volume_var.get(), 0, 100, 100)
        bitrate, channels, sample_rate_hz = read_mp3_format(self.app.settings)
        tab_title_snapshot = self.tab_title()
        visible_tab_index_snapshot = self.app.visible_tab_index(self)

        run_id = make_run_id("mp3")
        if not self.track_mp3_source_snapshot(
            run_id,
            raw_text,
        ):
            self.app.logger.event(
                "mp3_source_snapshot_track_failed",
                task_id=run_id,
                run_id=run_id,
                tab_id=self.workspace_id,
                source_chars=len(raw_text),
            )
            messagebox.showerror(
                APP_TITLE,
                "Не удалось зафиксировать точный снимок текста. "
                "Создание MP3 не запущено, чтобы не удалить неверный текст.",
            )
            return

        self._launch_mp3_job(
            run_id, raw_text, tab_title_snapshot, visible_tab_index_snapshot, output_path,
            voice, rate, pitch, volume, bitrate, channels=channels, sample_rate_hz=sample_rate_hz,
            approved_output_identity=approved_output_identity,
        )

    def resume_recovery_job(
        self,
        manifest: dict,
        work_dir: Path,
    ) -> bool:
        if engine_operation_busy(self.app):
            return False
        if self.current_run_id or self.conversion_running():
            return False

        raw_text = self.text_box.get("1.0", "end-1c")
        text = normalize_text(raw_text)
        if not text:
            return False

        expected_hash = str(manifest.get("text_sha256") or "")
        actual_hash = hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()
        if not expected_hash or expected_hash != actual_hash:
            return False

        run_id = str(manifest.get("run_id") or "").strip()
        if not run_id:
            return False

        output_path_text = str(
            manifest.get("output_path") or ""
        ).strip()
        if not output_path_text:
            return False
        output_path = Path(output_path_text)

        if self.app.output_is_used_by_other_task(
            self,
            output_path,
        ):
            messagebox.showwarning(
                APP_TITLE,
                "Нельзя продолжить восстановленную задачу: "
                "другая вкладка уже пишет в тот же MP3.",
            )
            return False

        voice = str(
            manifest.get("voice")
            or self.voice_var.get().strip()
        )
        rate = clamp_int(
            manifest.get("rate"),
            RATE_MIN,
            RATE_MAX,
            self.rate_var.get(),
        )
        pitch = clamp_int(
            manifest.get("pitch"),
            PITCH_MIN,
            PITCH_MAX,
            self.pitch_var.get(),
        )
        volume = clamp_int(
            manifest.get("volume"),
            0,
            100,
            self.volume_var.get(),
        )
        bitrate, channels, sample_rate_hz = read_mp3_format(
            {**manifest, "bitrate": manifest.get("bitrate") or self.app.settings.get("bitrate")},
            sample_rate_default=0,
        )

        # Reflect the exact saved recovery parameters in the UI. Pitch remains
        # global, so all tabs immediately show the same recovered Pitch value.
        self.voice_var.set(voice)
        self.app.global_rate_var.set(rate)
        self.app.global_pitch_var.set(pitch)
        self.volume_var.set(volume)

        tab_title_snapshot = self.tab_title()
        visible_tab_index_snapshot = (
            self.app.visible_tab_index(self)
        )

        if not self.track_mp3_source_snapshot(
            run_id,
            raw_text,
        ):
            self.app.logger.event(
                "mp3_source_snapshot_track_failed",
                task_id=run_id,
                run_id=run_id,
                tab_id=self.workspace_id,
                source_chars=len(raw_text),
                recovery_resume=True,
            )
            return False

        self._launch_mp3_job(
            run_id, raw_text, tab_title_snapshot, visible_tab_index_snapshot, output_path,
            voice, rate, pitch, volume, bitrate, work_dir=work_dir, normalized_text=text,
            channels=channels, sample_rate_hz=sample_rate_hz,
        )
        return True

    def _launch_mp3_job(
        self, run_id: str, raw_text: str, tab_title: str, tab_index: int | None,
        output_path: Path, voice: str, rate: int, pitch: int, volume: int, bitrate: str,
        *, work_dir: Path | None = None, normalized_text: str | None = None,
        channels: int = 0, sample_rate_hz: int = 0,
        approved_output_identity: tuple[int, ...] | None = None,
    ) -> None:
        self.stop_preview(
            preserve_bookmark=True,
            reason="mp3_recovery_started" if work_dir else "mp3_creation_started",
            force_bookmark_save=False,
        )
        self.cancel_event.clear()
        self.current_run_id = run_id
        self.current_output_path = output_path
        self.current_job_stage = "queued"

        self.progress["value"] = 0
        self._set_mp3_controls(active=True)
        self.status_var.set(f"Подготовка текста и резервной копии: {output_path.name}")

        def prepare_and_convert() -> None:
            stage = "prepare_mp3_text"
            try:
                if self.cancel_event.is_set():
                    raise InterruptedError
                text = normalized_text if normalized_text is not None else normalize_text(raw_text)
                if self.cancel_event.is_set():
                    raise InterruptedError
                if not text:
                    self.app.events.put((self.task_id, "job_error", {
                        "run_id": run_id, "user_message": "В тексте нет данных для создания MP3.",
                    }))
                    return
                metrics = analyze_text_for_logging(raw_text, text)
                if self.cancel_event.is_set():
                    raise InterruptedError
                stage = "create_mp3_text_backup"
                self.current_mp3_text_backup_path = create_mp3_text_backup(
                    run_id, tab_title, raw_text,
                )
                self.app.logger.event(
                    "mp3_text_backup_created", task_id=run_id, run_id=run_id,
                    tab_id=self.workspace_id, backup_path=str(self.current_mp3_text_backup_path),
                    source_chars=len(raw_text), source_sha256_16=self.mp3_source_sha256[:16],
                    retention_days=self.app.settings.get("text_backup_retention_days", MP3_TEXT_BACKUP_RETENTION_DAYS),
                    recovery_resume=bool(work_dir),
                )
                if self.cancel_event.is_set():
                    raise InterruptedError
            except InterruptedError:
                self.app.events.put((self.task_id, "cancelled", {
                    "run_id": run_id, "message": "Создание MP3 остановлено. Исходный текст сохранён.",
                }))
                return
            except Exception as exc:
                error_log = self.app.logger.error(
                    task_id=run_id, stage=stage, exc=exc, traceback_text=traceback.format_exc(),
                    context={"source_chars": len(raw_text), "tab_id": self.workspace_id},
                )
                self.app.events.put((self.task_id, "job_error", {
                    "run_id": run_id, "error_log": error_log,
                    "user_message": (
                        "Не удалось создать обязательную резервную копию текста."
                        if stage == "create_mp3_text_backup" else "Не удалось подготовить текст."
                    ),
                }))
                return
            self._job_worker(
                run_id, text, metrics, tab_title, tab_index, output_path, voice, rate, pitch,
                volume, bitrate, work_dir, approved_output_identity=approved_output_identity,
                channels=channels, sample_rate_hz=sample_rate_hz,
            )

        self.worker = threading.Thread(target=prepare_and_convert, name=f"tts-{run_id}", daemon=True)
        self.worker.start()

    def cancel_job(self) -> None:
        if self.worker and self.worker.is_alive():
            self.app.logger.event(
                "task_cancel_requested",
                task_id=self.current_run_id or self.task_id,
                run_id=self.current_run_id,
                tab_id=self.workspace_id,
                stage=self.current_job_stage,
                output_path=(
                    str(self.current_output_path)
                    if self.current_output_path
                    else ""
                ),
            )
            self.cancel_event.set()

            if self.current_job_stage == "ffmpeg":
                self.status_var.set(
                    "Останавливаю FFmpeg и удаляю незавершённый .part.mp3…"
                )
            else:
                self.status_var.set(
                    "Остановка запрошена. Текущий фрагмент будет закончен."
                )

    def set_job_idle(self) -> None:
        self._set_mp3_controls(active=False)
        self.current_output_path = None
        self.current_mp3_text_backup_path = None
        self.current_run_id = None
        self.current_job_stage = "idle"

    def conversion_running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())
