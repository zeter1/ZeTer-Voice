from __future__ import annotations

from concurrent.futures import Future

from .runtime import *
from .runtime_storage import _is_transient_atomic_replace_error
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *
from .ui_stall_diagnostics import ui_operation

class AppWorkspaceSaveMixin:
    _workspace_write = None
    _workspace_write_poll_id = None
    _workspace_save_again = False

    def schedule_workspace_save(
        self,
        delay_ms: int = 1000,
        *,
        keep_earliest: bool = True,
    ) -> None:
        delay_ms = max(0, int(delay_ms))
        due_perf = time.perf_counter() + delay_ms / 1000.0
        if (
            self.workspace_after_id
            and keep_earliest
            and self.workspace_save_due_perf is not None
            and self.workspace_save_due_perf <= due_perf
        ):
            return

        self._cancel_scheduled_workspace_save()
        self.workspace_save_due_perf = due_perf
        self.workspace_after_id = self.root.after(delay_ms, self._start_workspace_autosave)

    def _cancel_scheduled_workspace_save(self) -> None:
        if self.workspace_after_id:
            try:
                self.root.after_cancel(self.workspace_after_id)
            except tk.TclError:
                pass
        self.workspace_after_id = None
        self.workspace_save_due_perf = None

    def _workspace_snapshot(self) -> tuple[dict, dict]:
        if getattr(self, "_workspace_manifest_needs_read", False):
            # Writing a fresh manifest before rereading the old one would hide
            # all of its text files. Keep the old entries alongside new tabs.
            self._unread_workspace_tabs = read_workspace_manifest(WORKSPACE_FILE)["tabs"]
            self._workspace_manifest_needs_read = False
        tasks_by_frame = {str(task.frame): task for task in self.tabs.values()}
        tasks = [tasks_by_frame[name] for name in self.notebook.tabs() if name in tasks_by_frame]
        records = []
        texts = {}
        for task in tasks:
            text_path = WORKSPACE_TEXT_DIR / f"{task.workspace_id}.txt"
            if task.workspace_dirty or task.text_box.edit_modified() or not text_path.exists():
                texts[task.workspace_id] = text_path, task.text_box.get("1.0", "end-1c")
            records.append(task.workspace_record())
        try:
            selected_index = self.notebook.index(self.notebook.select())
        except tk.TclError:
            selected_index = 0
        manifest = {
            "schema": 3,
            "saved_at": now_iso(),
            "selected_index": selected_index,
            "tabs": records + getattr(self, "_unread_workspace_tabs", []),
        }
        # An edit during the write sets dirty again. Completion never clears it.
        for task in tasks:
            if task.workspace_id in texts:
                task.workspace_dirty = False
        return manifest, texts

    @staticmethod
    def _write_workspace_snapshot(manifest: dict, texts: dict) -> tuple[int, int, float]:
        started = time.perf_counter()
        retries = 0
        written = 0
        WORKSPACE_TEXT_DIR.mkdir(parents=True, exist_ok=True)
        for path, text in texts.values():
            retries += atomic_write_text(path, text)
            written += len(text.encode("utf-8"))
        retries += atomic_write_json(WORKSPACE_FILE, manifest)
        return retries, written, time.perf_counter() - started

    def _start_workspace_autosave(self) -> None:
        self.workspace_after_id = None
        self.workspace_save_due_perf = None
        if self.shutdown_in_progress:
            return
        if self._workspace_write is not None:
            self._workspace_save_again = True
            return

        texts = {}
        try:
            with ui_operation(self, "workspace_snapshot", tabs=len(self.tabs)):
                manifest, texts = self._workspace_snapshot()
            result = Future()

            def write() -> None:
                try:
                    result.set_result(self._write_workspace_snapshot(manifest, texts))
                except Exception as exc:
                    result.set_exception(exc)

            thread = threading.Thread(target=write, name="workspace-save", daemon=True)
            thread.start()
            self._workspace_write = result, manifest, texts
        except Exception as exc:
            self._workspace_save_error(texts, exc)
            return
        self._workspace_write_poll_id = self.root.after(50, self._poll_workspace_write)

    def _poll_workspace_write(self) -> None:
        self._workspace_write_poll_id = None
        state = self._workspace_write
        if state is None:
            return
        if not state[0].done():
            self._workspace_write_poll_id = self.root.after(50, self._poll_workspace_write)
            return
        self._finish_workspace_write()
        if self._workspace_save_again and not self.shutdown_in_progress:
            self._workspace_save_again = False
            self.schedule_workspace_save(0, keep_earliest=True)

    def _finish_workspace_write(self) -> None:
        if self._workspace_write is None:
            return
        if self._workspace_write_poll_id is not None:
            self.root.after_cancel(self._workspace_write_poll_id)
            self._workspace_write_poll_id = None
        result, manifest, texts = self._workspace_write
        self._workspace_write = None
        try:
            self._workspace_save_success(manifest, texts, result.result())
        except Exception as exc:
            self._workspace_save_error(texts, exc)

    def save_workspace(self) -> bool:
        # Explicit callers require a committed workspace. Serialize them with
        # the autosave before taking the final, up-to-date Tk snapshot.
        with ui_operation(self, "workspace_save", tabs=len(self.tabs)):
            self._finish_workspace_write()
            self._cancel_scheduled_workspace_save()
            self._workspace_save_again = False
            texts = {}
            try:
                manifest, texts = self._workspace_snapshot()
                outcome = self._write_workspace_snapshot(manifest, texts)
                self._workspace_save_success(manifest, texts, outcome)
                return True
            except Exception as exc:
                self._workspace_save_error(texts, exc)
                return False

    def _workspace_save_success(self, manifest: dict, texts: dict, outcome: tuple) -> None:
        retries, written, elapsed = outcome
        self.workspace_last_saved_perf = time.perf_counter()
        failures = self.workspace_save_failure_count
        self.workspace_save_failure_count = 0
        if retries or failures:
            self.logger.event(
                "workspace_save_lock_recovered",
                atomic_replace_retries=retries,
                failures_before_success=failures,
                dirty_tabs_written=len(texts),
            )
        if elapsed >= WORKSPACE_SLOW_SAVE_SEC:
            self.logger.event(
                "workspace_save_slow",
                duration_ms=round(elapsed * 1000.0, 1),
                dirty_tabs_written=len(texts),
                text_bytes_written=written,
                tabs_total=len(manifest["tabs"]),
            )

    def _workspace_save_error(self, texts: dict, exc: Exception) -> None:
        for task in self.tabs.values():
            if task.workspace_id in texts:
                task.workspace_dirty = True
        retry_delay_ms = None
        if (
            _is_transient_atomic_replace_error(exc)
            and not self.shutdown_in_progress
            and self.workspace_save_failure_count < len(WORKSPACE_SAVE_RETRY_DELAYS_MS)
        ):
            retry_delay_ms = WORKSPACE_SAVE_RETRY_DELAYS_MS[self.workspace_save_failure_count]
            self.workspace_save_failure_count += 1
            self.schedule_workspace_save(retry_delay_ms, keep_earliest=True)
        self.logger.error(
            task_id="app",
            stage="save_workspace",
            exc=exc,
            traceback_text=traceback.format_exc(),
            context={
                "workspace_file": str(WORKSPACE_FILE),
                "tabs_count": len(self.tabs),
                "winerror": getattr(exc, "winerror", None),
                "retry_scheduled": retry_delay_ms is not None,
                "retry_delay_ms": retry_delay_ms,
            },
        )

    def _task_by_workspace_id(
        self,
        workspace_id: str,
    ) -> TaskTab | None:
        for task in self.tabs.values():
            if task.workspace_id == workspace_id:
                return task
        return None
