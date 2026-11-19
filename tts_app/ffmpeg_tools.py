from __future__ import annotations

from .runtime import *

class FfmpegError(RuntimeError):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


def mp3_target_identity(path: Path) -> tuple[int, ...] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _replace_approved_mp3_on_windows(
    candidate: Path, target: Path, approved_identity: tuple[int, ...],
) -> bool:
    import pywintypes
    import win32con
    import win32file

    # Both renames refuse to overwrite a file that arrived in the meantime.
    # Keep the displaced copy outside candidate staging until publication succeeds.
    backup_dir = Path(tempfile.mkdtemp(prefix=".zeter-voice-mp3-original-", dir=target.parent))
    backup = backup_dir / target.name
    displaced = published = False
    read_lock = None
    try:
        try:
            target.rename(backup)
        except (FileNotFoundError, PermissionError):
            return False
        displaced = True
        # A writer can have an open handle that permits rename. Deny writes
        # before comparing the displaced file and until its approved removal.
        try:
            read_lock = win32file.CreateFile(
                str(backup), win32con.GENERIC_READ,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_DELETE,
                None, win32con.OPEN_EXISTING, 0, None,
            )
        except pywintypes.error:
            return False
        if mp3_target_identity(backup) != approved_identity:
            return False
        try:
            candidate.rename(target)
        except FileExistsError:
            return False
        published = True
        return True
    finally:
        if displaced:
            try:
                if published:
                    backup.unlink()
                else:
                    backup.rename(target)
            except OSError:
                # A new occupant or lock can prevent restoration. In that case,
                # leave the recording in its unique recovery directory.
                pass
        if read_lock is not None:
            read_lock.Close()
        try:
            backup_dir.rmdir()
        except OSError:
            pass


def publish_mp3(
    candidate: Path, target: Path, approved_identity: tuple[int, ...] | None,
) -> Path:
    if (
        os.name == "nt"
        and approved_identity is not None
        and mp3_target_identity(target) == approved_identity
        and _replace_approved_mp3_on_windows(candidate, target, approved_identity)
    ):
        return target

    # Windows rename fails if another process already created the target.
    # On POSIX, use a same-volume hard link for the same no-overwrite rule.
    for number in range(1, 10_001):
        destination = target if number == 1 else target.with_stem(f"{target.stem} ({number})")
        try:
            if os.name == "nt":
                candidate.rename(destination)
            else:
                os.link(candidate, destination)
        except FileExistsError:
            continue
        return destination
    raise FileExistsError("Не удалось подобрать свободное имя для MP3.")


def _parse_ffmpeg_clock(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.strip().split(":")
        return (
            float(hours) * 3600
            + float(minutes) * 60
            + float(seconds)
        )
    except Exception:
        return None


def run_ffmpeg_concat(
    ffmpeg: str,
    wav_files: list[Path],
    output_mp3: Path,
    bitrate: str,
    work_dir: Path,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> dict:
    """
    Concatenate WAV files and encode MP3 with live progress and cancellation.

    FFmpeg runs through Popen instead of blocking subprocess.run, so the user
    can cancel during the final encoding stage as well.
    """
    concat_file = work_dir / "concat.txt"

    def ffmpeg_escape(path: Path) -> str:
        value = path.resolve().as_posix().replace("'", r"'\''")
        return f"file '{value}'"

    concat_file.write_text(
        "\n".join(ffmpeg_escape(path) for path in wav_files),
        encoding="utf-8",
    )

    temporary_output = output_mp3.with_name(
        output_mp3.stem + ".part.mp3"
    )
    temporary_output.unlink(missing_ok=True)

    total_audio_sec = wav_total_duration_seconds(wav_files)

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        "-write_xing",
        "1",
        "-progress",
        "pipe:1",
        "-nostats",
        str(temporary_output),
    ]

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )

    line_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    stderr_lines: list[str] = []

    def reader(stream, source: str) -> None:
        try:
            if stream is None:
                return
            for line in iter(stream.readline, ""):
                line_queue.put((source, line.rstrip("\r\n")))
        finally:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    stdout_thread = threading.Thread(
        target=reader,
        args=(process.stdout, "stdout"),
        name="ffmpeg-stdout-reader",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=reader,
        args=(process.stderr, "stderr"),
        name="ffmpeg-stderr-reader",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    last_out_time_sec = 0.0
    last_callback_at = 0.0
    cancelled = False

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                try:
                    process.terminate()
                    process.wait(
                        timeout=FFMPEG_TERMINATE_TIMEOUT_SEC
                    )
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
                break

            while True:
                try:
                    source, line = line_queue.get_nowait()
                except queue.Empty:
                    break

                if source == "stderr":
                    if line:
                        stderr_lines.append(line)
                        if len(stderr_lines) > 300:
                            stderr_lines = stderr_lines[-300:]
                    continue

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                parsed_time = None

                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        parsed_time = float(value) / 1_000_000.0
                    except Exception:
                        parsed_time = None
                elif key == "out_time":
                    parsed_time = _parse_ffmpeg_clock(value)

                if parsed_time is not None:
                    last_out_time_sec = max(
                        last_out_time_sec,
                        parsed_time,
                    )

            now = time.perf_counter()
            if (
                progress_callback is not None
                and now - last_callback_at >= 0.5
            ):
                fraction = None
                if total_audio_sec > 0:
                    fraction = max(
                        0.0,
                        min(
                            1.0,
                            last_out_time_sec / total_audio_sec,
                        ),
                    )

                try:
                    part_size = (
                        temporary_output.stat().st_size
                        if temporary_output.exists()
                        else 0
                    )
                except OSError:
                    part_size = 0

                progress_callback(
                    {
                        "fraction": fraction,
                        "out_time_sec": round(
                            last_out_time_sec,
                            3,
                        ),
                        "total_audio_sec": round(
                            total_audio_sec,
                            3,
                        ),
                        "elapsed_sec": round(
                            now - started,
                            3,
                        ),
                        "output_part_bytes": part_size,
                    }
                )
                last_callback_at = now

            return_code = process.poll()
            if return_code is not None:
                break

            time.sleep(0.05)

        # Drain final output after process termination.
        deadline = time.perf_counter() + 1.0
        while time.perf_counter() < deadline:
            drained = False
            while True:
                try:
                    source, line = line_queue.get_nowait()
                except queue.Empty:
                    break
                drained = True
                if source == "stderr" and line:
                    stderr_lines.append(line)
            if not stdout_thread.is_alive() and not stderr_thread.is_alive():
                break
            if not drained:
                time.sleep(0.02)

        elapsed = time.perf_counter() - started

        if cancelled:
            temporary_output.unlink(missing_ok=True)
            raise InterruptedError(
                "Создание MP3 остановлено пользователем во время FFmpeg."
            )

        return_code = process.returncode
        stderr = "\n".join(stderr_lines)[-6000:].strip()

        if return_code != 0:
            temporary_output.unlink(missing_ok=True)
            raise FfmpegError(
                f"FFmpeg не смог создать MP3. Код выхода: {return_code}",
                stderr=stderr,
            )

        if (
            not temporary_output.exists()
            or temporary_output.stat().st_size == 0
        ):
            temporary_output.unlink(missing_ok=True)
            raise FfmpegError(
                "FFmpeg завершился без ошибки, но временный MP3 пуст или не создан.",
                stderr=stderr,
            )

        if progress_callback is not None:
            try:
                progress_callback(
                    {
                        "fraction": 1.0,
                        "out_time_sec": round(
                            total_audio_sec,
                            3,
                        ),
                        "total_audio_sec": round(
                            total_audio_sec,
                            3,
                        ),
                        "elapsed_sec": round(
                            elapsed,
                            3,
                        ),
                        "output_part_bytes": (
                            temporary_output.stat().st_size
                        ),
                    }
                )
            except Exception:
                pass

        os.replace(temporary_output, output_mp3)

        return {
            "duration_sec": round(elapsed, 3),
            "audio_duration_sec": round(total_audio_sec, 3),
            "stderr_tail": stderr,
        }

    finally:
        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
