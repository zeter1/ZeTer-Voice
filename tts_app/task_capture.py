from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .ui_stall_diagnostics import ui_operation

class TaskCaptureMixin:
    def _schedule_capture_persistence(self) -> None:
        # Coalesce bursts of global hotkey events rather than blocking Tk.
        # The ordinary shutdown path flushes any outstanding workspace save.
        self.app.schedule_workspace_save(delay_ms=500, keep_earliest=True)

    def capture_external_selection(self) -> None:
        """Capture selected text from the last active external Windows app."""
        self.app.capture_selected_text_for_tab(self)

    def _append_separator(self, text: str, end_index: str) -> str:
        if text.startswith(("\n", "\r")):
            return ""
        tail = self.text_box.get(f"{end_index}-1c", end_index)
        return "\n" if tail and tail not in ("\n", "\r") else ""

    def append_external_selected_text(self, captured_text: str) -> bool:
        """
        Append externally selected text to this tab and make it part of reading.
        """
        if not isinstance(captured_text, str):
            return False

        if not captured_text or captured_text.isspace():
            messagebox.showwarning(
                APP_TITLE,
                "В выделении нет текста для чтения.",
            )
            return False
        if self.text_box.cget("state") != "normal":
            return False

        old_end_index = self.text_box.index("end-1c")
        separator = self._append_separator(captured_text, old_end_index)
        added_text = separator + captured_text
        # Keep treating the MP3 task as active until its final GUI event is
        # handled. The worker may finish a fraction of a second before Tk
        # processes "done"/"cancelled"/"job_error"; text captured in that
        # window must still remain outside the just-finished snapshot.
        during_mp3_conversion = bool(self.current_run_id)
        appended_at_mp3_source_end = False
        if (
            during_mp3_conversion
            and self.mp3_source_run_id
            and self.mp3_source_run_id == self.current_run_id
        ):
            try:
                appended_at_mp3_source_end = self.text_box.compare(
                    self.mp3_source_end_mark,
                    "==",
                    old_end_index,
                )
            except tk.TclError:
                appended_at_mp3_source_end = False

        live_preview = bool(
            self.preview_thread and self.preview_thread.is_alive()
        )
        saved_pause = bool(
            not during_mp3_conversion
            and self.preview_is_paused
            and self._bookmark_is_valid()
        )
        auto_read_requested = bool(
            self.auto_read_hotkey_var.get()
        )
        auto_read = auto_read_requested and any(ch.isalnum() for ch in captured_text)
        old_chars = new_chars = None
        if not during_mp3_conversion and auto_read and (live_preview or saved_pause):
            # Preview ranges use Python character offsets. Tk 8.6's count
            # reports supplementary characters as two units.
            with ui_operation(self.app, "capture_old_text_snapshot"):
                old_chars = len(self._full_text())
            new_chars = old_chars + len(added_text)

        self._prepare_text_insertion(added_text, old_end_index, origin="external_capture")
        with ui_operation(self.app, "append_captured_text", added_chars=len(added_text)):
            self.text_box.insert("end-1c", added_text)
        self._text_metrics_cache = None
        if appended_at_mp3_source_end and separator == "\n":
            self.mp3_source_separator_pending = True

        if during_mp3_conversion:
            self.status_var.set(
                "Текст добавлен вниз. Он сохранён для следующего MP3 "
                "и не входит в текущую конвертацию."
            )

        elif auto_read_requested and not auto_read:
            self.status_var.set("Текст добавлен вниз.")

        elif live_preview and auto_read_requested:
            if not self.current_preview_follow_end:
                try:
                    self.text_box.mark_set(
                        self.preview_snapshot_end_mark,
                        old_end_index,
                    )
                    self.text_box.mark_gravity(
                        self.preview_snapshot_end_mark,
                        "left",
                    )
                except tk.TclError:
                    pass

            self.current_preview_follow_end = True
            self.current_preview_range_end = new_chars
            self.status_var.set(
                "Выделенный текст добавлен вниз. "
                "Текущее чтение не прерывается; новый текст будет прочитан следом."
            )

        elif live_preview:
            self.current_preview_follow_end = False
            self.status_var.set(
                "Текст добавлен вниз. Текущее чтение продолжается, "
                "но новый текст оставлен для ручного запуска."
            )

        elif saved_pause and auto_read_requested:
            if isinstance(self.preview_bookmark, dict):
                bookmark = self.preview_bookmark
                saved_chars = max(
                    0,
                    int(bookmark.get("text_chars") or old_chars),
                )
                bookmark["range_was_to_end"] = True
                bookmark["end_offset"] = new_chars
                bookmark["last_append_at"] = now_iso()
                bookmark["appended_chars_after_pause"] = max(
                    0,
                    new_chars - saved_chars,
                )

            self.status_var.set(
                "Выделенный текст добавлен вниз. "
                "Пауза сохранена — нажмите «Продолжить»."
            )

        elif saved_pause:
            self.status_var.set(
                "Текст добавлен вниз. Пауза сохранена; новый текст "
                "оставлен для ручного запуска."
            )

        elif auto_read_requested:
            captured_start_index = self.text_box.index(
                f"{old_end_index}+{len(separator)}c"
            )
            self._start_preview_worker(
                raw_text=captured_text,
                preview_base_index=captured_start_index,
                source_mode="external_capture",
                selected_text=False,
            )
        else:
            self.status_var.set(
                "Текст добавлен вниз. Автоматическое чтение отключено."
            )

        self.session_captured_chars += len(captured_text)
        self.workspace_dirty = True
        self._schedule_capture_persistence()
        self.schedule_queue_status_refresh(text_state_reason="external_selection_appended")

        self.app.logger.event(
            "external_selection_appended",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            preview_run_id=self.current_preview_run_id,
            captured_chars=len(captured_text),
            inserted_chars=len(added_text),
            old_text_chars=old_chars,
            new_text_chars=new_chars,
            during_mp3_conversion=during_mp3_conversion,
            included_in_current_mp3=False,
            live_preview=live_preview,
            paused=saved_pause,
            auto_read_requested=auto_read_requested,
            delete_read_text=bool(self.delete_read_text_var.get()),
        )
        return True

    def paste_text_from_editor(self, _event=None) -> str:
        """Handle the editor's <<Paste>> with its normal selection and undo rules."""
        if self.text_box.cget("state") != "normal":
            self._log_paste_rejected("editor_paste", "validate_target", "editor_disabled")
            return "break"
        try:
            text = self.app.root.clipboard_get()
        except tk.TclError:
            self._log_paste_rejected("editor_paste", "clipboard_read", "clipboard_unavailable")
            return "break"
        if not text:
            self._log_paste_rejected("editor_paste", "clipboard_read", "clipboard_empty")
            return "break"
        try:
            replaced = self._paste_at_cursor(text, origin="editor_paste")
        except (tk.TclError, ValueError, TypeError) as exc:
            self._log_paste_rejected("editor_paste", "insert", "paste_exception", type(exc).__name__)
            raise
        self.app.logger.event(
            "clipboard_text_pasted", task_id=self.task_id, tab_id=self.workspace_id,
            source="editor_paste", chars=len(text), replaced_selection=replaced,
        )
        return "break"

    def _log_paste_rejected(self, source, stage, reason, exception_type=None):
        self.app.logger.event(
            "text_paste_rejected", task_id=self.task_id, tab_id=self.workspace_id,
            source=source, stage=stage, reason=reason, exception_type=exception_type,
        )

    def _paste_at_cursor(self, text: str, *, origin: str) -> bool:
        box = self.text_box
        selection = box.tag_ranges("sel")
        if selection:
            start, end = str(selection[0]), str(selection[-1])
        else:
            start = end = box.index("insert")

        self._prepare_text_insertion(text, start, origin=origin, replace_end=end)
        box.mark_set("insert", start)
        with ui_operation(self.app, "paste_clipboard_text", added_chars=len(text), source=origin):
            # Tk's replace keeps deletion and insertion together in undo. A
            # separator on each side also keeps it apart from ordinary typing.
            separate = box.tk.getboolean(box.cget("autoseparators"))
            if separate:
                box.edit_separator()
            box.replace(start, end, text)
            if separate:
                box.edit_separator()
        self._text_metrics_cache = None
        return bool(selection)

    def paste_text_from_clipboard(self) -> None:
        """
        Paste Unicode text from the clipboard.

        Normal state:
            behaves like ordinary Ctrl+V at the caret/selection.

        While text is being read OR while a saved listening pause is active:
            appends the clipboard text to the very end of the tab and preserves
            the current reading position. The live SAPI stream is not stopped.
            When the original snapshot reaches its end, the newly appended tail
            is picked up automatically and reading continues.
        """
        if self.worker and self.worker.is_alive():
            self._log_paste_rejected("paste_button", "validate_target", "mp3_running")
            messagebox.showwarning(
                APP_TITLE,
                "Нельзя изменять текст, пока эта вкладка создаёт MP3.",
            )
            return
        if self.text_box.cget("state") != "normal":
            self._log_paste_rejected("paste_button", "validate_target", "editor_disabled")
            return

        try:
            clipboard_text = self.app.root.clipboard_get()
        except tk.TclError:
            self._log_paste_rejected("paste_button", "clipboard_read", "clipboard_unavailable")
            messagebox.showwarning(
                APP_TITLE,
                "В буфере обмена нет текста для вставки.",
            )
            return

        if not isinstance(clipboard_text, str) or not clipboard_text:
            self._log_paste_rejected("paste_button", "clipboard_read", "clipboard_empty")
            messagebox.showwarning(
                APP_TITLE,
                "В буфере обмена нет текста для вставки.",
            )
            return

        live_preview = bool(
            self.preview_thread
            and self.preview_thread.is_alive()
        )
        saved_pause = bool(
            self.preview_is_paused
            and self._bookmark_is_valid()
        )
        append_to_end = bool(live_preview or saved_pause)

        try:
            if append_to_end:
                # "Докинуть вниз": keep the appended block visually separate.
                old_end_index = self.text_box.index("end-1c")
                separator = self._append_separator(clipboard_text, old_end_index)
                added_text = separator + clipboard_text
                old_chars = new_chars = None
                if self.current_preview_follow_end or isinstance(self.preview_bookmark, dict):
                    with ui_operation(self.app, "paste_old_text_snapshot"):
                        old_chars = len(self._full_text())
                    new_chars = old_chars + len(added_text)
                self._prepare_text_insertion(added_text, old_end_index, origin="clipboard_append")
                with ui_operation(self.app, "append_clipboard_text", added_chars=len(added_text)):
                    self.text_box.insert("end-1c", added_text)

                self._text_metrics_cache = None

                # A running whole-text/cursor/bookmark preview must ultimately
                # reach the new bottom as well. The current SAPI sentence is
                # untouched and continues normally.
                if live_preview and self.current_preview_follow_end:
                    self.current_preview_range_end = new_chars

                # A persisted pause can remain valid after an append because all
                # offsets before the old end are unchanged. If its range was
                # intended to reach the end, extend that range to the new end.
                if isinstance(self.preview_bookmark, dict):
                    bookmark = self.preview_bookmark
                    saved_chars = max(
                        0,
                        int(bookmark.get("text_chars") or old_chars),
                    )
                    old_end = max(
                        0,
                        int(bookmark.get("end_offset") or saved_chars),
                    )
                    range_was_to_end = bool(
                        bookmark.get(
                            "range_was_to_end",
                            old_end >= saved_chars,
                        )
                    )
                    bookmark["range_was_to_end"] = range_was_to_end
                    if range_was_to_end:
                        bookmark["end_offset"] = new_chars
                    bookmark["last_append_at"] = now_iso()
                    bookmark["appended_chars_after_pause"] = max(
                        0,
                        new_chars - saved_chars,
                    )

                self.workspace_dirty = True
                self._schedule_capture_persistence()
                self.schedule_queue_status_refresh(text_state_reason="clipboard_append")

                if saved_pause:
                    self.status_var.set(
                        "Текст добавлен в конец. Пауза сохранена — "
                        "нажмите «Продолжить»."
                    )
                elif live_preview:
                    self.status_var.set(
                        "Текст добавлен в конец. Чтение продолжается."
                    )

                self.app.logger.event(
                    "clipboard_text_appended_during_preview",
                    task_id=self.task_id,
                    tab_id=self.workspace_id,
                    source="paste_button",
                    preview_run_id=self.current_preview_run_id,
                    clipboard_chars=len(clipboard_text),
                    inserted_chars=len(added_text),
                    old_text_chars=old_chars,
                    new_text_chars=new_chars,
                    paused=bool(
                        self.preview_pause_event.is_set()
                        or saved_pause
                    ),
                    follow_end=bool(self.current_preview_follow_end),
                )
                return

            # Normal editor paste when no listening session/bookmark is active.
            self.clear_saved_preview_bookmark(
                reason="clipboard_paste",
                schedule_save=False,
            )

            had_selection = self._paste_at_cursor(clipboard_text, origin="clipboard_paste")
            self._see_paste_caret()

            self.workspace_dirty = True
            self.app.schedule_workspace_save()
            self.schedule_queue_status_refresh(text_state_reason="clipboard_paste")

            self.status_var.set(
                "Вставлено из буфера обмена: "
                f"{len(clipboard_text):,} символов.".replace(",", " ")
            )

            self.app.logger.event(
                "clipboard_text_pasted",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                source="paste_button",
                chars=len(clipboard_text),
                replaced_selection=had_selection,
            )

        except (tk.TclError, TypeError, ValueError) as exc:
            self._log_paste_rejected("paste_button", "insert", "paste_exception", type(exc).__name__)
            self.app.log_ui_error(
                task_id=self.task_id,
                stage="paste_clipboard_text",
                exc=exc,
                context={
                    "clipboard_chars": len(clipboard_text),
                    "append_to_end": append_to_end,
                },
            )
            messagebox.showerror(
                APP_TITLE,
                "Не удалось вставить текст из буфера обмена.\n\n"
                f"{exc}",
            )
