"""MP3 conversion regressions using synthetic text and local audio only."""
from __future__ import annotations

from array import array
from contextlib import ExitStack
from io import BytesIO, TextIOWrapper
from math import sin, tau
from pathlib import Path
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tts_app import task_conversion_worker as worker
from tts_app import app_recovery, ffmpeg_tools, runtime_audio, runtime_recovery, sapi_core, voice_worker
from tts_app.runtime_audio import get_wav_info, probe_audio_file
from tts_app.task_source_tracking import TaskSourceTrackingMixin
from tts_app.text_processing import build_sapi_text, split_text


VOICE = "Synthetic fixture voice"
LINES = tuple(f"Строка {index} 😀 " + word * 215 + "." for index, word in enumerate(
    ("первый ", "second ", "третий "), 1
))
FREQUENCIES = dict(zip(LINES, (410, 710, 1010)))


def write_tone(path: Path, frequency: int) -> None:
    data = array("h", (int(12000 * sin(tau * frequency * i / 24000)) for i in range(6000)))
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(data.tobytes())


class SourceTracking(TaskSourceTrackingMixin):
    task_id = workspace_id = "fixture-tab"
    clear_saved_preview_bookmark = schedule_queue_status_refresh = log_text_state = Mock()

    def __init__(self, widget):
        self.app = Mock()
        self.text_box = widget
        self.mp3_source_start_mark = "fixture-start"
        self.mp3_source_end_mark = "fixture-end"

    def _full_text(self):
        return self.text_box.get("1.0", "end-1c")

    def _reset_mp3_source_tracking(self):
        self.text_box.mark_unset(self.mp3_source_start_mark, self.mp3_source_end_mark)
        self.mp3_source_run_id = None
        self.mp3_source_separator_pending = False


class Conversion(worker.TaskConversionWorkerMixin):
    task_id = workspace_id = "fixture-tab"
    current_mp3_text_backup_path = None

    def __init__(self):
        self.cancel_event = Event()
        self.app = SimpleNamespace(events=Queue(), logger=Mock())
        self.app.logger.error.return_value = ""


class MP3TextIntegrityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" and shutil.which("ffmpeg"), "requires Windows and local FFmpeg")
    def test_publication_keeps_external_recording_replaced_after_precheck(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "готовая запись.mp3"
            staged = root / "кандидат.mp3"
            incoming = root / "сторонняя запись.mp3"
            saved_first = root / "первая запись.mp3"
            ffmpeg = shutil.which("ffmpeg")
            wave_path = root / "fixture.wav"
            for path, frequency in ((target, 410), (staged, 710), (incoming, 1010)):
                write_tone(wave_path, frequency)
                worker.run_ffmpeg_concat(ffmpeg, [wave_path], path, "96k", root)
            initial, pending, external = (path.read_bytes() for path in (target, staged, incoming))
            self.assertEqual(len({initial, pending, external}), 3)

            identity = ffmpeg_tools.mp3_target_identity
            approved = identity(target)
            replaced = False

            def replace_after_precheck(path):
                nonlocal replaced
                observed = identity(path)
                if path == target and not replaced:
                    replaced = True
                    target.rename(saved_first)
                    incoming.rename(target)
                return observed

            with patch.object(ffmpeg_tools, "mp3_target_identity", side_effect=replace_after_precheck):
                result = ffmpeg_tools.publish_mp3(staged, target, approved)
            self.assertTrue(replaced)
            self.assertEqual(target.read_bytes(), external)
            self.assertEqual(saved_first.read_bytes(), initial)
            self.assertEqual(result, target.with_stem(target.stem + " (2)"))
            self.assertEqual(result.read_bytes(), pending)

            # A fresh, unchanged approval still replaces the chosen name.
            approved = identity(target)
            self.assertEqual(ffmpeg_tools.publish_mp3(result, target, approved), target)
            self.assertEqual(target.read_bytes(), pending)
            self.assertFalse(result.exists())
            self.assertFalse(list(root.glob(".zeter-voice-mp3-original-*")))

    @unittest.skipUnless(os.name == "nt", "requires Windows sharing modes")
    def test_publication_preserves_open_writer_that_allows_rename(self):
        import win32con
        import win32file

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target, staged = root / "target.mp3", root / "staged.mp3"
            target.write_bytes(b"original")
            staged.write_bytes(b"candidate")
            approved = ffmpeg_tools.mp3_target_identity(target)
            handle = win32file.CreateFile(
                str(target), win32con.GENERIC_WRITE,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                None, win32con.OPEN_EXISTING, 0, None,
            )
            try:
                published = ffmpeg_tools.publish_mp3(staged, target, approved)
                win32file.WriteFile(handle, b"external")
            finally:
                handle.Close()
            self.assertEqual(target.read_bytes(), b"external")
            self.assertEqual(published, target.with_stem("target (2)"))
            self.assertEqual(published.read_bytes(), b"candidate")

    def test_edge_worker_rejects_unconfirmed_audio(self):
        class Communicate:
            def __init__(self, text, *_args, **_kwargs):
                self.text = text
                self.state = {"cumulative_audio_bytes": 0}

            async def save(self, filename):
                Path(filename).write_bytes(b"audio bytes")
                # Edge v7.3.1 can save those bytes without throwing, while
                # only the turns acknowledged by turn.end contribute here.
                self.state["cumulative_audio_bytes"] = 11 if self.text == "full" else 5

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as patches:
            root = Path(temporary)
            package = root / "edge_tts"
            package.mkdir()
            (package / "__init__.py").touch()
            voice = "Microsoft Edge — Светлана (интернет)"
            requests = [dict(id=index, text=value, path=str(root / f"{index}.mp3"))
                        for index, value in enumerate(("partial", "full"), 1)]
            payload = "".join(json.dumps(item) + "\n" for item in requests).encode()
            with TextIOWrapper(BytesIO(payload)) as stdin, TextIOWrapper(BytesIO()) as stdout:
                patches.enter_context(patch.dict(sys.modules, edge_tts=SimpleNamespace(Communicate=Communicate)))
                patches.enter_context(patch.object(voice_worker, "EDGE_RUNTIME", root))
                patches.enter_context(patch.object(sys, "path", sys.path.copy()))
                patches.enter_context(patch.object(sys, "argv", ["voice_worker.py", voice]))
                patches.enter_context(patch.object(sys, "stdin", stdin))
                patches.enter_context(patch.object(sys, "stdout", stdout))
                voice_worker.main()
                responses = [json.loads(line) for line in stdout.buffer.getvalue().splitlines()]
            self.assertEqual([(item["id"], item["ok"]) for item in responses], [(1, False), (2, True)])
            self.assertEqual(responses[0]["code"], "synthesis")

    def test_recovery_rejects_unverified_legacy_edge_wavs(self):
        voice = "Microsoft Edge — Светлана (интернет)"
        manifest = {"voice": voice, "completed_chunks": 2}
        self.assertEqual(runtime_recovery.recovery_checkpoint_limit(manifest, 3), 0)
        manifest["edge_turn_end_verified"] = True
        self.assertEqual(runtime_recovery.recovery_checkpoint_limit(manifest, 3), 2)

    def test_recovery_stops_at_truncated_pcm_even_with_large_junk_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            first, broken, third = [work / f"chunk_{i:05d}.wav" for i in range(1, 4)]
            write_tone(first, 410)
            write_tone(third, 1010)
            info = first.read_bytes()
            junk = struct.pack("<4sI", b"JUNK", 12000) + bytes(12000)
            declared_size = len(info) - 8 + len(junk)
            broken.write_bytes(info[:4] + struct.pack("<I", declared_size) + info[8:12] + junk + info[12:-6000])
            self.assertTrue(get_wav_info(first)["appears_complete"])
            self.assertFalse(get_wav_info(broken)["appears_complete"])
            self.assertEqual(runtime_recovery.count_valid_recovery_wavs(work, 3)[0], 1)
            self.assertTrue(first.is_file())
            self.assertFalse(broken.exists())
            self.assertFalse(third.exists())

    def test_sapi_close_failure_keeps_primary_error(self):
        for speak_error in (None, RuntimeError("speak failed")):
            with self.subTest(speak_error=speak_error):
                voice = Mock()
                voice.Speak.side_effect = speak_error
                stream = Mock()
                stream.Close.side_effect = RuntimeError("close failed")
                client = SimpleNamespace(Dispatch=Mock(side_effect=[voice, stream]))
                com = SimpleNamespace(PumpWaitingMessages=Mock())
                with patch.dict(sys.modules, pythoncom=com, win32com=SimpleNamespace(client=client),
                                **{"win32com.client": client}), patch.object(sapi_core, "set_voice_by_description"):
                    with self.assertRaisesRegex(RuntimeError, "speak failed" if speak_error else "close failed"):
                        sapi_core.synthesize_wav_once("fixture", Path("unused.wav"), VOICE, 0, 0, 100)
                self.assertIsNone(voice.AudioOutputStream)
                stream.Close.assert_called_once()
                com.PumpWaitingMessages.assert_called_once()

    @unittest.skipUnless(shutil.which("ffmpeg"), "local FFmpeg unavailable")
    def test_ffmpeg_metadata_fallback_accepts_gapless_audio_even_below_centisecond(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, candidate = root / "source.wav", root / "candidate.mp3"
            with wave.open(str(source), "wb") as wav:
                wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x01\x00" * 43)
            ffmpeg = shutil.which("ffmpeg")
            worker.run_ffmpeg_concat(ffmpeg, [source], candidate, "96k", root)
            with patch.object(runtime_audio, "find_ffprobe", return_value=None):
                result = runtime_audio.validate_final_mp3(ffmpeg, candidate, 43 / 24000)
            self.assertTrue(result["validated"], result)
            self.assertEqual(result["tool"], "ffmpeg_metadata_fallback")
            self.assertTrue(43 <= result["decoded_samples"] <= 47, result)

    def test_cancel_reaps_active_decoder_with_a_busy_stdout_pipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "decoder started"
            stop = Event()
            children = []
            real_popen = subprocess.Popen

            def start_decoder(_command, **kwargs):
                code = ("import sys, threading; from pathlib import Path; "
                        "sys.stdout.buffer.write(bytes(1024 * 1024)); sys.stdout.buffer.flush(); "
                        "Path(sys.argv[1]).touch(); threading.Event().wait(15)")
                process = real_popen([sys.executable, "-c", code, str(marker)], **kwargs)
                children.append(process)
                return process

            def request_cancel():
                for _ in range(300):
                    if marker.is_file() or stop.wait(0.01):
                        break
                stop.set()

            watcher = Thread(target=request_cancel)
            watcher.start()
            try:
                with patch.object(runtime_audio.subprocess, "Popen", side_effect=start_decoder):
                    with self.assertRaises(InterruptedError):
                        runtime_audio._decode_audio_samples("ffmpeg", marker, stop)
            finally:
                stop.set()
                watcher.join()
            self.assertTrue(marker.exists(), "decoder could not drain a busy stdout pipe")
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertTrue(children[0].stdout.closed)

    def test_discard_and_expiry_keep_unrelated_file_beside_mp3(self):
        with tempfile.TemporaryDirectory(prefix="recovery ownership ") as temporary:
            base = Path(temporary)
            directory = base / "recovery"
            directory.mkdir()
            external_partial = base / "recording.part.mp3"
            external_partial.write_bytes(b"unrelated")
            for reason in ("discard", "expiry"):
                with self.subTest(reason=reason):
                    job = directory / ("job_" + reason)
                    job.mkdir()
                    manifest = {
                        "run_id": reason, "text_sha256": "abcdef", "chunks_total": 1,
                        "output_path": str(base / "recording.mp3"), "_job_dir": str(job),
                    }
                    external_partial.write_bytes(b"unrelated")
                    if reason == "discard":
                        app = app_recovery.AppRecoveryMixin()
                        app.logger = Mock()
                        with patch.object(app_recovery, "RECOVERY_DIR", directory):
                            app._discard_recovery_job(manifest, "test")
                    else:
                        (job / "recovery.json").write_text(json.dumps(manifest), encoding="utf-8")
                        os.utime(job, (1, 1))
                        with patch.object(runtime_recovery, "RECOVERY_DIR", directory):
                            self.assertEqual(runtime_recovery.scan_recovery_jobs(), ([], 1))
                    self.assertFalse(job.exists())
                    self.assertTrue(external_partial.is_file())
                    self.assertEqual(external_partial.read_bytes(), b"unrelated")

    def test_large_mixed_input_reaches_chunks_in_order(self):
        lines = [f"Строка {i:04d} 😀 Café 1 <tag> {i * 2}" for i in range(12000)]
        long_text = "Ж今Z" * 420000 + " конец последней строки"
        text = "\r\n\t".join(lines) + "\u2028" + long_text
        chunks = split_text(text)
        self.assertTrue(all(0 < len(part) <= 2600 for part in chunks))
        self.assertEqual(
            "".join(char for part in chunks for char in part if not char.isspace()),
            "".join(char for part in (*lines, long_text) for char in part if not char.isspace()),
        )

    def test_sapi_treats_user_markup_as_literal(self):
        text = "<bookmark mark='anything'/> Первый текст.\n第二行 & < третья строка"
        self.assertEqual(build_sapi_text(text, 0), (text, 16))
        escaped, flags = build_sapi_text(text, 1)
        self.assertEqual(flags, 8)
        self.assertIn("&lt;bookmark", escaped)
        self.assertIn("&amp; &lt; третья строка", escaped)

    def test_editor_cleanup_keeps_appends_and_modified_snapshot(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        editor = tk.Text(root)
        original = "Начало 😀\n第二行 🖼\nКонец"
        added = "\nНовый текст 🟢\nещё строка"
        editor.insert("1.0", original)
        task = SourceTracking(editor)
        self.assertTrue(task.track_mp3_source_snapshot("exact", original))
        editor.insert("end-1c", added)
        self.assertTrue(task.remove_completed_mp3_source("exact")["removed"])
        self.assertEqual(task._full_text(), added)

        self.assertTrue(task.track_mp3_source_snapshot("edited", added))
        editor.insert("2.0", "ИЗМЕНЕНО ")
        modified = task._full_text()
        self.assertFalse(task.remove_completed_mp3_source("edited")["removed"])
        self.assertEqual(task._full_text(), modified)

    @unittest.skipUnless(shutil.which("ffmpeg"), "local FFmpeg unavailable")
    def test_failed_synthesis_and_legacy_checkpoint_preserve_every_segment(self):
        with tempfile.TemporaryDirectory(prefix="mp3_integrity_") as temporary:
            directory = Path(temporary)
            output = directory / "готовая запись.mp3"
            prior_wav = directory / "previous.wav"
            write_tone(prior_wav, 1900)
            subprocess.run(
                [shutil.which("ffmpeg"), "-v", "error", "-i", str(prior_wav), str(output)],
                check=True, capture_output=True, timeout=10,
            )
            prior = output.read_bytes()
            recovery = directory / "незавершённые задачи"
            work = recovery / "job_fixture"
            text = "\n".join(LINES)
            self.assertEqual(split_text(text), list(LINES))
            called = []

            def synthesize(*, text, wav_path, **_kwargs):
                called.append(text)
                if len(called) == 2:
                    write_tone(wav_path, 1600)
                    raise RuntimeError("engine failed after writing a partial recording")
                write_tone(wav_path, FREQUENCIES[text])

            def convert(resume=None):
                conversion = Conversion()
                conversion._job_worker("fixture", text, {}, "fixture", 1,
                                       output, VOICE, 0, 0, 100, "96k", resume)
                return list(conversion.app.events.queue)

            com = SimpleNamespace(CoInitialize=Mock(), CoUninitialize=Mock())
            with ExitStack() as patches:
                patches.enter_context(patch.dict(sys.modules, pythoncom=com))
                patches.enter_context(patch.object(worker, "RECOVERY_DIR", recovery))
                patches.enter_context(patch.object(worker, "MAX_RETRIES", 1))
                patches.enter_context(patch.object(worker, "synthesize_wav_once", synthesize))
                self.assertEqual(convert()[-1][1], "job_error")
                self.assertEqual(output.read_bytes(), prior)
                manifest = json.loads((work / "recovery.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["completed_chunks"], 1)
                with self.subTest(phase="failed WAV"):
                    self.assertFalse((work / "chunk_00002.wav").exists())

                # A prior version could leave a valid-header failed chunk.
                # It is present on disk but absent from the schema-1 checkpoint.
                manifest.pop("sapi_literal_text", None)
                (work / "recovery.json").write_text(json.dumps(manifest), encoding="utf-8")
                write_tone(work / "chunk_00002.wav", 1600)
                actual_concat = worker.run_ffmpeg_concat

                def truncate(**kwargs):
                    # A valid and decodable MP3 is still wrong if one fragment is absent.
                    return actual_concat(**{**kwargs, "wav_files": kwargs["wav_files"][1:]})

                with patch.object(worker, "run_ffmpeg_concat", truncate):
                    _, kind, failure = convert(work)[-1]
                    self.assertEqual(kind, "job_error")
                    self.assertIn("Длительность MP3", failure["user_message"])
                with self.subTest(phase="prior recording"):
                    self.assertEqual(output.read_bytes(), prior)
                with self.subTest(phase="legacy prefix"):
                    self.assertEqual(called, [LINES[0], LINES[1], LINES[1], LINES[2]])

                metadata_durations = []

                def corrupt(**kwargs):
                    result = actual_concat(**kwargs)
                    candidate = kwargs["output_mp3"]
                    metadata_before = probe_audio_file(kwargs["ffmpeg"], candidate)
                    data = candidate.read_bytes()
                    start, end = len(data) // 3, len(data) * 2 // 3
                    candidate.write_bytes(data[:start] + bytes(end - start) + data[end:])
                    metadata_after = probe_audio_file(kwargs["ffmpeg"], candidate)
                    metadata_durations.extend(info["duration_sec"] for info in (metadata_before, metadata_after))
                    return result

                with patch.object(worker, "run_ffmpeg_concat", corrupt):
                    _, kind, failure = convert(work)[-1]
                self.assertEqual(len(metadata_durations), 2)
                self.assertGreater(metadata_durations[0], 0)
                self.assertEqual(*metadata_durations)
                self.assertEqual(kind, "job_error")
                self.assertRegex(failure["user_message"], "декодировать|Длительность MP3")
                self.assertEqual(output.read_bytes(), prior)
                self.assertEqual((work / "source_text.txt").read_text(encoding="utf-8"), text)
                _, kind, payload = convert(work)[-1]
                self.assertEqual(kind, "done")
                result_path = Path(payload["output"])
                self.assertNotEqual(result_path, output)
                self.assertEqual(output.read_bytes(), prior)
                self.assertFalse(work.exists())

            decoded = subprocess.run(
                [shutil.which("ffmpeg"), "-v", "error", "-xerror", "-i", str(result_path),
                 "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", "24000", "-"],
                check=True, capture_output=True, timeout=10,
            )
            samples = array("h", decoded.stdout)
            self.assertEqual(len(samples), 18000)
            measured = []
            for index in range(3):
                segment = samples[index * 6000 + 600:index * 6000 + 5400]
                measured.append(sum(a < 0 <= b for a, b in zip(segment, segment[1:])) * 5)
            for actual, expected in zip(measured, (410, 710, 1010)):
                self.assertAlmostEqual(actual, expected, delta=5)


if __name__ == "__main__":
    unittest.main()
