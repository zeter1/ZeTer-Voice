from __future__ import annotations

import stat
from .i18n import DEFAULT_LANGUAGE, normalize_language
from .mp3_format import read_mp3_format

from .runtime_core import *
from .runtime_core import _ATOMIC_WRITE_LOCK

def app_code_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    try:
        return Path(__file__).resolve().parent.parent
    except NameError:
        return Path.cwd()


def resolve_storage_dirs() -> tuple[Path, Path, Path]:
    """
    Primary location: next to the .py/.exe.
    Fallback: %LOCALAPPDATA%\\TextToMp3Irina if the primary location is read-only.
    """
    base = app_code_dir()
    settings_dir = base / SETTINGS_DIR_NAME
    logs_dir = base / LOGS_DIR_NAME

    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        probe = settings_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return base, settings_dir, logs_dir
    except Exception:
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        fallback = local / "TextToMp3Irina"
        settings_dir = fallback / SETTINGS_DIR_NAME
        logs_dir = fallback / LOGS_DIR_NAME
        settings_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        return fallback, settings_dir, logs_dir


STORAGE_BASE_DIR, SETTINGS_DIR, LOGS_DIR = resolve_storage_dirs()
SETTINGS_FILE = SETTINGS_DIR / SETTINGS_FILE_NAME
WORKSPACE_FILE = SETTINGS_DIR / WORKSPACE_FILE_NAME
WORKSPACE_RECOVERY_FILE = SETTINGS_DIR / "workspace.recovery.json"
TEXT_STORAGE_DIR = STORAGE_BASE_DIR / TEXT_STORAGE_DIR_NAME
WORKSPACE_TEXT_DIR = TEXT_STORAGE_DIR / WORKSPACE_TEXT_DIR_NAME
WORKSPACE_TEXT_HISTORY_DIR = TEXT_STORAGE_DIR / WORKSPACE_TEXT_HISTORY_DIR_NAME
RECOVERY_DIR = TEXT_STORAGE_DIR / RECOVERY_DIR_NAME
MP3_TEXT_BACKUPS_DIR = TEXT_STORAGE_DIR / MP3_TEXT_BACKUPS_DIR_NAME


def _text_storage_directory_exists(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise OSError(f"Небезопасный путь к папке текста: {path.name}")
    return True


def migrate_text_storage() -> int:
    """Move known legacy directories on the same volume without merging files."""
    _text_storage_directory_exists(SETTINGS_DIR)
    if not _text_storage_directory_exists(TEXT_STORAGE_DIR):
        TEXT_STORAGE_DIR.mkdir()
    pairs = (
        (SETTINGS_DIR / WORKSPACE_TEXT_DIR_NAME, WORKSPACE_TEXT_DIR),
        (STORAGE_BASE_DIR / MP3_TEXT_BACKUPS_DIR_NAME, MP3_TEXT_BACKUPS_DIR),
        (STORAGE_BASE_DIR / RECOVERY_DIR_NAME, RECOVERY_DIR),
    )
    planned = []
    for source, target in pairs:
        target_present = _text_storage_directory_exists(target)
        if _text_storage_directory_exists(source):
            if target_present:
                raise FileExistsError(f"Найдены старая и новая папки: {target.name}")
            planned.append((source, target))
    moved = 0
    for source, target in planned:
        source.rename(target)
        moved += 1
    return moved


def read_workspace_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tabs"), list):
        raise ValueError("Некорректный список вкладок.")
    if not all(isinstance(tab, dict) for tab in payload["tabs"]):
        raise ValueError("Некорректная запись вкладки.")
    return payload


def merge_workspace_tabs(first: list[dict], second: list[dict]) -> list[dict]:
    existing = {str(tab["workspace_id"]) for tab in first if tab.get("workspace_id")}
    return first + [tab for tab in second if str(tab.get("workspace_id")) not in existing]


DEFAULT_SETTINGS = {
    "schema": 1,
    "ui_language": DEFAULT_LANGUAGE,
    "voice": DEFAULT_VOICE_HINT,
    "rate": 0,
    "pitch": 0,
    "audio_output": DEFAULT_AUDIO_OUTPUT_LABEL,
    "volume": 100,
    "bitrate": "96k",
    "mp3_channels": 0,
    "mp3_sample_rate_hz": 44100,
    "mp3_output_dir": "",
    "quiet_mp3_notifications": False,
    "editor_font_size": EDITOR_FONT_DEFAULT,
    "highlight_current_sentence": True,
    "save_workspace_text": True,
    "archive_text_versions": True,
    "text_backup_retention_days": MP3_TEXT_BACKUP_RETENTION_DAYS,
    "window_geometry": "1200x820",
    "last_input_dir": "",
    "last_output_dir": "",
    "global_copy_enabled": False,
    "global_copy_hotkey": "Ctrl+C",
    "autostart_windows": False,
}


def normalize_settings(raw: dict | None) -> dict:
    result = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        result.update(raw)

    result["ui_language"] = normalize_language(result["ui_language"])
    result["voice"] = str(result.get("voice") or DEFAULT_VOICE_HINT)
    result["rate"] = clamp_int(
        result.get("rate"),
        RATE_MIN,
        RATE_MAX,
        0,
    )
    result["pitch"] = clamp_int(
        result.get("pitch"),
        PITCH_MIN,
        PITCH_MAX,
        0,
    )
    result["audio_output"] = str(
        result.get("audio_output") or DEFAULT_AUDIO_OUTPUT_LABEL
    )
    result["volume"] = clamp_int(result.get("volume"), 0, 100, 100)

    result["bitrate"], result["mp3_channels"], result["mp3_sample_rate_hz"] = read_mp3_format(result)
    if not isinstance(result["mp3_output_dir"], str):
        result["mp3_output_dir"] = ""
    result["editor_font_size"] = clamp_int(
        result["editor_font_size"], EDITOR_FONT_MIN, EDITOR_FONT_MAX, EDITOR_FONT_DEFAULT,
    )
    for key in ("quiet_mp3_notifications", "highlight_current_sentence", "save_workspace_text", "archive_text_versions"):
        if not isinstance(result[key], bool):
            result[key] = DEFAULT_SETTINGS[key]
    days = result["text_backup_retention_days"]
    if type(days) is not int or not 1 <= days <= TEXT_BACKUP_RETENTION_MAX_DAYS:
        result["text_backup_retention_days"] = MP3_TEXT_BACKUP_RETENTION_DAYS

    result["window_geometry"] = str(result.get("window_geometry") or "1200x820")
    result["last_input_dir"] = str(result.get("last_input_dir") or "")
    result["last_output_dir"] = str(result.get("last_output_dir") or "")
    result["global_copy_enabled"] = bool(
        result.get("global_copy_enabled", False)
    )
    result["global_copy_hotkey"] = str(
        result.get("global_copy_hotkey") or "Ctrl+C"
    ).strip() or "Ctrl+C"
    result["autostart_windows"] = bool(
        result.get("autostart_windows", False)
    )
    return result


def _is_transient_atomic_replace_error(exc: BaseException) -> bool:
    return (
        os.name == "nt"
        and isinstance(exc, OSError)
        and getattr(exc, "winerror", None) in {5, 32, 33}
    )


def atomic_write_text(path: Path, text: str) -> int:
    """Publish complete UTF-8 text and return recovered replace retries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}."
        f"{uuid.uuid4().hex}.tmp"
    )

    with _ATOMIC_WRITE_LOCK:
        try:
            with tmp.open(
                "w",
                encoding="utf-8",
                newline="",
            ) as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())

            with tmp.open(encoding="utf-8", newline="") as handle:
                verified = handle.read()
            if verified != text:
                raise OSError(
                    "Проверка временного файла после записи не пройдена"
                )

            recovered_retries = 0
            while True:
                try:
                    os.replace(tmp, path)
                    return recovered_retries
                except OSError as exc:
                    if (
                        not _is_transient_atomic_replace_error(exc)
                        or recovered_retries
                        >= len(ATOMIC_REPLACE_RETRY_DELAYS_SEC)
                    ):
                        raise
                    time.sleep(
                        ATOMIC_REPLACE_RETRY_DELAYS_SEC[
                            recovered_retries
                        ]
                    )
                    recovered_retries += 1
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_json(path: Path, data: dict) -> int:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("Корневое значение JSON должно быть объектом")
    return atomic_write_text(path, payload)


def cleanup_old_mp3_text_backups(
    retention_days: int = MP3_TEXT_BACKUP_RETENTION_DAYS, *, protected_paths: frozenset[Path] = frozenset(),
) -> tuple[int, int]:
    """Delete only app-owned MP3 text backups older than the retention limit."""
    if not _text_storage_directory_exists(TEXT_STORAGE_DIR) or not _text_storage_directory_exists(MP3_TEXT_BACKUPS_DIR):
        return 0, 0
    cutoff_timestamp = (
        time.time()
        - retention_days * 24 * 60 * 60
    )
    removed = 0
    failed = 0

    for path in MP3_TEXT_BACKUPS_DIR.glob(
        "mp3_text_*.txt"
    ):
        if path in protected_paths:
            continue
        match = re.fullmatch(r"mp3_text_(\d{8}_\d{6}_\d{6})_[A-Za-z0-9_-]+_.+\.txt", path.name)
        if match is None:
            continue
        try:
            datetime.strptime(match[1], "%Y%m%d_%H%M%S_%f")
        except ValueError:
            continue
        try:
            info = path.lstat()
            if (
                stat.S_ISREG(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                and info.st_mtime <= cutoff_timestamp
            ):
                path.unlink()
                removed += 1
        except OSError:
            failed += 1

    return removed, failed


def create_mp3_text_backup(
    run_id: str,
    tab_title: str,
    raw_text: str,
) -> Path:
    """Persist the exact raw text snapshot before its MP3 task starts."""
    safe_title = re.sub(
        r'[<>:"/\\|?*]+',
        "_",
        str(tab_title or "").strip(),
    ).strip(" .")
    if not safe_title:
        safe_title = "Вкладка"
    safe_title = safe_title[:60].rstrip(" .") or "Вкладка"

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )
    run_suffix = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(run_id or "")[-24:],
    ).strip("_") or uuid.uuid4().hex[:8]
    backup_path = MP3_TEXT_BACKUPS_DIR / (
        f"mp3_text_{timestamp}_{run_suffix}_{safe_title}.txt"
    )
    atomic_write_text(backup_path, raw_text)
    return backup_path


def load_settings() -> dict:
    try:
        text = SETTINGS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Файл настроек должен содержать объект JSON.")
    return normalize_settings(payload)


def ensure_help_files() -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    MP3_TEXT_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    settings_readme = SETTINGS_DIR / "README.txt"
    settings_readme.write_text(
        "Эта папка хранит постоянные настройки и рабочие вкладки программы.\n\n"
        f"{SETTINGS_FILE_NAME} — общие настройки: голос, скорость, Pitch, громкость, "
        "качество MP3, размер окна и последние рабочие папки.\n"
        f"{WORKSPACE_FILE_NAME} — список открытых вкладок, их имена, параметры "
        "и сохранённая позиция прослушивания каждой вкладки.\n"
        f"Текст вкладок хранится рядом с программой в «{TEXT_STORAGE_DIR_NAME}\\{WORKSPACE_TEXT_DIR_NAME}».\n"
        f"Предыдущие версии хранятся в «{TEXT_STORAGE_DIR_NAME}\\{WORKSPACE_TEXT_HISTORY_DIR_NAME}» "
        "в файлах .txt.gz. Срок хранения можно настроить в программе.\n\n"
        f"Скорость едина для всех вкладок: {RATE_MIN}..{RATE_MAX}. "
        f"Pitch един для всех вкладок: {PITCH_MIN}..{PITCH_MAX}.\n"
        "Во время чтения кнопка «Вставить текст» добавляет текст в самый низ "
        "и не останавливает текущую фразу; добавленный хвост будет дочитан.\n"
        "Горячая клавиша чтения назначается отдельно каждой вкладке. Новая "
        "вкладка всегда создаётся со значением «Нет». Выделите текст в браузере, "
        "Telegram, Word или другом месте и нажмите клавишу конкретной вкладки: "
        "текст будет добавлен именно в эту вкладку, даже если на экране открыта "
        "другая вкладка. Сохраняются буквы разных языков, знаки и переносы строк. "
        "Одинаковую горячую клавишу нельзя назначить "
        "двум вкладкам.\n"
        "Галочка «Сразу читать добавленный текст» сохраняется отдельно для каждой "
        "вкладки. Она определяет, запускать ли чтение автоматически после "
        "добавления горячей клавишей и включать ли новый фрагмент в уже идущую "
        "очередь чтения.\n"
        "Галочка «Удалять прочитанные предложения» сохраняется отдельно "
        "для каждой вкладки. Полностью прочитанные предложения удаляются, "
        "но одно последнее предложение временно остаётся для безопасного "
        "Pause → Continue на предложение назад.\n"
        "Во время создания MP3 горячая клавиша продолжает добавлять новый "
        "текст вниз. После успешной проверки MP3 программа удаляет только тот "
        "снимок текста, который вошёл в этот файл; добавленный позже текст "
        "остаётся. При отмене, ошибке или изменении исходного снимка текст не "
        "удаляется.\n"
        "Текст вкладок и пауза прослушивания сохраняются автоматически. "
        "После перезапуска кнопка «Продолжить» возвращает к сохранённой позиции.\n"
        "Галочка «Автозапуск с Windows» добавляет программу в автозапуск "
        "текущего пользователя Windows без прав администратора.\n",
        encoding="utf-8",
    )

    recovery_readme = RECOVERY_DIR / "README.txt"
    recovery_readme.write_text(
        "Эта папка нужна только для восстановления аварийно оборванных "
        "конвертаций MP3.\n"
        "Не удаляйте свежие папки job_..., если хотите продолжить задачу "
        "после сбоя или выключения компьютера.\n"
        "Внутри job_... временно хранится source_text.txt — точная копия "
        "текста этой конкретной конвертации. Это НЕ диагностический лог и "
        "после успешного завершения/отмены удаляется вместе с WAV.\n"
        f"Данные старше {RECOVERY_RETENTION_DAYS} дней программа удаляет "
        "автоматически.\n",
        encoding="utf-8",
    )

    backups_readme = MP3_TEXT_BACKUPS_DIR / "README.txt"
    backups_readme.write_text(
        "Эта папка хранит точные UTF-8 копии текста, отправленного на создание "
        "MP3. Новый файл mp3_text_*.txt создаётся перед запуском каждой "
        "конвертации.\n"
        "Срок хранения бэкапов задаётся в настройках. Программа "
        "автоматически удаляет только собственные файлы mp3_text_*.txt старше "
        "этого срока; README.txt и другие файлы не удаляются.\n",
        encoding="utf-8",
    )

    logs_readme = LOGS_DIR / "КАК_АНАЛИЗИРОВАТЬ_ЛОГИ.txt"
    logs_readme.write_text(
        "ЛОГИ ДЛЯ АНАЛИЗА CHATGPT / CODEX\n"
        "=================================\n\n"
        "Передавайте нейросети последнюю папку session_.... Обычно ей не нужно читать её целиком.\n\n"
        "ПОРЯДОК ЧТЕНИЯ (экономит контекст):\n"
        "1. AI_READ_FIRST.json — маленький индекс проблемы: error_code, stage, owner_module и что открыть дальше.\n"
        "2. errors/<id>.json — traceback и bounded context только конкретной ошибки.\n"
        "3. diagnostic_summary.json — статистика, warnings, recent_signals и последние состояния.\n"
        "4. task_<run_id>.json — итог конкретного MP3-запуска. Успешные задачи сохраняются компактно.\n"
        "5. events*.jsonl — читать только нужный хвост/ID, если предыдущих файлов недостаточно.\n\n"
        "ОПТИМИЗАЦИЯ РАЗМЕРА:\n"
        "- точные повторения шумных событий дедуплицируются; счётчик остаётся в summary;\n"
        "- при длинной сессии routine events имеют мягкий лимит, но ошибки/final status не отбрасываются;\n"
        "- одинаковая ошибка группируется по fingerprint в одном errors/*.json с occurrences;\n"
        "- traceback и context ограничиваются по размеру; очевидные secrets редактируются;\n"
        "- добавление текста описывается этапами, размерами и результатом. Исходный текст и фрагменты синтеза не прикладываются.\n\n"
        "ID: tab_id = вкладка, run_id = создание MP3, preview_run_id = прослушивание.\n"
        f"Сессии хранятся не более {LOG_RETENTION_DAYS} дней и максимум {LOG_MAX_SESSIONS} папок. "
        f"Routine events ограничены примерно {EVENTS_SESSION_SOFT_LIMIT_BYTES // 1024} КБ на сессию.\n",
        encoding="utf-8",
    )
