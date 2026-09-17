"""Refuse to run two samplers against one database.

Two writers do not corrupt SQLite, but they quietly corrupt the *meaning* of the
data: both probe on their own schedule, so the round spacing halves, the sample
count doubles, and each keeps its own incident state, so one outage can be
recorded twice with different durations. That is exactly what happened once here,
and it is invisible from the dashboard -- it just looks like a busier link.

The lock is held on the database, not the port, because the port is not the thing
being shared: the database is, and `--bind`/`--web-port` can differ between
instances.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class InstanceLock:
    """An exclusive advisory lock on ``<database>.lock``."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(str(db_path) + ".lock")
        self._handle = None

    def acquire(self) -> str | None:
        """Take the lock. Returns ``None`` on success, or the holder's pid."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" rather than "w": opening for truncation would wipe the holder's
        # pid before we discover the lock is taken, so we could not say who has
        # it. Nothing is written until after the lock is won.
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            holder = handle.read().strip()
            handle.close()
            return holder or "unknown"
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return None

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
        except OSError:
            pass
        finally:
            self._handle = None
            # The file is deliberately left behind: advisory locks die with the
            # process, so a stale file is harmless, and unlinking here would
            # open a window where another process locks the old inode while a
            # third creates a new file at the same path.

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
