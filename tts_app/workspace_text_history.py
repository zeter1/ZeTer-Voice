"""Compressed copies of workspace tab text with a fixed retention window."""
from __future__ import annotations

import gzip
import os
import re
import stat
import tempfile
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path


WORKSPACE_TEXT_HISTORY_RETENTION_DAYS = 30
_TIME_FORMAT = "%Y%m%dT%H%M%S.%fZ"
_PREFIX = "workspace-text-"
_TAB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
_ARCHIVE_NAME = re.compile(
    rf"{_PREFIX}(?P<created>\d{{8}}T\d{{6}}\.\d{{6}}Z)"
    rf"(?:-{_TAB_ID.pattern})?-[a-f0-9]{{32}}\.txt\.gz",
    re.ASCII,
)


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _is_plain_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not _is_reparse_point(info)


def _checked_directory(directory: Path, *, create: bool = False) -> os.stat_result:
    if ".." in directory.parts:
        raise OSError("Workspace history directory must not contain parent traversal")
    for path in (*reversed(directory.parents), directory):
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                raise
            path.mkdir(exist_ok=True)
            info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
            raise NotADirectoryError("Workspace history requires a plain directory", str(path))
    return info


def _recheck_directory(directory: Path, original: os.stat_result) -> None:
    if not os.path.samestat(original, _checked_directory(directory)):
        raise OSError("Workspace history directory changed during the operation")


def archive_workspace_text(directory: Path, text: str, *, tab_id: str | None = None) -> bool:
    """Publish a verified UTF-8 copy. Return False for empty text."""
    if not text:
        return False

    payload = text.encode("utf-8")
    directory = directory.absolute()
    directory_state = _checked_directory(directory, create=True)
    timestamp = datetime.now(timezone.utc).strftime(_TIME_FORMAT)
    tab_part = f"-{tab_id}" if tab_id and _TAB_ID.fullmatch(tab_id) else ""
    destination = directory / f"{_PREFIX}{timestamp}{tab_part}-{uuid.uuid4().hex}.txt.gz"
    staging = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", dir=directory, prefix=f".{_PREFIX}", suffix=".tmp", delete=False
        ) as stream:
            staged_state = os.fstat(stream.fileno())
            staging = Path(stream.name)
            with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as output:
                output.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            try:
                with gzip.GzipFile(mode="rb", fileobj=stream) as candidate:
                    if candidate.read() != payload:
                        raise OSError("Workspace history archive failed content verification")
            except (EOFError, zlib.error) as exc:
                raise OSError("Workspace history archive is incomplete or invalid") from exc

        _recheck_directory(directory, directory_state)
        current = staging.lstat()
        if not _is_plain_file(current) or not os.path.samestat(staged_state, current):
            raise OSError("Staged workspace history archive changed before publication")
        if os.path.lexists(destination):
            raise FileExistsError(str(destination))
        staging.rename(destination)
        staging = None
        return True
    finally:
        if staging is not None:
            _recheck_directory(directory, directory_state)
            try:
                current = staging.lstat()
                if _is_plain_file(current) and os.path.samestat(staged_state, current):
                    staging.unlink()
            except FileNotFoundError:
                pass


def cleanup_workspace_text_history(
    directory: Path, *, now: datetime | None = None, retention_days: int = WORKSPACE_TEXT_HISTORY_RETENTION_DAYS,
) -> tuple[int, int]:
    """Remove expired workspace archives and count removals and I/O failures."""
    directory = directory.absolute()
    try:
        directory_state = _checked_directory(directory)
    except FileNotFoundError:
        return 0, 0
    except OSError:
        return 0, 1

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now.astimezone(timezone.utc) - timedelta(days=retention_days)
    removed = failures = 0
    try:
        for entry in directory.iterdir():
            match = _ARCHIVE_NAME.fullmatch(entry.name)
            if match is None:
                continue
            try:
                created = datetime.strptime(match["created"], _TIME_FORMAT).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if created > cutoff:
                continue
            _recheck_directory(directory, directory_state)
            try:
                if _is_plain_file(entry.lstat()):
                    entry.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                failures += 1
    except OSError:
        failures += 1
    return removed, failures
