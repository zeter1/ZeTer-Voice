"""Interface translations selected once at startup."""
from . import (
    ui_messages_capture,
    ui_messages_conversion,
    ui_messages_engines,
    ui_messages_main,
    ui_messages_playback,
    ui_messages_recovery,
    ui_messages_settings,
)

DEFAULT_LANGUAGE = "ru"
LANGUAGES = {
    "ru": "Русский",
    "en": "English",
    "es": "Español",
    "pl": "Polski",
    "fr": "Français",
    "pt": "Português (Brasil)",
}

_MESSAGES = {
    **ui_messages_main.MESSAGES,
    **ui_messages_settings.MESSAGES,
    **ui_messages_engines.MESSAGES,
    **ui_messages_playback.MESSAGES,
    **ui_messages_capture.MESSAGES,
    **ui_messages_conversion.MESSAGES,
    **ui_messages_recovery.MESSAGES,
}
_LANGUAGE_COLUMNS = {code: index for index, code in enumerate(tuple(LANGUAGES)[1:])}
_language_column = None


def normalize_language(value: object) -> str:
    return value if isinstance(value, str) and value in LANGUAGES else DEFAULT_LANGUAGE


def set_language(code: str) -> None:
    global _language_column
    _language_column = _LANGUAGE_COLUMNS.get(code)


def tr(source: str, *args: object) -> str:
    message = source
    if _language_column is not None:
        translations = _MESSAGES.get(source)
        if translations is not None:
            message = translations[_language_column]
    return message.format(*args) if args else message
