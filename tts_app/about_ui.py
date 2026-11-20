"""About page and the developer's public contact links."""
from .i18n import tr

from tkinter import ttk
import webbrowser

from .runtime_core import APP_VERSION

GITHUB = "https://github.com/zeter1"
CONTACTS = (
    ("Почта", "zeter11@gmail.com", "mailto:zeter11@gmail.com"),
    ("Telegram", "@zeter1", "https://t.me/zeter1"),
    ("Telegram-группа", "@zeterchat", "https://t.me/zeterchat"),
    ("Сайт", "dkoles.com", "https://dkoles.com/"),
)
SOCIALS = (
    ("LinkedIn", "https://www.linkedin.com/in/zeter/"), ("Facebook", "https://www.facebook.com/zeter1"),
    ("ВКонтакте", "https://vk.ru/zeter1"), ("Instagram", "https://www.instagram.com/zeter1992/"),
    ("TenChat", "https://tenchat.ru/zeter"), ("X", "https://x.com/zeter1"),
    ("Threads", "https://www.threads.com/@zeter1992"), ("Одноклассники", "https://ok.ru/zeter1"),
)


def build_about_page(parent: ttk.Notebook) -> ttk.Frame:
    page = ttk.Frame(parent)
    card = ttk.Frame(page, padding=(20, 16), style="Card.TFrame")
    card.pack(fill="x", padx=8, pady=16)
    card.columnconfigure((0, 1), weight=1, uniform="about_links")
    ttk.Label(card, text="ZeTer Voice", style="Heading.Card.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(card, text=APP_VERSION, style="Note.Card.TLabel").grid(row=0, column=1, sticky="e")
    ttk.Label(
        card,
        text=tr("Программа для Windows, которая озвучивает введённый или выделенный текст "
             "и сохраняет озвучку в MP3."),
        style="Card.TLabel", wraplength=600, justify="left",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(9, 12))

    developer = ttk.Frame(card, style="Paper.TFrame")
    developer.grid(row=2, column=0, columnspan=2, sticky="w")
    ttk.Label(developer, text=tr("Разработчик"), style="Card.TLabel").pack(side="left")
    ttk.Button(
        developer, text=tr("Дмитрий Колесниченко · GitHub"),
        command=lambda: webbrowser.open(GITHUB), style="Link.TButton", cursor="hand2",
    ).pack(side="left", padx=(8, 0))
    ttk.Separator(card).grid(row=3, column=0, columnspan=2, sticky="ew", pady=12)
    ttk.Label(card, text=tr("Контакты для связи"), style="Heading.Card.TLabel").grid(
        row=4, column=0, columnspan=2, sticky="w", pady=(0, 10),
    )
    primary = ttk.Frame(card, style="Paper.TFrame")
    primary.grid(row=5, column=0, columnspan=2, sticky="ew")
    primary.columnconfigure((0, 2), weight=1, uniform="direct_contact")
    primary.columnconfigure(1, minsize=8)
    for index, (title, address, url) in enumerate(CONTACTS):
        ttk.Button(
            primary, text=f"{tr(title)}  ↗\n{address}", command=lambda target=url: webbrowser.open(target),
            style="Contact.TButton", cursor="hand2",
        ).grid(row=index // 2, column=2 * (index % 2), sticky="ew", pady=(0, 8))
    ttk.Label(card, text=tr("Социальные сети"), style="Card.TLabel").grid(row=6, column=0, sticky="w", pady=(9, 8))
    socials = ttk.Frame(card, style="Paper.TFrame")
    socials.grid(row=7, column=0, columnspan=2, sticky="ew")
    socials.columnconfigure((0, 2, 4, 6), weight=1, uniform="social_contact")
    socials.columnconfigure((1, 3, 5), minsize=8)
    for index, (title, url) in enumerate(SOCIALS):
        ttk.Button(
            socials, text=f"{tr(title)}  ↗", command=lambda target=url: webbrowser.open(target),
            style="Social.TButton", cursor="hand2",
        ).grid(row=index // 4, column=2 * (index % 4), sticky="ew", pady=(0, 6))
    return page
