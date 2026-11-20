"""Engine manager tests on synthetic source/state only; no installs into real runtime."""
import hashlib
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1]
MIRROR = tempfile.TemporaryDirectory(prefix='tts-manager-', dir=SOURCE)
ROOT = Path(MIRROR.name)
(ROOT / 'tts_app').mkdir()
for file in (SOURCE / 'tts_app').glob('*.py'):
    shutil.copyfile(file, ROOT / 'tts_app' / file.name)
sys.path.insert(0, str(ROOT))
from tts_app import engine_installation as owner
from tts_app import engine_manager_ui as manager_ui
from tts_app import voice_catalog as catalog
from tts_app.runtime import STORAGE_BASE_DIR
assert STORAGE_BASE_DIR.resolve() == ROOT.resolve()


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.fixture.name)
        self.engines = self.root / 'Движки речи'
        self.engines.mkdir()
        self.model = self.engines / 'silero.pt'
        self.edge = self.engines / 'edge'
        self.patches = [patch.object(owner, name, value) for name, value in {
            'ENGINE_ROOT': self.engines, 'MODEL_PATH': self.model, 'EDGE_RUNTIME': self.edge,
            'OPTIONAL': self.engines / 'optional', 'BACKUPS': self.root / '_maintenance_backups' / 'removed_engines',
        }.items()]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.fixture.cleanup()

    def test_install_existing_never_reads_network_or_replaces_bytes(self):
        self.model.write_bytes(b'existing model sentinel')
        self.edge.mkdir()
        (self.edge / 'private.txt').write_bytes(b'existing edge sentinel')
        with patch.object(owner.urllib.request, 'urlopen', side_effect=AssertionError('network')), \
             patch.object(owner, 'pip_install', side_effect=AssertionError('pip')):
            owner.install_engine('silero')
            owner.install_engine('edge')
        self.assertEqual(self.model.read_bytes(), b'existing model sentinel')
        self.assertEqual((self.edge / 'private.txt').read_bytes(), b'existing edge sentinel')

    def test_install_existing_verified_model_adds_only_missing_torch(self):
        model_bytes = b'existing verified model sentinel'
        self.model.write_bytes(model_bytes)
        digest = hashlib.sha256(model_bytes).hexdigest()

        def fake_pip(packages, destination):
            self.assertEqual(packages[0], 'torch==2.8.0')
            package = destination / 'torch'
            package.mkdir(parents=True)
            (package / '__init__.py').write_bytes(b'# synthetic torch')

        with patch.object(owner, 'verified_model', side_effect=lambda path: catalog.verified_model(path, digest)), \
             patch.object(owner.importlib.util, 'find_spec', return_value=None), \
             patch.object(owner.urllib.request, 'urlopen', side_effect=AssertionError('model download')), \
             patch.object(owner, 'pip_install', side_effect=fake_pip):
            owner.install_engine('silero')
        self.assertEqual(self.model.read_bytes(), model_bytes)
        self.assertTrue((owner.OPTIONAL / 'torch' / '__init__.py').is_file())

    def test_remove_preserves_recovery_and_unrelated_files(self):
        self.model.write_bytes(b'existing model sentinel')
        self.edge.mkdir()
        (self.edge / 'keep.txt').write_bytes(b'keep')
        owner.remove_engine('silero')
        self.assertFalse(self.model.exists())
        backups = list(owner.BACKUPS.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'existing model sentinel')
        self.assertEqual((self.edge / 'keep.txt').read_bytes(), b'keep')
        self.assertEqual(owner.remove_engine('silero'), 'Уже отсутствует')

    def test_unknown_engine_rejected_before_file_changes(self):
        for engine in ('sapi', '../outside', 'global'):
            with self.assertRaises(ValueError): owner.remove_engine(engine)
            with self.assertRaises(ValueError): owner.install_engine(engine)
        self.assertEqual(list(self.engines.iterdir()), [])

    def test_staged_install_does_not_touch_global_environment(self):
        def fake_pip(packages, destination):
            self.assertEqual(packages, ['edge-tts==7.3.1'])
            self.assertTrue(destination.resolve().is_relative_to(self.engines))
            (destination / 'edge_tts').mkdir(parents=True)
            (destination / 'edge_tts' / '__init__.py').write_text('# fixture')
        with patch.object(owner, 'pip_install', side_effect=fake_pip):
            owner.install_engine('edge')
        self.assertTrue((self.edge / 'edge_tts' / '__init__.py').is_file())
        self.assertEqual(list(self.engines.iterdir()), [self.edge])

    def test_failed_install_never_publishes_partial_engine(self):
        def partial(_packages, destination):
            destination.mkdir()
            (destination / 'partial').write_bytes(b'partial')
            raise RuntimeError('synthetic download failure')
        with patch.object(owner, 'pip_install', side_effect=partial):
            with self.assertRaises(RuntimeError): owner.install_engine('edge')
        self.assertFalse(self.edge.exists())
        self.assertEqual(list(self.engines.iterdir()), [])


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = manager_ui.EngineManager.__new__(manager_ui.EngineManager)
        self.manager.messages = queue.Queue()
        self.manager.stop = threading.Event()
        self.manager.closed = False
        self.manager.busy = True
        self.manager.pending_process = None
        self.manager.tree_stop_unconfirmed = False
        self.manager.statuses = {}
        self.manager.buttons = [Mock()]
        self.manager.root = Mock()
        self.manager.app = Mock(tabs={}, shutdown_in_progress=False)
        self.manager.app.engine_manager = self.manager
        self.manager.progress = Mock()
        self.manager.cancel = Mock()
        self.manager.message = Mock()
        self.manager.refresh = Mock()

    def test_tree_stop_releases_audio_and_blocks_management_until_process_exits(self):
        process = Mock(pid=1234, returncode=None)
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired('fixture', 2)
        process.kill.side_effect = PermissionError('fixture')
        self.manager.stop.set()
        with patch.object(manager_ui.subprocess, 'Popen', return_value=process), \
             patch.object(manager_ui.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as kill:
            self.manager.run('install', 'silero')
        if sys.platform == 'win32':
            self.assertEqual(kill.call_args.args[0], ['taskkill', '/PID', '1234', '/T', '/F'])
        process.kill.assert_called_once()
        kind, (message, pending, tree_stop_unconfirmed) = self.manager.messages.get_nowait()
        self.assertEqual(kind, 'done')
        self.assertIn('не удалось подтвердить остановку', message.lower())
        self.assertIs(pending, process)
        self.assertFalse(tree_stop_unconfirmed)
        self.assertTrue(self.manager.messages.empty())
        self.manager.messages.put((kind, (message, pending, tree_stop_unconfirmed)))
        self.manager.poll()
        self.assertFalse(manager_ui.engine_operation_busy(self.manager.app))
        self.manager.message.set.assert_called_once_with(message)
        self.manager.progress.stop.assert_called_once()
        with patch.object(manager_ui.threading, 'Thread') as launch:
            self.manager.start('install', 'edge')
            launch.assert_not_called()
            process.poll.return_value = 1
            self.manager.poll()
            self.manager.start('install', 'edge')
            launch.assert_called_once_with(target=self.manager.run, args=('install', 'edge'),
                                           daemon=True, name='engine-management')
        self.manager.app._load_voices_async.assert_called_once()

    @unittest.skipUnless(sys.platform == 'win32', 'taskkill is Windows-only')
    def test_unconfirmed_tree_keeps_management_disabled_after_parent_exit(self):
        process = Mock(pid=1234, returncode=None)
        process.poll.return_value = None

        def reaped(*, timeout):
            process.poll.return_value = 1
            return 1

        process.wait.side_effect = reaped
        self.manager.stop.set()
        with patch.object(manager_ui.subprocess, 'Popen', return_value=process), \
             patch.object(manager_ui.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            self.manager.run('install', 'silero')
        process.kill.assert_called_once()
        self.assertEqual(process.poll(), 1)
        self.manager.poll()
        self.assertFalse(manager_ui.engine_operation_busy(self.manager.app))
        with patch.object(manager_ui.threading, 'Thread') as launch:
            self.manager.start('install', 'edge')
            launch.assert_not_called()
        self.manager.buttons[0].configure.assert_not_called()
        self.assertIn('дерева процессов', self.manager.message.set.call_args.args[0])

    def test_timeout_reports_timeout_after_owned_process_is_reaped(self):
        process = Mock(pid=1234)
        process.poll.return_value = None
        process.wait.return_value = 1
        with patch.object(manager_ui.subprocess, 'Popen', return_value=process), \
             patch.object(manager_ui.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(manager_ui.time, 'monotonic', side_effect=[1, 1802]):
            self.manager.run('install', 'edge')
        kind, (message, pending, tree_stop_unconfirmed) = self.manager.messages.get_nowait()
        self.assertEqual(kind, 'done')
        self.assertIsNone(pending)
        self.assertFalse(tree_stop_unconfirmed)
        self.assertIn('время', message.lower())
        process.wait.assert_called_once()


if __name__ == '__main__':
    try: unittest.main()
    finally: MIRROR.cleanup()
