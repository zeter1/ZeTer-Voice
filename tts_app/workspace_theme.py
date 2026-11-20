"""Light appearance for the existing Tk workspace and controls."""
import math
import tkinter as tk
from tkinter import ttk

WHITE = "#ffffff"
SURFACE = "#f7f8fb"
BORDER = "#dce2ed"
INK = "#1b253a"
MUTED = "#4d5971"
ACCENT = "#3542bb"
ACCENT_WASH = "#eef0ff"
FOCUS = "#7581d9"
DISABLED = "#6f7b8e"


def _rounded_image(root, fill, outline, *, radius=8, gap=0, tile=3):
    """Create a small stretchable border. Its transparent corners stay fixed."""
    size = radius * 2 + tile
    image = tk.PhotoImage(master=root, width=size + gap, height=size)
    for y in range(size):
        vertical = max(radius - y, y - (size - radius - 1), 0)
        inset = radius - int(math.sqrt(radius * radius - vertical * vertical))
        image.put(outline, to=(inset, y, size - inset, y + 1))
        if 0 < y < size - 1:
            inner = max(inset + 1, 1)
            if inner * 2 < size:
                image.put(fill, to=(inner, y, size - inner, y + 1))
    return image


def _rename_element(layout, source, target):
    result = []
    for element, options in layout:
        options = dict(options)
        if "children" in options:
            options["children"] = _rename_element(options["children"], source, target)
        if element == source:
            options.pop("border", None)
        result.append((target if element == source else element, options))
    return result


def _switch_image(root, enabled: bool) -> tk.PhotoImage:
    image = tk.PhotoImage(master=root, width=43, height=18)
    track = ACCENT if enabled else "#8995ab"
    for y in range(18):
        dy = y - 8.5
        inset = 9 - int(math.sqrt(81 - dy * dy))
        image.put(track, to=(inset, y, 36 - inset, y + 1))
    center = 26 if enabled else 9
    for y in range(3, 15):
        half = int(math.sqrt(36 - (y - 8.5) ** 2))
        image.put(WHITE, to=(center - half, y, center + half + 1, y + 1))
    return image


def _apply_workspace_elements(root, style):
    images = []

    def element(name, fill, edge, *, states=(), gap=0, radius=8, tile=3):
        border = (radius, radius, radius + gap, radius)
        base = _rounded_image(root, fill, edge, radius=radius, gap=gap, tile=tile)
        variants = []
        for state, color, border_color in states:
            variant = _rounded_image(root, color, border_color, radius=radius, gap=gap, tile=tile)
            variants.append((*state.split(), variant))
            images.append(variant)
        images.append(base)
        size_override = {"width": 1, "height": 1} if tile > 3 else {}
        style.element_create(name, "image", base, *variants, border=border, padding=(1, 1), sticky="nsew", **size_override)

    element("Voice.Card", WHITE, BORDER, radius=10, tile=96)
    style.layout("Card.TFrame", [("Voice.Card", {"sticky": "nsew"})])
    # Full-width contact rows use these button borders. A larger center tile
    # avoids thousands of tiny Win32 blits without changing the corner radius.
    element("Voice.Button.border", WHITE, BORDER, tile=96, states=(
        ("disabled", SURFACE, BORDER), ("focus", WHITE, FOCUS), ("pressed", ACCENT_WASH, FOCUS),
        ("active", "#f4f5ff", "#bbc2ee"),
    ))
    element("Voice.Primary.border", ACCENT_WASH, "#b6beff", states=(
        ("disabled", SURFACE, BORDER), ("focus", ACCENT_WASH, FOCUS),
        ("pressed", "#dce1ff", FOCUS), ("active", "#e4e8ff", "#a4aff3"),
    ))
    element("Voice.Contact.border", "#f8f9ff", "#dce2f3", tile=96, states=(
        ("focus", ACCENT_WASH, FOCUS), ("pressed", "#dce1ff", FOCUS),
        ("active", ACCENT_WASH, "#b6beff"),
    ))
    style.layout("TButton", _rename_element(style.layout("TButton"), "Button.border", "Voice.Button.border"))
    style.layout("Primary.TButton", _rename_element(style.layout("TButton"), "Voice.Button.border", "Voice.Primary.border"))
    style.layout("Contact.TButton", _rename_element(style.layout("TButton"), "Voice.Button.border", "Voice.Contact.border"))
    style.layout("Link.TButton", [
        ("Button.focus", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "w"})]}),
        ]}),
    ])
    style.layout("TMenubutton", _rename_element(style.layout("TMenubutton"), "Menubutton.border", "Voice.Button.border"))
    style.layout("TCombobox", _rename_element(style.layout("TCombobox"), "Combobox.field", "Voice.Button.border"))
    style.layout("TSpinbox", _rename_element(style.layout("TSpinbox"), "Spinbox.field", "Voice.Button.border"))
    element("Voice.Notebook.tab", WHITE, BORDER, gap=7, states=(
        ("selected focus", "#dfe4ff", "#b6beff"),
        ("selected", ACCENT_WASH, "#b6beff"), ("active", "#f4f5ff", "#bbc2ee"),
    ))

    page = tk.PhotoImage(master=root, width=128, height=128)
    page.put(SURFACE, to=(0, 0, 128, 128))
    rule = tk.PhotoImage(master=root, width=8, height=5)
    rule.put(WHITE, to=(0, 0, 8, 5))
    selected = rule.copy()
    selected.put(ACCENT, to=(0, 2, 8, 5))
    separator = tk.PhotoImage(master=root, width=128, height=1)
    separator.put(BORDER, to=(0, 0, 128, 1))
    style.element_create("Voice.Notebook.client", "image", page, width=1, height=1, sticky="nsew")
    style.element_create("Voice.Separator", "image", separator, width=1, sticky="ew")
    style.layout("Horizontal.TSeparator", [("Voice.Separator", {"sticky": "ew"})])
    style.element_create("Voice.Section.tab", "image", rule, ("selected", selected), border=(0, 0, 0, 3), padding=(0, 0, 0, 0), sticky="nsew")
    style.layout("Section.TNotebook", [("Voice.Notebook.client", {"sticky": "nsew"})])
    style.layout("Section.TNotebook.Tab", _rename_element(style.layout("TNotebook.Tab"), "Notebook.tab", "Voice.Section.tab"))

    empty_box = _rounded_image(root, WHITE, "#a7b3c9", radius=3, tile=8, gap=5)
    checked_box = _rounded_image(root, ACCENT, ACCENT, radius=3, tile=8, gap=5)
    for x, y in [(3, 6), (4, 7), (5, 8), (6, 7), (7, 6), (8, 5), (9, 4)]:
        checked_box.put(WHITE, to=(x, y, x + 2, y + 2))
    style.element_create("Voice.Checkbutton.indicator", "image", empty_box, ("selected", checked_box), sticky="")
    style.layout("TCheckbutton", _rename_element(
        style.layout("TCheckbutton"), "Checkbutton.indicator", "Voice.Checkbutton.indicator",
    ))
    off, on = _switch_image(root, False), _switch_image(root, True)
    style.element_create("Voice.Switch.indicator", "image", off, ("selected", on), sticky="")
    style.layout("Switch.Card.TCheckbutton", _rename_element(
        style.layout("TCheckbutton"), "Voice.Checkbutton.indicator", "Voice.Switch.indicator",
    ))

    track = tk.PhotoImage(master=root, width=64, height=3)
    track.put("#dce2f4", to=(0, 0, 64, 3))
    knob = _rounded_image(root, WHITE, ACCENT, radius=6, tile=1)
    active_knob = _rounded_image(root, ACCENT_WASH, ACCENT, radius=6, tile=1)
    style.element_create("Voice.Scale.trough", "image", track, width=4, sticky="ew")
    style.element_create("Voice.Scale.slider", "image", knob, ("active", active_knob), sticky="")
    style.layout("Voice.Horizontal.TScale", [
        ("Horizontal.Scale.focus", {"sticky": "nsew", "children": [
            ("Voice.Scale.trough", {"sticky": "ew"}),
            ("Voice.Scale.slider", {"side": "left", "sticky": ""}),
        ]}),
    ])
    root._workspace_theme_images = (*images, page, rule, selected, separator, empty_box, checked_box,
                                    off, on, track, knob, active_knob)


def apply_workspace_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=SURFACE)
    style.configure(
        ".", font=("Segoe UI", 10), background=SURFACE, foreground=INK,
        bordercolor=BORDER, lightcolor=WHITE, darkcolor=BORDER, focuscolor=FOCUS,
    )
    style.configure("Card.TFrame", background=WHITE, bordercolor=BORDER, relief="solid", borderwidth=1)
    style.configure("Paper.TFrame", background=WHITE)
    style.configure("Card.TLabel", background=WHITE)
    style.configure("Heading.Card.TLabel", font=("Segoe UI", 14, "bold"))
    style.configure("Note.Card.TLabel", foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Warning.Card.TLabel", foreground="#843d00", font=("Segoe UI", 10, "bold"))
    style.configure("Note.TLabel", foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Card.TCheckbutton", background=WHITE)
    style.map("Card.TCheckbutton", background=[("active", WHITE)])
    for control in ("TButton", "TMenubutton"):
        style.configure(control, background=WHITE, padding=(11, 7))
        style.map(control, background=[("pressed", ACCENT_WASH), ("active", "#f0f4fa")])
        style.configure(f"Compact.{control}", padding=(6, 5))
    style.configure("Primary.TButton", background=ACCENT_WASH, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
    style.configure("Plus.TButton", padding=(7, 3), font=("Segoe UI", 15), foreground=MUTED)
    style.configure("Link.TButton", padding=(3, 4), foreground=ACCENT, width=0, anchor="w")
    style.map("Link.TButton", foreground=[("active", "#18268e")])
    style.configure("Contact.TButton", padding=(14, 10), foreground=ACCENT, font=("Segoe UI", 11), width=0, anchor="w")
    style.configure("Social.TButton", padding=(11, 7), foreground=INK, width=0, anchor="w")
    style.map(
        "Primary.TButton", background=[("disabled", SURFACE), ("pressed", "#cfdfff"), ("active", "#dce8ff")],
        foreground=[("disabled", DISABLED), ("!disabled", ACCENT)],
    )
    for control in ("TCombobox", "TSpinbox"):
        style.configure(control, padding=(7, 4), fieldbackground=WHITE, selectbackground=ACCENT_WASH, selectforeground=INK)
        style.map(
            control, fieldbackground=[("disabled", SURFACE), ("readonly", WHITE)], bordercolor=[("focus", FOCUS)],
            selectforeground=[("!disabled", INK)], selectbackground=[("focus", ACCENT_WASH), ("!focus", WHITE)],
        )
    for notebook, padding in (("Section.TNotebook", (8, 12)), ("ClosableNotebook", (12, 8))):
        style.configure(notebook, background=WHITE if notebook.startswith("Section") else SURFACE, borderwidth=0, tabmargins=0)
        style.configure(f"{notebook}.Tab", background=WHITE, padding=padding, font=("Segoe UI", 11))
        style.map(
            f"{notebook}.Tab", background=[("selected", ACCENT_WASH)], foreground=[("selected", ACCENT)],
            padding=[("selected", padding)], expand=[("selected", (0, 0, 0, 0))],
        )
    style.configure("Horizontal.TProgressbar", background=FOCUS, troughcolor=ACCENT_WASH, thickness=6)
    _apply_workspace_elements(root, style)
