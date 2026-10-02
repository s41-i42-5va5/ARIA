from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aria.errors import AriaError
from aria.io import exclusive_lock


class LockTests(unittest.TestCase):
    def test_os_lock_never_steals_live_owner_and_recovers_dead_owner_immediately(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            lock = root / "project.lock"
            ready = root / "ready"
            child_code = """
import sys
import time
from pathlib import Path
from aria.io import exclusive_lock

lock = Path(sys.argv[1])
ready = Path(sys.argv[2])
with exclusive_lock(lock, timeout_seconds=2):
    ready.write_text("locked", encoding="utf-8")
    time.sleep(60)
"""
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", child_code, str(lock), str(ready)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                for _ in range(100):
                    if ready.is_file():
                        break
                    if child.poll() is not None:
                        break
                    time.sleep(0.05)
                if not ready.is_file():
                    stdout, stderr = child.communicate(timeout=2)
                    self.fail(f"lock holder did not start: {stdout}\n{stderr}")
                old = time.time() - 3600
                os.utime(lock, (old, old))
                with self.assertRaisesRegex(AriaError, "Timed out waiting for lock"):
                    with exclusive_lock(
                        lock,
                        timeout_seconds=0.25,
                        stale_seconds=0.01,
                    ):
                        self.fail("a live OS lock was stolen")
            finally:
                if child.poll() is None:
                    child.terminate()
                child.communicate(timeout=5)

            # The lock file deliberately survives, but kernel ownership died
            # with the process and must be reusable without a 600-second lease.
            self.assertTrue(lock.is_file())
            with exclusive_lock(lock, timeout_seconds=1, stale_seconds=99999):
                pass
            owner = lock.read_text(encoding="ascii")
            self.assertIn(f'"pid":{os.getpid()}', owner)


if __name__ == "__main__":
    unittest.main()
