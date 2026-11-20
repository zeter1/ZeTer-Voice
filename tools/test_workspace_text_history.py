"""Test workspace archives on isolated Windows paths without starting the app."""
from __future__ import annotations

import gzip
import importlib.util
import os
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "workspace_text_history", ROOT / "tts_app" / "workspace_text_history.py"
)
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)

NOW = datetime(2026, 11, 19, 13, 30, tzinfo=timezone(timedelta(hours=3)))
EXPIRED_NAME = "workspace-text-20261020T103000.000000Z-tab01-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.txt.gz"


class WorkspaceTextHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workspace-history-")
        self.addCleanup(temporary.cleanup)
        self.sandbox = Path(temporary.name)
        self.directory = self.sandbox / "Текст проекта" / "История вкладок"

    def test_writes_exact_utf8_bytes_and_preserves_whitespace(self):
        self.assertFalse(history.archive_workspace_text(self.directory, ""))
        self.assertFalse(self.directory.exists())

        self.assertTrue(history.archive_workspace_text(self.directory, "Слово\r\n🙂\n\n", tab_id="tab_01"))
        self.assertTrue(history.archive_workspace_text(self.directory, " \t\r\n", tab_id="../another folder"))

        archives = list(self.directory.iterdir())
        self.assertEqual(len(archives), 2)
        by_payload = {}
        for entry in archives:
            self.assertTrue(entry.name.endswith(".txt.gz"))
            with gzip.open(entry, "rb") as archive:
                by_payload[archive.read()] = entry.name
        self.assertEqual(set(by_payload), {"Слово\r\n🙂\n\n".encode("utf-8"), b" \t\r\n"})
        self.assertIn("-tab_01-", by_payload["Слово\r\n🙂\n\n".encode("utf-8")])
        self.assertNotIn("another", by_payload[b" \t\r\n"])

    def test_cleans_by_utc_creation_at_exact_cutoff_and_preserves_foreign_paths(self):
        self.directory.mkdir(parents=True)
        expired = self.directory / EXPIRED_NAME
        older = self.directory / "workspace-text-20261020T102959.999999Z-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.txt.gz"
        fresh = self.directory / "workspace-text-20261020T103000.000001Z-cccccccccccccccccccccccccccccccc.txt.gz"
        malformed = self.directory / "workspace-text-20261099T103000.000000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.txt.gz"
        foreign = self.directory / "backup.txt.gz"
        staging = self.directory / ".workspace-text-in-progress.tmp"
        named_folder = self.directory / EXPIRED_NAME.replace("tab01", "folder")
        named_folder.mkdir()
        expected_kept = (fresh, malformed, foreign, staging)
        for entry in (expired, older, *expected_kept):
            entry.write_bytes(b"fixture")
        os.utime(fresh, (0, 0))

        self.assertEqual(history.cleanup_workspace_text_history(self.directory, now=NOW), (2, 0))
        self.assertEqual(set(self.directory.iterdir()), {*expected_kept, named_folder})
        self.assertTrue(named_folder.is_dir())
        for entry in expected_kept:
            self.assertEqual(entry.read_bytes(), b"fixture")
        self.assertEqual(history.cleanup_workspace_text_history(self.directory, now=NOW), (0, 0))

    def test_custom_retention_uses_utc_archive_creation(self):
        self.directory.mkdir(parents=True)
        expired = self.directory / "workspace-text-20261117T103000.000000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.txt.gz"
        fresh = self.directory / "workspace-text-20261117T103000.000001Z-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.txt.gz"
        expired.write_bytes(b"expired")
        fresh.write_bytes(b"fresh")
        self.assertEqual(history.cleanup_workspace_text_history(self.directory, now=NOW, retention_days=2), (1, 0))
        self.assertEqual(list(self.directory.iterdir()), [fresh])

    def test_failed_validation_keeps_prior_archive_and_retry_publishes(self):
        self.assertTrue(history.archive_workspace_text(self.directory, "Старая копия"))
        original, = self.directory.iterdir()
        original_bytes = original.read_bytes()
        with patch.object(history.gzip.GzipFile, "read", return_value=b"bad data"):
            with self.assertRaisesRegex(OSError, "failed content verification"):
                history.archive_workspace_text(self.directory, "Новая копия")
        self.assertEqual(list(self.directory.iterdir()), [original])
        self.assertEqual(original.read_bytes(), original_bytes)

        self.assertTrue(history.archive_workspace_text(self.directory, "Новая копия"))
        newest, = set(self.directory.iterdir()) - {original}
        self.assertEqual(gzip.decompress(newest.read_bytes()), "Новая копия".encode("utf-8"))

    def test_collision_preserves_prior_file_and_removes_staging(self):
        clock_time = datetime(2026, 10, 20, 10, 30, tzinfo=timezone.utc)
        archive_id = uuid.UUID(hex="a" * 32)
        self.directory.mkdir(parents=True)
        existing = self.directory / EXPIRED_NAME
        existing.write_bytes(b"existing file")

        with patch.object(history, "datetime", wraps=datetime) as clock, patch.object(
            history.uuid, "uuid4", return_value=archive_id
        ):
            clock.now.return_value = clock_time
            with self.assertRaises(FileExistsError):
                history.archive_workspace_text(self.directory, "new text", tab_id="tab01")

        self.assertEqual(list(self.directory.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"existing file")

    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing behavior")
    def test_locked_archive_reports_partial_cleanup_and_can_be_retried(self):
        self.directory.mkdir(parents=True)
        locked = self.directory / EXPIRED_NAME
        other = self.directory / EXPIRED_NAME.replace("tab01", "other")
        locked.write_bytes(b"before lock")
        other.write_bytes(b"other")
        with locked.open("rb") as reader:
            self.assertEqual(history.cleanup_workspace_text_history(self.directory, now=NOW), (1, 1))
            self.assertEqual(reader.read(), b"before lock")
        self.assertEqual(locked.read_bytes(), b"before lock")
        self.assertFalse(other.exists())
        self.assertEqual(history.cleanup_workspace_text_history(self.directory, now=NOW), (1, 0))

    def test_refuses_directory_junction_and_preserves_its_target(self):
        target = self.sandbox / "outside"
        target.mkdir()
        sentinel = target / EXPIRED_NAME
        sentinel.write_bytes(b"outside")
        junction = self.sandbox / "junction"
        if os.name == "nt":
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            if result.returncode:
                self.skipTest("The test filesystem cannot create a directory junction")
            self.addCleanup(junction.rmdir)
        else:
            junction.symlink_to(target, target_is_directory=True)
            self.addCleanup(junction.unlink)

        self.assertEqual(history.cleanup_workspace_text_history(junction, now=NOW), (0, 1))
        with self.assertRaises(NotADirectoryError):
            history.archive_workspace_text(junction / "child", "new text")
        self.assertEqual(list(target.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"outside")

    def test_preserves_matching_symbolic_link(self):
        self.directory.mkdir(parents=True)
        target = self.sandbox / "outside.txt"
        target.write_bytes(b"sentinel")
        link = self.directory / EXPIRED_NAME
        try:
            link.symlink_to(target)
        except OSError as exc:
            if os.name != "nt" or exc.winerror != 1314:
                raise
            self.skipTest("Windows did not grant permission to create file symlinks")

        self.assertEqual(history.cleanup_workspace_text_history(self.directory, now=NOW), (0, 0))
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"sentinel")


if __name__ == "__main__":
    unittest.main()
