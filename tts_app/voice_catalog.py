"""Fixed voice identities and offline discovery of the optional runtimes."""
from .i18n import tr

from contextlib import contextmanager
from functools import lru_cache
import hashlib
from pathlib import Path
import subprocess
import sys
import threading

if __package__:
    from .engine_runtime import engine_python, engine_environment, program_root
else:
    from engine_runtime import engine_python, engine_environment, program_root

MODEL_URL = 'https://models.silero.ai/models/tts/ru/v5_6_ru.pt'
MODEL_SHA256 = '04cc9b1d08ce38bcaa9d02c488d80d3e6806d17f7eaa7c95982f36a3cbf50b2e'
MODEL_BYTES = 147664410
MODEL_PATH = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
              else Path(__file__).resolve().parents[1]) / 'Движки речи' / 'silero_v5_6_ru.pt'
EDGE_RUNTIME = MODEL_PATH.parent / 'edge_tts_7_3_1'
_MODEL_CHECK_LOCK = threading.Lock()
VOICES = {
    'Silero — Ксения (без интернета)': ('silero', 'kseniya'),
    'Silero — Ксения, вариант 2 (без интернета)': ('silero', 'xenia'),
    'Silero — Бая (без интернета)': ('silero', 'baya'),
    'Silero — Айдар (без интернета)': ('silero', 'aidar'),
    'Silero — Евгений (без интернета)': ('silero', 'eugene'),
    'Microsoft Edge — Светлана (интернет)': ('edge', 'ru-RU-SvetlanaNeural'),
    'Microsoft Edge — Дмитрий (интернет)': ('edge', 'ru-RU-DmitryNeural'),
}


def extra_voice_names() -> list[str]:
    return list(VOICES)


def available_extra_voice_names() -> list[str]:
    try:
        engine_python()
    except (OSError, RuntimeError):
        return []
    silero = model_available(MODEL_PATH) and torch_available(MODEL_PATH.parent / 'optional_dependencies')
    edge = (EDGE_RUNTIME / 'edge_tts' / '__init__.py').is_file()
    available = {'silero': silero, 'edge': edge}
    return [name for name, (engine, _) in VOICES.items() if available[engine]]


def torch_available(optional: Path) -> bool:
    # The GUI interpreter can differ from the interpreter used by voice_worker.py.
    probe = ('import importlib.util, sys; sys.path.insert(0, sys.argv[1]); sys.path.append(sys.argv[2]); '
             'raise SystemExit(0 if importlib.util.find_spec("torch") else 1)')
    try:
        return subprocess.run([engine_python(), '-P', '-c', probe, str(program_root() / 'tts_app'), str(optional)],
                              env=engine_environment(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode == 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False


def _model_signature(path: Path) -> tuple[int, ...]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@lru_cache(maxsize=1)
def _check_model(path: Path, signature: tuple[int, ...], digest: str) -> bool:
    with verified_model(path, digest):
        return _model_signature(path) == signature


def model_available(path: Path) -> bool:
    # Share the disk read between background UI checks. Synthesis and installation
    # still verify the current model without consulting this cache.
    try:
        with _MODEL_CHECK_LOCK:
            return _check_model(path, _model_signature(path), MODEL_SHA256)
    except (OSError, RuntimeError):
        return False


def voice_spec(description: str) -> tuple[str, str] | None:
    return VOICES.get(description)


@contextmanager
def verified_model(path: Path = MODEL_PATH, digest: str = MODEL_SHA256):
    try:
        stream = path.open('rb')
    except FileNotFoundError:
        raise RuntimeError(tr('Модель Silero не установлена. Выполните: python tools/setup_voices.py')) from None
    with stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != digest:
            raise RuntimeError(tr('Модель Silero повреждена. Повторите python tools/setup_voices.py'))
        stream.seek(0)
        yield stream
