from __future__ import annotations

from .runtime_core import *
from .runtime_audio import *
from .runtime_storage import RECOVERY_DIR
from .voice_catalog import voice_spec as _voice_spec

def cleanup_legacy_temp_workdirs() -> int:
    """Delete old pre-2.9 temp folders that can no longer be resumed safely."""
    removed = 0
    cutoff = time.time() - RECOVERY_RETENTION_DAYS * 86400
    temp_root = Path(tempfile.gettempdir())
    try:
        candidates = list(temp_root.glob("text_to_mp3_*"))
    except Exception:
        return 0

    for path in candidates:
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
                removed += 1
        except OSError:
            pass
    return removed


def scan_recovery_jobs(*, deferred: list[Path] | None = None) -> tuple[list[dict], int]:
    """
    Return resumable jobs and count successfully removed expired folders.
    Keep live jobs and folders whose state could not be verified.
    """
    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    jobs: list[dict] = []
    removed = 0
    cutoff = time.time() - RECOVERY_RETENTION_DAYS * 86400

    for job_dir in RECOVERY_DIR.glob("job_*"):
        manifest_path = job_dir / "recovery.json"
        try:
            if not job_dir.is_dir() or job_dir.is_symlink() or job_dir.is_junction():
                continue
            payload = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            if not isinstance(payload, dict):
                raise ValueError("recovery.json is not an object")

            run_id = str(payload.get("run_id") or "").strip()
            text_sha256 = str(
                payload.get("text_sha256") or ""
            ).strip()
            chunks_total = clamp_int(
                payload.get("chunks_total"),
                0,
                10_000_000,
                0,
            )
            output_text = str(
                payload.get("output_path") or ""
            ).strip()

            if (
                not run_id
                or not text_sha256
                or chunks_total <= 0
                or not output_text
            ):
                raise ValueError(
                    "recovery.json misses required fields"
                )

            owner_pid = clamp_int(
                payload.get("owner_pid"),
                0,
                2_147_483_647,
                0,
            )
            if owner_pid and process_is_running(owner_pid):
                continue

            if job_dir.stat().st_mtime < cutoff:
                shutil.rmtree(job_dir)
                removed += 1
                continue

            payload["_job_dir"] = str(job_dir)
            payload["_manifest_path"] = str(manifest_path)
            jobs.append(payload)

        except (OSError, ValueError):
            # A sharing violation or an incomplete manifest says nothing about
            # the usable WAV checkpoints and text already saved in this folder.
            if deferred is not None:
                deferred.append(job_dir)

    jobs.sort(
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )
    return jobs, removed


def recovery_checkpoint_limit(manifest: dict, chunks_total: int) -> int:
    """A WAV from a failed synthesis may have a complete header."""
    spec = _voice_spec(str(manifest.get("voice") or ""))
    if spec and spec[0] == "edge" and manifest.get("edge_turn_end_verified") is not True:
        return 0
    return clamp_int(manifest.get("completed_chunks"), 0, chunks_total, 0)


def count_valid_recovery_wavs(
    work_dir: Path,
    chunks_total: int,
    *,
    cleanup_stale: bool = True,
) -> tuple[int, int, float, dict]:
    """
    Count only a consecutive valid prefix: chunk_00001.wav ... chunk_N.wav.
    Any broken/later stale files are removed so a resume cannot mix old data.
    """
    valid_count = 0
    total_bytes = 0
    total_audio_sec = 0.0
    first_sample: dict = {}

    for index in range(1, chunks_total + 1):
        path = work_dir / f"chunk_{index:05d}.wav"
        try:
            if not path.exists() or path.stat().st_size < 128:
                break
            info = get_wav_info(path)
            if not info.get("appears_complete") or not info.get("frames"):
                break
            valid_count = index
            total_bytes += int(path.stat().st_size)
            total_audio_sec += float(info.get("duration_sec") or 0)
            if not first_sample:
                first_sample = {
                    "sample_chunk_index": index,
                    **info,
                }
        except Exception:
            break

    if cleanup_stale:
        for path in work_dir.glob("chunk_*.wav"):
            match = re.fullmatch(r"chunk_(\d{5,})\.wav", path.name)
            if match and int(match.group(1)) > valid_count:
                try:
                    path.unlink()
                except OSError:
                    pass

    return valid_count, total_bytes, total_audio_sec, first_sample
