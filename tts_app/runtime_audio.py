from __future__ import annotations

from .runtime_core import *
from .voice_process import stop_process as _stop_process

def get_wav_info(path: Path) -> dict:
    try:
        file_size = int(path.stat().st_size)
        with wave.open(str(path), "rb") as wav:
            frame_rate = int(wav.getframerate())
            frames = int(wav.getnframes())
            channels = int(wav.getnchannels())
            sample_width_bytes = int(wav.getsampwidth())
            frame_bytes = channels * sample_width_bytes
            appears_complete = False
            if frames > 0:
                # Seek within the declared data chunk, past any metadata chunks.
                wav.setpos(frames - 1)
                appears_complete = len(wav.readframes(1)) == frame_bytes
            return {
                "channels": channels,
                "sample_rate_hz": frame_rate,
                "sample_width_bits": sample_width_bytes * 8,
                "frames": frames,
                "duration_sec": (
                    round(frames / frame_rate, 3)
                    if frame_rate > 0
                    else 0
                ),
                "file_size_bytes": file_size,
                "expected_pcm_data_bytes": frames * frame_bytes,
                "appears_complete": appears_complete,
            }
    except Exception:
        return {}


def wav_total_duration_seconds(wav_files: list[Path]) -> float:
    total = 0.0
    for path in wav_files:
        info = get_wav_info(path)
        try:
            total += float(info.get("duration_sec") or 0)
        except Exception:
            pass
    return total


def get_ffmpeg_version(ffmpeg: str) -> str:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    try:
        result = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=creationflags,
        )
        first_line = (result.stdout or result.stderr or "").splitlines()
        return first_line[0].strip() if first_line else ""
    except Exception:
        return ""


def find_ffprobe(ffmpeg: str) -> str | None:
    system_ffprobe = shutil.which("ffprobe")
    if system_ffprobe:
        return system_ffprobe

    ffmpeg_path = Path(ffmpeg)
    candidates = [
        ffmpeg_path.with_name("ffprobe.exe"),
        ffmpeg_path.with_name("ffprobe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def probe_audio_file(ffmpeg: str, path: Path) -> dict:
    """
    Fast post-encode validation.

    Prefer ffprobe. If ffprobe is not installed, use FFmpeg itself only to read
    container/stream metadata; this does NOT decode the whole multi-hour MP3.
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    ffprobe = find_ffprobe(ffmpeg)
    if ffprobe:
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    (
                        "format=duration,bit_rate,format_name:"
                        "stream=codec_name,sample_rate,channels,bit_rate"
                    ),
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=creationflags,
            )
            if result.returncode == 0:
                payload = json.loads(result.stdout or "{}")
                streams = payload.get("streams") or []
                stream = streams[0] if streams else {}
                fmt = payload.get("format") or {}
                codec_name = str(
                    stream.get("codec_name") or ""
                )
                duration_value = float(
                    fmt.get("duration") or 0
                )
                return {
                    "tool": "ffprobe",
                    "tool_path": ffprobe,
                    "validated": bool(
                        codec_name and duration_value > 0
                    ),
                    "codec": codec_name,
                    "sample_rate_hz": clamp_int(
                        stream.get("sample_rate"),
                        0,
                        1_000_000,
                        0,
                    ),
                    "channels": clamp_int(
                        stream.get("channels"),
                        0,
                        64,
                        0,
                    ),
                    "bit_rate_bps": clamp_int(
                        stream.get("bit_rate")
                        or fmt.get("bit_rate"),
                        0,
                        10_000_000,
                        0,
                    ),
                    "duration_sec": duration_value,
                    "format_name": str(fmt.get("format_name") or ""),
                }
        except Exception:
            pass

    # Metadata-only fallback. "-t 0" exits immediately after input probing.
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-i",
                str(path),
                "-t",
                "0",
                "-f",
                "null",
                os.devnull,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        output = (result.stderr or "") + "\n" + (result.stdout or "")

        duration_sec = 0.0
        duration_match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
            output,
        )
        if duration_match:
            duration_sec = (
                float(duration_match.group(1)) * 3600
                + float(duration_match.group(2)) * 60
                + float(duration_match.group(3))
            )

        audio_match = re.search(
            r"Audio:\s*([^,]+),\s*(\d+)\s*Hz,\s*([^,]+)",
            output,
            re.IGNORECASE,
        )
        codec = ""
        sample_rate = 0
        channels = 0
        if audio_match:
            codec = audio_match.group(1).strip().split()[0]
            sample_rate = int(audio_match.group(2))
            channel_text = audio_match.group(3).strip().casefold()
            if "stereo" in channel_text:
                channels = 2
            elif "mono" in channel_text:
                channels = 1

        bitrate_match = re.search(
            r"bitrate:\s*(\d+)\s*kb/s",
            output,
            re.IGNORECASE,
        )
        bitrate = (
            int(bitrate_match.group(1)) * 1000
            if bitrate_match
            else 0
        )

        return {
            "tool": "ffmpeg_metadata_fallback",
            "tool_path": ffmpeg,
            "validated": bool(duration_sec > 0 and codec),
            "codec": codec,
            "sample_rate_hz": sample_rate,
            "channels": channels,
            "bit_rate_bps": bitrate,
            "duration_sec": duration_sec,
            "format_name": "mp3" if codec.casefold() == "mp3" else "",
        }
    except Exception as exc:
        return {
            "tool": "unavailable",
            "tool_path": "",
            "validated": False,
            "probe_error": f"{type(exc).__name__}: {exc}",
            "codec": "",
            "sample_rate_hz": 0,
            "channels": 0,
            "bit_rate_bps": 0,
            "duration_sec": 0.0,
            "format_name": "",
        }


def _decode_audio_samples(ffmpeg: str, path: Path, cancel_event: threading.Event) -> tuple[int, int]:
    """Count gapless samples from the first audio stream using bounded memory."""
    if cancel_event.is_set():
        raise InterruptedError("Операция остановлена пользователем.")

    with subprocess.Popen(
        [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
         "-i", str(path), "-map", "0:a:0", "-ac", "1", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        pcm_bytes = 0
        read_failed = False

        def count_pcm_bytes() -> None:
            nonlocal pcm_bytes, read_failed
            try:
                while block := process.stdout.read(128 * 1024):
                    pcm_bytes += len(block)
            except OSError:
                read_failed = True
                # Close our pipe so a failed reader cannot block the decoder.
                process.stdout.close()

        reader = threading.Thread(target=count_pcm_bytes, name="mp3-verification-reader")
        reader.start()
        try:
            while process.poll() is None and not cancel_event.wait(0.05):
                pass
        finally:
            _stop_process(process)
            reader.join()

        if cancel_event.is_set():
            raise InterruptedError("Операция остановлена пользователем.")
        return (0 if read_failed or pcm_bytes % 2 else pcm_bytes // 2), process.returncode


def validate_final_mp3(
    ffmpeg: str,
    path: Path,
    expected_duration_sec: float,
    cancel_event: threading.Event | None = None,
    *,
    channels: int = 0,
    sample_rate_hz: int = 0,
) -> dict:
    if cancel_event is None:
        cancel_event = threading.Event()
    if cancel_event.is_set():
        raise InterruptedError("Операция остановлена пользователем.")
    result = probe_audio_file(ffmpeg, path)
    if cancel_event.is_set():
        raise InterruptedError("Операция остановлена пользователем.")
    sample_rate = int(result.get("sample_rate_hz") or 0)
    codec = result.get("codec") or ""
    result["expected_duration_sec"] = round(expected_duration_sec, 3)
    result["validated"] = False
    # The FFmpeg fallback rounds metadata duration to centiseconds. Sample
    # count from the decoder also works for recordings below that precision.
    if not codec or sample_rate <= 0:
        result["fatal_error"] = "Не удалось подтвердить, что MP3 содержит звуковую дорожку."
        return result
    if codec.casefold() != "mp3":
        result["fatal_error"] = "Проверка готового файла обнаружила неожиданный кодек: " + codec
        return result
    if ((channels and result.get("channels") != channels)
            or (sample_rate_hz and sample_rate != sample_rate_hz)):
        result["fatal_error"] = "Количество каналов или частота дискретизации MP3 не совпадает с настройками."
        return result

    decoded, returncode = _decode_audio_samples(ffmpeg, path, cancel_event)
    result.update(decoded_samples=decoded, decode_returncode=returncode)
    if returncode or not decoded:
        result["fatal_error"] = "FFmpeg не смог полностью декодировать итоговый MP3."
        return result

    # Our LAME files include Xing gapless information. FFmpeg discards the
    # encoder delay and padding on decode. Allow two samples for resampling
    # rounding, plus LAME's minimum decoded output of 47 for very short input.
    expected_samples = round(expected_duration_sec * sample_rate)
    minimum = expected_samples - 2
    maximum = max(47, expected_samples + 2)
    matches = minimum <= decoded <= maximum
    result.update(
        expected_samples=expected_samples,
        decoded_duration_sec=round(decoded / sample_rate, 3),
        duration_delta_sec=round(abs(decoded - expected_samples) / sample_rate, 6),
        duration_tolerance_sec=round(max(2, maximum - expected_samples) / sample_rate, 6),
        duration_matches=matches,
        validated=matches,
    )
    if not matches:
        result["fatal_error"] = "Длительность MP3 не совпадает с длительностью звуковых фрагментов."
    return result


def estimate_disk_forecast(
    *,
    processed_chunks: int,
    total_chunks: int,
    temp_wav_bytes: int,
    sampled_audio_sec: float,
    bitrate: str,
) -> dict:
    if processed_chunks <= 0 or total_chunks <= 0:
        return {}

    ratio = total_chunks / processed_chunks
    estimated_temp_wav = int(temp_wav_bytes * ratio)
    estimated_audio_sec = max(0.0, sampled_audio_sec * ratio)
    estimated_mp3 = int(
        estimated_audio_sec
        * bitrate_to_bps(bitrate)
        / 8.0
        * 1.03
    )
    return {
        "sampled_chunks": processed_chunks,
        "chunks_total": total_chunks,
        "estimated_temp_wav_bytes": estimated_temp_wav,
        "estimated_audio_duration_sec": round(
            estimated_audio_sec,
            3,
        ),
        "estimated_mp3_bytes": estimated_mp3,
        "reserve_ratio": DISK_FORECAST_RESERVE_RATIO,
    }
