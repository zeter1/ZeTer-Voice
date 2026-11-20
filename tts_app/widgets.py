from __future__ import annotations

from .i18n import tr
from .runtime import *
from .workspace_theme import ACCENT, ACCENT_WASH, FOCUS, INK, WHITE

class ClosableNotebook(ttk.Notebook):
    """
    ttk.Notebook with a real close icon rendered inside every tab.
    The widget does not close tabs itself: it emits <<NotebookCloseRequested>>
    so the application can reject closing a running conversion safely.
    Moving a tab emits <<NotebookTabsReordered>>.
    """

    def __init__(self, master=None, *, new_tab=None, **kwargs) -> None:
        self._build_close_style(master)
        kwargs["style"] = "ClosableNotebook"
        super().__init__(master, **kwargs)
        self._close_pressed_index: int | None = None
        self.close_requested_index: int | None = None
        self._drag_origin: tuple[str, int, int] | None = None
        self._drag_cursor: str | None = None
        self._drag_preview: tk.Toplevel | None = None

        self.bind("<ButtonPress-1>", self._on_close_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_close_release, add="+")
        self.bind("<ButtonPress-1>", self._on_drag_press, add="+")
        self.bind("<B1-Motion>", self._on_drag_motion, add="+")
        self.bind("<ButtonRelease-1>", self._on_drag_release, add="+")
        self.bind("<Unmap>", self._clear_drag, add="+")
        self._new_tab_layout_id = None
        self._new_tab_hint_id = None
        self._new_tab_hint: tk.Label | None = None
        self.new_tab_button = None
        if new_tab is not None:
            self.new_tab_button = ttk.Button(
                self, text=tr("Новая вкладка"), style="NewTab.TButton", cursor="hand2", command=new_tab,
            )
            self.new_tab_button.bind("<Enter>", self._schedule_new_tab_hint)
            for event in ("<Leave>", "<ButtonPress>", "<Unmap>", "<Destroy>"):
                self.new_tab_button.bind(event, self._hide_new_tab_hint)
            # The button is outside the real tab list. Reserve its width so even
            # squeezed tabs and their close icons stay separately clickable.
            reserve = self.new_tab_button.winfo_reqwidth() + 2
            ttk.Style(self).configure("ClosableNotebook", tabmargins=(0, 0, reserve, 0))
            self.bind("<Configure>", self._schedule_new_tab_button, add="+")
            self.bind("<<NotebookTabChanged>>", self._schedule_new_tab_button, add="+")
            self.bind("<Destroy>", self._cancel_new_tab_button_layout, add="+")
            self._schedule_new_tab_button()

    def tab(self, tab_id, option=None, **kwargs):
        result = super().tab(tab_id, option, **kwargs)
        if kwargs:
            self._schedule_new_tab_button()
        return result

    def forget(self, tab_id) -> None:
        super().forget(tab_id)
        if self._drag_origin is not None and self._drag_origin[0] not in self.tabs():
            self._clear_drag()
        self._schedule_new_tab_button()

    def destroy(self) -> None:
        self._clear_drag()
        super().destroy()

    def _schedule_new_tab_hint(self, event) -> None:
        self._hide_new_tab_hint()
        if not event.widget.instate(("disabled",)):
            self._new_tab_hint_id = event.widget.after(450, self._show_new_tab_hint)

    def _hide_new_tab_hint(self, _event=None) -> None:
        if self._new_tab_hint_id is not None:
            self.new_tab_button.after_cancel(self._new_tab_hint_id)
            self._new_tab_hint_id = None
        if self._new_tab_hint is not None:
            self._new_tab_hint.destroy()
            self._new_tab_hint = None

    def _show_new_tab_hint(self) -> None:
        self._new_tab_hint_id = None
        button = self.new_tab_button
        if not button.winfo_ismapped() or button.instate(("disabled",)):
            return
        root = self.winfo_toplevel()
        hint = tk.Label(
            root, text=button.cget("text") + "\n" + tr("Открывает пустую вкладку для озвучивания текста и сохранения в MP3."),
            background=INK, foreground=WHITE, justify="left", font=("Segoe UI", 10),
            wraplength=310, padx=13, pady=8, borderwidth=0,
        )
        x = button.winfo_rootx() - root.winfo_rootx()
        x = max(8, min(x, root.winfo_width() - hint.winfo_reqwidth() - 8))
        y = button.winfo_rooty() - root.winfo_rooty() + button.winfo_height() + 6
        hint.place(x=x, y=y)
        hint.lift()
        self._new_tab_hint = hint

    def _schedule_new_tab_button(self, _event=None) -> None:
        if self.new_tab_button is not None and self._new_tab_layout_id is None:
            self._new_tab_layout_id = self.after_idle(self._position_new_tab_button)

    def _cancel_new_tab_button_layout(self, _event=None) -> None:
        if self._new_tab_layout_id is not None:
            self.after_cancel(self._new_tab_layout_id)
            self._new_tab_layout_id = None

    def _position_new_tab_button(self) -> None:
        self._new_tab_layout_id = None
        button = self.new_tab_button
        if button is None:
            return
        width = self.winfo_width()
        if width <= 1:
            return
        y = button.winfo_reqheight() // 2
        limit = max(0, width - button.winfo_reqwidth() - 2)
        # Tk lays out the real tabs as a contiguous strip. Their skin supplies
        # the transparent visual gap inside each tab's actual hit rectangle.
        low, high = 0, limit
        while low < high:
            middle = (low + high) // 2
            element = self.identify(middle, y)
            if element and not element.endswith(".client"):
                low = middle + 1
            else:
                high = middle
        if low != button.winfo_x():
            self._hide_new_tab_hint()
        button.place(x=low, y=0)

    def _build_close_style(self, master) -> None:
        style = ttk.Style(master)

        # Keep images on the instance later; Tcl keeps named images, but Python
        # references are also retained by assigning them to the root.
        root = master.winfo_toplevel() if master is not None else None

        normal = tk.PhotoImage(width=17, height=9, master=root)
        active = tk.PhotoImage(width=17, height=9, master=root)
        pressed = tk.PhotoImage(width=17, height=9, master=root)

        def draw_cross(image: tk.PhotoImage, color: str) -> None:
            for offset in (0, 1):
                for i in range(1, 8):
                    x1 = min(8, i + offset)
                    x2 = max(0, 8 - i - offset)
                    image.put(color, to=(x1 + 7, i))
                    image.put(color, to=(x2 + 7, i))

        draw_cross(normal, "#666666")
        draw_cross(active, "#cc3333")
        draw_cross(pressed, "#992222")

        if root is not None:
            root._closable_notebook_images = (normal, active, pressed)  # type: ignore[attr-defined]

        try:
            style.element_create(
                "ClosableNotebook.close",
                "image",
                normal,
                ("active", "!disabled", active),
                ("pressed", "!disabled", pressed),
                border=2,
                sticky="",
            )
        except tk.TclError:
            # The element may already exist if another notebook was created.
            pass

        themed = root is not None and hasattr(root, "_workspace_theme_images")
        client_element = "Voice.Notebook.client" if themed else "Notebook.client"
        tab_element = "Voice.Notebook.tab" if themed else "Notebook.tab"
        style.layout("ClosableNotebook", [(client_element, {"sticky": "nswe"})])
        style.layout(
            "ClosableNotebook.Tab",
            [
                (
                    tab_element,
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Notebook.padding",
                                {
                                    "side": "top",
                                    "sticky": "nswe",
                                    "children": [
                                        ("Notebook.label", {"side": "left", "sticky": ""}),
                                        ("ClosableNotebook.close", {"side": "left", "sticky": ""}),
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )

    def _on_close_press(self, event):
        element = self.identify(event.x, event.y)
        if "close" not in element:
            return None

        try:
            index = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None

        self._close_pressed_index = index
        self.state(["pressed"])
        return "break"

    def _on_close_release(self, event):
        if self._close_pressed_index is None:
            return None

        pressed_index = self._close_pressed_index
        self._close_pressed_index = None
        self.state(["!pressed"])

        element = self.identify(event.x, event.y)
        if "close" not in element:
            return "break"

        try:
            index = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return "break"

        if index == pressed_index:
            self.close_requested_index = index
            self.event_generate("<<NotebookCloseRequested>>")

        return "break"

    def _on_drag_press(self, event):
        self._clear_drag()
        if len(self.tabs()) < 2:
            return
        try:
            index = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        self._drag_origin = self.tabs()[index], event.x, event.y

    def _on_drag_motion(self, event):
        if self._drag_origin is None:
            return
        frame, start_x, start_y = self._drag_origin
        preview = self._drag_preview
        if preview is None:
            if max(abs(event.x - start_x), abs(event.y - start_y)) < 6:
                return
            self._drag_cursor = str(self.cget("cursor"))
            self.configure(cursor="fleur")
            preview = self._create_drag_preview(frame)
            self._drag_preview = preview
            self._move_drag_preview(preview, event)
            preview.deiconify()
        else:
            self._move_drag_preview(preview, event)
        return "break"

    def _create_drag_preview(self, frame: str) -> tk.Toplevel:
        preview = tk.Toplevel(self, takefocus=False)
        preview.withdraw()
        preview.overrideredirect(True)
        preview.transient(self.winfo_toplevel())
        if os.name == "nt":
            preview.attributes("-disabled", True)
        tab_font = ttk.Style(self).lookup("ClosableNotebook.Tab", "font") or "TkDefaultFont"
        tk.Label(
            preview, text=self.tab(frame, "text"), font=tab_font,
            background=ACCENT_WASH, foreground=ACCENT, padx=12, pady=8,
            borderwidth=0, highlightthickness=1, highlightbackground=FOCUS,
        ).pack()
        return preview

    @staticmethod
    def _move_drag_preview(preview: tk.Toplevel, event) -> None:
        # Keep the pointer outside the preview so the drop target stays visible.
        preview.geometry(f"+{event.x_root + 12}+{event.y_root + 10}")

    def _clear_drag(self, _event=None) -> None:
        self._drag_origin = None
        preview = self._drag_preview
        self._drag_preview = None
        if preview is not None:
            preview.destroy()
        if self._drag_cursor is not None:
            self.configure(cursor=self._drag_cursor)
            self._drag_cursor = None

    def _on_drag_release(self, event):
        origin = self._drag_origin
        dragging = self._drag_preview is not None
        self._clear_drag()
        if not dragging:
            return
        if origin is None or origin[0] not in self.tabs():
            return "break"
        frame = origin[0]
        try:
            destination = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return "break"
        if destination != self.index(frame):
            self.insert(destination, frame)
            self._schedule_new_tab_button()
            self.event_generate("<<NotebookTabsReordered>>")
        return "break"
