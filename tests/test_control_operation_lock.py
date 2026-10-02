from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from aria.control_worktree_sync import serialized_control_operation


class ControlOperationLockTests(unittest.TestCase):
    def test_three_coordinator_operations_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = SimpleNamespace(runtime_root=Path(temporary) / "project")
            barrier = threading.Barrier(3)
            guard = threading.Lock()
            active = 0
            maximum = 0
            errors: list[BaseException] = []

            def operation() -> None:
                nonlocal active, maximum
                try:
                    barrier.wait(timeout=5)
                    with serialized_control_operation(project):
                        with guard:
                            active += 1
                            maximum = max(maximum, active)
                        time.sleep(0.05)
                        with guard:
                            active -= 1
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=operation) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(maximum, 1)
            self.assertEqual(active, 0)


if __name__ == "__main__":
    unittest.main()
