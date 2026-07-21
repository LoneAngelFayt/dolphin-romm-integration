"""Shared test helpers: import broker.py in isolation and build fixtures."""

import importlib.util
import io
import os
import sys
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BROKER_PATH = REPO_ROOT / "root" / "root" / "broker.py"


def _load_broker():
    """Import broker.py once, with env set so module-level constants are sane.

    PUID/PGID are the current user so the module's chown calls succeed without
    root; they are otherwise EPERM and every write path returns an error string.
    """
    os.environ.setdefault("PUID", str(os.getuid()))
    os.environ.setdefault("PGID", str(os.getgid()))
    os.environ.setdefault("BROKER_LOG_LEVEL", "CRITICAL")
    spec = importlib.util.spec_from_file_location("broker_under_test", BROKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


broker = _load_broker()


def reset_session():
    """Return the session dict to its post-import state.

    Every key is listed rather than update()-ing a subset: a leftover
    relaunch_failures count carries into the next test and changes how many
    crashes the monitor tolerates there.
    """
    with broker._session_lock:
        broker._session.update(
            process=None,
            rom_path=None,
            rom_name=None,
            started_at=None,
            is_managed=False,
            save_in_progress=False,
            save_baseline=None,
            relaunch_failures=0,
        )


def make_zip(members: dict[str, bytes], date_time=(2020, 1, 1, 0, 0, 0)) -> bytes:
    """Build a zip in memory. Keys are archive member paths."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=date_time)
            zf.writestr(info, data)
    return buf.getvalue()


def zip_names(content: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {i.filename for i in zf.infolist() if not i.is_dir()}


def write(path: Path, data: bytes = b"x", mtime: float | None = None) -> Path:
    """Create a file with parents, optionally backdating its mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def past(seconds: float = 3600.0) -> float:
    return time.time() - seconds
