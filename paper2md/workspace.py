"""
Private, self-deleting working directories for ``--no-cache`` requests.

They are created under ``NO_CACHE_WORKDIR`` (default: /dev/shm, a RAM-backed
tmpfs, so nothing reaches a persistent disk) with mode 0700 and the PID of the
bot process in their name, and are removed when the request finishes, also on
errors.  Leftovers of a process that no longer exists (e.g. after a crash) are
removed by ``purge_stale_workdirs``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

NO_CACHE_PREFIX = "p2m-nocache-"
_PID_RE = re.compile(rf"^{re.escape(NO_CACHE_PREFIX)}(\d+)-")


def no_cache_root() -> Path:
    from .config import Settings

    return Settings.from_env().no_cache_dir or Path(tempfile.gettempdir())


def create_private_workdir() -> Path:
    """Create a new directory only this user can read (mode 0700)."""
    root = no_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{NO_CACHE_PREFIX}{os.getpid()}-", dir=root))


def remove_tree(path: str | os.PathLike) -> bool:
    """Delete ``path`` recursively; return True when nothing is left."""
    path = Path(path)
    for attempt in range(3):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(0.5 * (attempt + 1))  # e.g. a file that was still being closed
    logger.error("Could not completely remove the temporary directory %s", path)
    return False


@contextmanager
def ephemeral_workdir() -> Iterator[Path]:
    """Yield a fresh private directory that is deleted on exit."""
    path = create_private_workdir()
    try:
        yield path
    finally:
        remove_tree(path)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but belongs to someone else
    return True


def purge_stale_workdirs(max_age_seconds: float = 24 * 3600) -> int:
    """Remove this user's no-cache directories left by dead processes.

    A directory is removed when the process that created it no longer exists,
    or when it is older than ``max_age_seconds`` (guards against PID reuse).
    """
    root = no_cache_root()
    if not root.is_dir():
        return 0
    removed = 0
    now = time.time()
    for path in root.glob(f"{NO_CACHE_PREFIX}*"):
        try:
            stat = path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink() or not path.is_dir() or stat.st_uid != os.getuid():
            continue
        match = _PID_RE.match(path.name)
        pid = int(match.group(1)) if match else None
        if pid == os.getpid() and now - stat.st_mtime <= max_age_seconds:
            continue  # in use by this process
        if (pid is None or not process_alive(pid) or now - stat.st_mtime > max_age_seconds) and remove_tree(path):
            removed += 1
    if removed:
        logger.info("Removed %d stale no-cache working directories", removed)
    return removed
