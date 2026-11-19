"""Tk text viewing and compact, non-content metrics for large workspaces."""
from __future__ import annotations

import hashlib
import re
import time
from tkinter import messagebox

# Word-wrapping a logical line with millions of chars can stall native Tk.
LONG_LINE_NO_WRAP_THRESHOLD = 20_000
_SENTENCE_END = re.compile(r"(?<![.!?…])[.!?…]+(?=\s|$)")
_SENTENCE_SAMPLE_CHARS = 32_768


def has_long_line(text: str, threshold: int = LONG_LINE_NO_WRAP_THRESHOLD) -> bool:
    """Early return, without copying the text with splitlines()."""
    if len(text) < threshold:
        return False
    start = 0
    while True:
        end = text.find("\n", start)
        if end == -1:
            return len(text) - start >= threshold
        if end - start >= threshold:
            return True
        start = end + 1


def estimate_sentence_count(text: str) -> int:
    chars = len(text)
    width = _SENTENCE_SAMPLE_CHARS
    sampled_chars = 3 * width
    if chars <= sampled_chars:
        count = sum(1 for _ in _SENTENCE_END.finditer(text))
        if chars and not count:
            return sum(1 for line in text.splitlines() if line.strip()) or 1
        return count

    # The queue label is approximate. Bound the regex scan even for many MB
    # of punctuation or frequent appends, and sample across the document.
    starts = (0, (chars - width) // 2, chars - width)
    count = sum(
        1 for start in starts for _ in _SENTENCE_END.finditer(text[start:start + width])
    )
    if count:
        return max(1, round(count * chars / sampled_chars))
    return max(1, text.count("\n") + int(not text.endswith("\n")))


class TaskTextPerformanceMixin:
    def configure_text_view_for_content(self, text: str, *, origin: str) -> None:
        self._text_metrics_cache = None
        self._configure_text_view(has_long_line(text), text_chars=len(text), origin=origin)

    def _prepare_text_insertion(
        self, text: str, index: str, *, origin: str, replace_end: str | None = None,
    ) -> None:
        if not self._word_wrap_var.get() or self._manual_wrap_large_line:
            return
        # A paste can make the receiving line too long even when all of the
        # pasted lines are short. Check both join points before Tk lays out text.
        column = int(index.split(".")[1])
        suffix_index = replace_end or index
        end_column = int(self.text_box.index(f"{suffix_index} lineend").split(".")[1])
        suffix_chars = end_column - int(suffix_index.split(".")[1])
        first_break = text.find("\n")
        if first_break == -1:
            longest_join = column + len(text) + suffix_chars
        else:
            longest_join = max(column + first_break, len(text) - text.rfind("\n") - 1 + suffix_chars)
        if longest_join >= LONG_LINE_NO_WRAP_THRESHOLD or has_long_line(text):
            self._configure_text_view(True, text_chars=len(text), origin=origin)

    def _see_paste_caret(self) -> None:
        line_length = int(self.text_box.index("insert lineend").split(".")[1])
        # Scrolling right to the end of a huge line stalls Windows Tk on redraw,
        # even with wrap disabled. Move the viewport without moving the caret.
        index = "insert linestart" if line_length >= LONG_LINE_NO_WRAP_THRESHOLD else "insert"
        self.text_box.see(index)

    def _configure_text_view(self, large_line: bool, *, text_chars: int, origin: str) -> None:
        self._display_long_line = large_line
        self._word_wrap_auto_disabled = large_line
        self._manual_wrap_large_line = False
        # Set before inserting a huge line to avoid Tk's expensive word layout.
        self._word_wrap_var.set(not large_line)
        self._apply_word_wrap()
        if large_line:
            self.app.logger.event(
                "large_text_view_optimized", task_id=self.task_id,
                tab_id=self.workspace_id, source=origin,
                text_chars=text_chars, threshold_chars=LONG_LINE_NO_WRAP_THRESHOLD,
                wrap="none", user_content_logged=False,
            )

    def _apply_word_wrap(self) -> None:
        wrapping = bool(self._word_wrap_var.get())
        self.text_box.configure(wrap="word" if wrapping else "none")
        if wrapping:
            self.horizontal_scrollbar.pack_forget()
        else:
            self.horizontal_scrollbar.pack(side="bottom", fill="x")

    def _on_word_wrap_changed(self) -> None:
        if self._display_long_line and self._word_wrap_var.get():
            # The article may have shrunk before the throttled metrics update.
            # Reuse that lightweight path so an obsolete warning is not shown.
            if self._word_wrap_auto_disabled:
                self._text_metrics()
        if self._display_long_line and self._word_wrap_var.get():
            if not messagebox.askyesno(
                "Большая строка текста",
                "У этой вкладки очень длинная строка. Включение переноса "
                "может надолго заблокировать интерфейс.\n\n"
                "Всё равно включить перенос?",
                default="no",
            ):
                self._word_wrap_var.set(False)
                self._apply_word_wrap()
                return
        # A user's explicit choice takes priority over automatic restoration.
        self._word_wrap_auto_disabled = False
        self._manual_wrap_large_line = bool(
            self._display_long_line and self._word_wrap_var.get()
        )
        self._apply_word_wrap()
        self.app.logger.event(
            "text_wrap_setting_changed", task_id=self.task_id,
            tab_id=self.workspace_id,
            wrap_enabled=bool(self._word_wrap_var.get()),
            large_line_detected=bool(self._display_long_line),
        )

    def _text_metrics(self, *, include_hash: bool = False) -> dict:
        cache = self._text_metrics_cache
        if cache is None or (include_hash and "sha16" not in cache):
            # A single retrieval per modified version. Cache numbers/hashes, not text.
            text = self._full_text()
            chars = len(text)
            count = estimate_sentence_count(text)
            cache = {"chars": chars, "sentences": count}
            if include_hash:
                cache["sha16"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            self._text_metrics_cache = cache
            # When read sentences are deleted (or a large selection replaced),
            # the originally unsafe logical line may no longer exist. Restore
            # legible wrapping without changing a single character in the data.
            # We already fetched the text for metrics, so this adds no Tk read.
            if self._word_wrap_auto_disabled and not has_long_line(text):
                self._word_wrap_auto_disabled = False
                self._display_long_line = False
                self._word_wrap_var.set(True)
                self._apply_word_wrap()
                self.app.logger.event(
                    "large_text_wrap_auto_restored", task_id=self.task_id,
                    tab_id=self.workspace_id, text_chars=chars,
                    threshold_chars=LONG_LINE_NO_WRAP_THRESHOLD,
                    source="text_metrics", user_content_logged=False,
                )
        return cache

    def _text_changed_performance(self) -> None:
        self._text_metrics_cache = None
        # Re-evaluate the physical line only for genuinely huge one-line edits.
        # Tcl index returns logical line number and column without copying text.
        if self._word_wrap_var.get() and not self._manual_wrap_large_line:
            try:
                line, column = (int(n) for n in self.text_box.index("end-1c").split("."))
                if column >= LONG_LINE_NO_WRAP_THRESHOLD:
                    self._word_wrap_var.set(False)
                    self._display_long_line = True
                    self._word_wrap_auto_disabled = True
                    self._apply_word_wrap()
                    self.app.logger.event(
                        "large_text_wrap_auto_disabled", task_id=self.task_id,
                        tab_id=self.workspace_id, line_chars_at_end=column,
                    )
            except (ValueError, RuntimeError):
                pass
