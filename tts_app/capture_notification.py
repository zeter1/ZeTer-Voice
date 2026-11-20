"""Native ZeTer Voice notification for a failed external text capture."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from .i18n import tr


SURFACE = "#ffffff"
INK = "#162238"
MUTED = "#52627b"
LINE = "#d3dce9"
INSET = "#eff4fa"
AMBER = "#784710"
AMBER_WASH = "#fff0d2"
CORNER_KEY = "#ff00ff"


class CaptureNotificationCard:
    def __init__(self, window: tk.Toplevel, close) -> None:
        self.window = window
        style = ttk.Style(window)
        if os.name == "nt" and "Voice.Card" in style.element_names():
            window.configure(background=CORNER_KEY)
            window.attributes("-transparentcolor", CORNER_KEY)

        for name, background, color, font in (
            ("CaptureBrand", SURFACE, MUTED, ("Segoe UI", 9, "bold")),
            ("CaptureTitle", SURFACE, INK, ("Segoe UI", 14, "bold")),
            ("CaptureReason", SURFACE, INK, ("Segoe UI", 10, "bold")),
            ("CaptureAdvice", INSET, INK, ("Segoe UI", 10)),
            ("CaptureKey", INSET, INK, ("Segoe UI", 9, "bold")),
            ("CaptureSource", SURFACE, MUTED, ("Segoe UI", 9)),
            ("CaptureCount", SURFACE, AMBER, ("Segoe UI", 9, "bold")),
        ):
            style.configure(f"{name}.TLabel", background=background, foreground=color, font=font)
        style.configure("Capture.Link.TButton", padding=(5, 1), width=0, anchor="center",
                        foreground=MUTED, font=("Segoe UI", 15))

        card = ttk.Frame(window, style="Card.TFrame", padding=(16, 13))
        card.pack(fill="both", expand=True)
        card.columnconfigure(0, weight=1)

        header = tk.Frame(card, background=SURFACE)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        self._warning_icon(header).grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 10))
        ttk.Label(header, text="ZeTer Voice", style="CaptureBrand.TLabel").grid(row=0, column=1, sticky="w")
        self.heading = ttk.Label(header, style="CaptureTitle.TLabel", justify="left")
        self.heading.grid(row=1, column=1, sticky="w")
        ttk.Button(header, text="×", style="Capture.Link.TButton", command=close).grid(
            row=0, column=2, rowspan=2, sticky="n", padx=(6, 0), ipadx=3,
        )

        self.reason = ttk.Label(card, style="CaptureReason.TLabel", justify="left")
        self.reason.grid(row=1, column=0, sticky="ew", pady=(11, 7))
        self.advice_row = tk.Frame(card, background=INSET, padx=9, pady=7)
        self.advice_row.grid(row=2, column=0, sticky="ew")
        self.advice_row.columnconfigure(1, weight=1)
        ttk.Label(self.advice_row, text="↳", style="CaptureAdvice.TLabel").grid(
            row=0, column=0, sticky="nw", padx=(0, 7),
        )
        self.advice = ttk.Label(self.advice_row, style="CaptureAdvice.TLabel", justify="left")
        self.advice.grid(row=0, column=1, sticky="ew")

        tk.Frame(card, height=1, background=LINE).grid(row=3, column=0, sticky="ew", pady=(11, 7))
        footer = tk.Frame(card, background=SURFACE)
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)
        self.hotkey = ttk.Label(footer, style="CaptureKey.TLabel", padding=(5, 2))
        self.hotkey.grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.source = ttk.Label(footer, style="CaptureSource.TLabel", justify="left")
        self.source.grid(row=0, column=1, sticky="w")
        self.count = ttk.Label(footer, style="CaptureCount.TLabel")
        self.count.grid(row=0, column=2, sticky="e", padx=(6, 0))

    @staticmethod
    def _warning_icon(parent) -> tk.Canvas:
        icon = tk.Canvas(parent, width=39, height=39, highlightthickness=0, background=SURFACE)
        icon.create_polygon(
            0, 9, 0, 0, 9, 0, 30, 0, 39, 0, 39, 9, 39, 30, 39, 39, 30, 39, 9, 39, 0, 39, 0, 30,
            smooth=True, fill=AMBER_WASH,
        )
        icon.create_rectangle(14, 12, 25, 28, outline=AMBER, width=1)
        icon.create_rectangle(17, 10, 22, 14, outline=AMBER, fill=AMBER_WASH)
        icon.create_line(17, 18, 22, 23, fill=AMBER)
        icon.create_line(22, 18, 17, 23, fill=AMBER)
        return icon

    def update(self, hotkey: str, message: str, width: int, count: int, *,
               source_process: str, insertion_uncertain: bool) -> None:
        self.window.title(tr("Сбой захвата текста ({0})", hotkey))
        self.heading.configure(text=tr(
            "Сбой добавления текста" if insertion_uncertain else "Текст не скопирован",
        ), wraplength=max(100, width - 122))
        # Capture guidance uses one sentence for the reason followed by the action.
        reason, separator, advice = message.partition(". ")
        self.reason.configure(text=reason + ("." if separator else ""), wraplength=max(100, width - 38))
        if advice:
            self.advice.configure(text=advice, wraplength=max(75, width - 77))
            self.advice_row.grid()
        else:
            self.advice_row.grid_remove()

        name = tr("Блокнот") if source_process.lower() == "notepad.exe" else source_process
        self.hotkey.configure(text=hotkey)
        self.source.configure(text=name if len(name) <= 64 else name[:63] + "…", wraplength=max(50, width // 3))
        self.count.configure(text=tr("Сбоев: {0}", count))
