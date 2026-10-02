from __future__ import annotations

import json
import errno
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from aria.errors import AriaError


def json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if path.read_bytes() != content:
            raise AriaError(f"Atomic write read-back mismatch: {path}")
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_bytes(path, json_bytes(payload))


@contextmanager
def exclusive_lock(
    path: Path,
    *,
    timeout_seconds: float = 5.0,
    stale_seconds: float = 600.0,
) -> Iterator[None]:
    """Acquire a process-owned OS lock without stealing an old live lock.

    The lock file is only a stable inode/handle and may remain after a crash.
    The kernel releases the actual byte-range/flock ownership when the process
    exits, so a dead owner can be recovered immediately and a long-running live
    owner cannot be stolen merely because its mtime is old.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    token = uuid.uuid4().hex
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR)
    acquired = False
    _ = stale_seconds  # Kept for API compatibility; OS ownership replaces leases.
    while not acquired:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                    os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            busy_codes = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
            if error.errno not in busy_codes:
                os.close(descriptor)
                raise
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise AriaError(f"Timed out waiting for lock: {path}")
            time.sleep(0.05)
    try:
        owner = json.dumps(
            {"pid": os.getpid(), "token": token, "acquired_at": time.time()},
            separators=(",", ":"),
        ).encode("ascii")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, owner)
        os.fsync(descriptor)
        yield
    finally:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def append_jsonl(path: Path, payload: object) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    lock_path = path.with_suffix(path.suffix + ".lock")
    with exclusive_lock(lock_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
