"""Expire only completed ZeTer Voice diagnostic sessions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import stat

from .runtime_core import APP_TITLE


_SESSION_NAME = re.compile(r"session_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{6}")
_ROOT_LOGS = re.compile(
    r"(?:session|AI_READ_FIRST|diagnostic_summary)\.json|events(?:_\d{3,})?\.jsonl"
    r"|task_mp3_\d{8}_\d{6}_\d{6}_[0-9a-f]{6}\.json"
)
_CHILD_LOGS = {
    "errors": re.compile(r"\d{8}_\d{6}_\d{6}_[0-9a-f]{6}\.json"),
    "hangs": re.compile(r"hang_\d{8}_\d{6}_[0-9a-f]{6}\.json"),
}


def _is_plain_path(path: Path, *, directory: bool = False) -> bool:
    info = path.lstat()
    mode_matches = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return mode_matches and not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def read_owned_log_session(path: Path) -> dict | None:
    if not _SESSION_NAME.fullmatch(path.name) or not _is_plain_path(path, directory=True):
        return None
    manifest = path / "session.json"
    try:
        if not _is_plain_path(manifest) or manifest.stat().st_size > 256 * 1024:
            return None
        record = json.loads(manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != 2:
        return None
    app = record.get("app")
    if (not isinstance(app, dict) or app.get("name") != APP_TITLE
            or record.get("session_id") != path.name.removeprefix("session_")):
        return None
    return record


def _only_log_files(path: Path) -> bool:
    for entry in path.iterdir():
        pattern = _CHILD_LOGS.get(entry.name)
        if pattern is None:
            if not _ROOT_LOGS.fullmatch(entry.name) or not _is_plain_path(entry):
                return False
        elif not _is_plain_path(entry, directory=True) or any(
            not pattern.fullmatch(child.name) or not _is_plain_path(child) for child in entry.iterdir()
        ):
            return False
    return True


def cleanup_expired_log_sessions(logs_dir: Path, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    failed = 0
    try:
        if not _is_plain_path(logs_dir, directory=True):
            return 0
        root = logs_dir.resolve()
        candidates = list(root.glob("session_*"))
    except FileNotFoundError:
        return 0
    except OSError:
        return 1
    for path in candidates:
        try:
            record = read_owned_log_session(path)
            if record is None:
                continue
            status = record.get("status")
            if status == "unclean_shutdown_detected":
                timestamp = record.get("unclean_shutdown_detected_at")
            elif status in ("closed", "forced_shutdown", "startup_failed"):
                timestamp = record.get("updated_at")
            else:
                continue
            finished = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
            if (finished is not None and finished.utcoffset() is not None and finished < cutoff
                    and path.resolve().parent == root and _only_log_files(path)):
                shutil.rmtree(path)
        except (ValueError, OverflowError):
            continue
        except OSError:
            failed += 1
    return failed
