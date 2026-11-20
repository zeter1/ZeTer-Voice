from __future__ import annotations

from .i18n import tr

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .ui_stall_diagnostics import ui_operation

class TaskFileIoMixin:
    def _can_load_file(self) -> bool:
        if self.app.shutdown_in_progress or self.app.tabs.get(self.task_id) is not self:
            return False
        if self.current_preview_run_id or self.current_run_id:
            messagebox.showwarning(
                APP_TITLE,
                tr("Сначала остановите чтение или создание MP3 в этой вкладке."),
                parent=self.frame.winfo_toplevel(),
            )
            return False
        return True

    def load_file(self) -> None:
        if not self._can_load_file():
            return
        kwargs = {}
        last_dir = self.app.settings.get("last_input_dir")
        if last_dir and Path(last_dir).is_dir():
            kwargs["initialdir"] = last_dir

        path = filedialog.askopenfilename(
            parent=self.frame.winfo_toplevel(),
            title=tr("Выберите текстовый файл"),
            filetypes=[
                (tr("Текстовые файлы"), ("*.txt", "*.md")),
                (tr("Все файлы"), "*.*"),
            ],
            **kwargs,
        )
        if not path or not self._can_load_file():
            return

        src = Path(path)

        try:
            text = read_text_file(src)
        except Exception as exc:
            self.app.log_ui_error(
                task_id=self.task_id,
                stage="load_text_file",
                exc=exc,
                context={"input_path": str(src)},
            )
            messagebox.showerror(
                APP_TITLE,
                tr("Не удалось прочитать файл:\n{0}", exc),
            )
            return

        if not self.app._archive_tab_before_overwrite(self, "load_file"):
            return
        self.clear_saved_preview_bookmark(
            reason="load_new_text",
            schedule_save=False,
        )
        self.configure_text_view_for_content(text, origin="file_load")
        with ui_operation(self.app, "load_file_into_text", text_chars=len(text)):
            self.text_box.delete("1.0", "end")
            self.text_box.insert("1.0", text)
        self.text_box.edit_modified(False)
        self._text_metrics_cache = None
        self.workspace_dirty = True
        self.schedule_queue_status_refresh()

        self.app.settings["last_input_dir"] = str(src.parent)
        # A filename is a meaningful user-facing title and must not be overwritten
        # by automatic renumbering.
        self.set_tab_title(src.stem, custom=True)
        self.app.renumber_default_tabs()
        self.status_var.set(
            tr("Загружено: {0} — {1:,} символов.", src.name, len(text)).replace(",", " ")
        )
        self.app.schedule_settings_save(self)
        self.app.schedule_workspace_save()

    def clear_text(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(
                APP_TITLE,
                tr("Нельзя очистить текст, пока эта вкладка создаёт MP3."),
            )
            return

        if not self.app._archive_tab_before_overwrite(self, "clear_text"):
            return
        self.stop_preview(
            preserve_bookmark=False,
            reason="clear_text",
        )
        self.clear_saved_preview_bookmark(
            reason="clear_text",
            schedule_save=False,
        )
        self.text_box.delete("1.0", "end")
        self._text_metrics_cache = None
        self.workspace_dirty = True
        self.status_var.set(tr("Текст очищен."))
        self.schedule_queue_status_refresh()
        self.log_text_state(
            "clear_text",
            force=True,
        )
        self.app.schedule_workspace_save()

    def _full_text(self) -> str:
        return self.text_box.get("1.0", "end-1c")
