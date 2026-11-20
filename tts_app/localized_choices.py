"""Keep locale-specific labels out of saved voice, device, and hotkey values."""
import tkinter as tk
from tkinter import ttk
from collections.abc import Callable


class LocalizedCombobox(ttk.Combobox):
    def __init__(
        self, parent, variable: tk.StringVar, choices, label: Callable[[str], str], *,
        editable: bool = False, **kwargs,
    ) -> None:
        self._value = variable
        self._label = label
        self.display = tk.StringVar(master=parent)
        super().__init__(
            parent, textvariable=self.display, state="normal" if editable else "readonly", **kwargs,
        )
        self.set_choices(choices)
        self._trace = variable.trace_add("write", self._sync_display)
        self.bind("<Destroy>", self._remove_trace, add="+")
        if not editable:
            self.bind("<<ComboboxSelected>>", self.commit)

    def set_choices(self, choices) -> None:
        self._choices = tuple(choices)
        self["values"] = tuple(map(self._label, self._choices))
        self._sync_display()

    def commit(self, _event=None) -> None:
        index = self.current()
        self._value.set(self._choices[index] if index >= 0 else self.display.get())

    def _sync_display(self, *_args) -> None:
        self.display.set(self._label(self._value.get()))

    def _remove_trace(self, _event) -> None:
        self._value.trace_remove("write", self._trace)
