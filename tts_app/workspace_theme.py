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


def _rounded_image(root, fill, outline, *, radius=8, gap=0, tile=3, background=WHITE, width=None):
    """Blend fixed corners into the surface beneath the widget."""
    height = radius * 2 + tile
    width = width or height
    image = tk.PhotoImage(master=root, width=width + gap, height=height)
    image.put(background, to=(0, 0, width + gap, height))
    image.put(outline, to=(0, 0, width, height))
    image.put(fill, to=(1, 1, width - 1, height - 1))
    colors = [bytes.fromhex(color[1:]) for color in (background, outline, fill)]
    for y in range(radius):
        row = []
        for x in range(radius):
            depth = radius + 0.5 - math.hypot(radius - x - 0.5, radius - y - 0.5)
            outer = min(1, max(0, depth))
            inner = max(0, min(1, depth - 1))
            rgb = bytes(round(bg * (1 - outer) + edge * (outer - inner) + face * inner)
                        for bg, edge, face in zip(*colors))
            row.append("#" + rgb.hex())
        for line in (y, height - 1 - y):
            image.put("{" + " ".join(row) + "}", to=(0, line))
            image.put("{" + " ".join(reversed(row)) + "}", to=(width - radius, line))
    return image


def _switch_image(root, enabled: bool) -> tk.PhotoImage:
    track = ACCENT if enabled else "#8995ab"
    image = _rounded_image(root, track, track, radius=9, tile=0, width=36, gap=7)
    thumb = _rounded_image(root, WHITE, WHITE, radius=6, tile=0, background=track)
    image.tk.call(image, "copy", thumb, "-to", 20 if enabled else 3, 3)
    return image


def _apply_workspace_elements(root, style):
    images = []

    def element(name, fill, edge, *, states=(), gap=0, radius=8, tile=3, background=WHITE):
        border = (radius, radius, radius + gap, radius)
        base = _rounded_image(root, fill, edge, radius=radius, gap=gap, tile=tile, background=background)
        variants = []
        for state, color, border_color in states:
            variant = _rounded_image(root, color, border_color, radius=radius, gap=gap, tile=tile, background=background)
            variants.append((*state.split(), variant))
            images.append(variant)
        images.append(base)
        size_override = {"width": 1, "height": 1} if tile > 3 else {}
        style.element_create(name, "image", base, *variants, border=border, padding=(1, 1), sticky="nsew", **size_override)

    element("Voice.Card", WHITE, BORDER, radius=10, tile=96, background=SURFACE)
    style.layout("Card.TFrame", [("Voice.Card", {"sticky": "nsew"})])
    # Full-width contact rows use these button borders. A larger center tile
    # avoids thousands of tiny Win32 blits without changing the corner radius.
    button_states = (
        ("disabled", SURFACE, BORDER), ("focus", WHITE, FOCUS), ("pressed", ACCENT_WASH, FOCUS),
        ("active", "#f4f5ff", "#bbc2ee"),
    )
    element("Voice.Button.border", WHITE, BORDER, tile=96, states=button_states)
    element("Voice.SurfaceButton.border", WHITE, BORDER, tile=96, background=SURFACE, states=button_states)
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
    style.layout("Surface.TButton", _rename_element(style.layout("TButton"), "Voice.Button.border", "Voice.SurfaceButton.border"))
    element("Voice.NewTab.border", ACCENT, ACCENT, background=SURFACE, radius=10, states=(
        ("disabled", DISABLED, DISABLED), ("pressed", "#202c8c", "#202c8c"),
        ("active focus", "#2b38aa", "#8694f2"), ("focus", ACCENT, "#8694f2"),
        ("active", "#2b38aa", "#2b38aa"),
    ))
    icon_size = max(13, round(root.winfo_fpixels("10p"))) | 1
    plus = tk.PhotoImage(master=root, width=icon_size, height=icon_size)
    center = icon_size // 2
    plus.put(WHITE, to=(0, center - 1, icon_size, center + 2))
    plus.put(WHITE, to=(center - 1, 0, center + 2, icon_size))
    images.append(plus)
    style.element_create("Voice.NewTab.plus", "image", plus, sticky="")
    style.layout("NewTab.TButton", _rename_element(
        _rename_element(style.layout("TButton"), "Voice.Button.border", "Voice.NewTab.border"),
        "Button.label", "Voice.NewTab.plus",
    ))
    style.layout("Contact.TButton", _rename_element(style.layout("TButton"), "Voice.Button.border", "Voice.Contact.border"))
    style.layout("Link.TButton", [
        ("Button.focus", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "w"})]}),
        ]}),
    ])
    style.layout("TMenubutton", _rename_element(style.layout("TMenubutton"), "Menubutton.border", "Voice.Button.border"))
    element("Voice.Field", WHITE, BORDER, radius=7, states=button_states[:2])
    arrow = tk.PhotoImage(master=root, width=19, height=7)
    for y in range(4):
        arrow.put(INK, to=(6 + y, y + 1, 13 - y, y + 2))
    images.append(arrow)
    style.element_create("Voice.Combobox.downarrow", "image", arrow, sticky="")
    arrow_node, (_, field) = style.layout("TCombobox")
    style.layout("TCombobox", [("Voice.Field", {"sticky": "nsew", "children": [
        ("Voice.Combobox.downarrow", {"side": arrow_node[1]["side"], "sticky": "e"}),
        *field["children"],
    ]})])
    style.layout("TSpinbox", _rename_element(style.layout("TSpinbox"), "Spinbox.field", "Voice.Field"))
    element("Voice.Notebook.tab", WHITE, BORDER, background=SURFACE, gap=7, states=(
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
    style.configure("NewTab.TButton", padding=(12, 10), focuscolor=WHITE)
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
