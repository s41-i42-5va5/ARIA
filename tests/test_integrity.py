from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aria.integrity import engine_state


class EngineIntegrityTests(unittest.TestCase):
    def test_framework_identity_prunes_non_operational_dependency_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "aria").mkdir()
            (root / "aria" / "__init__.py").write_text(
                "__version__ = 'test'\n", encoding="utf-8"
            )
            (root / ".aria-root").write_text("aria\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                "[project]\nname='aria-test'\n", encoding="utf-8"
            )
            (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
            ignored = root / ".venv" / "Lib" / "site-packages" / "huge"
            ignored.mkdir(parents=True)
            for index in range(2000):
                (ignored / f"module_{index}.py").write_text(
                    "raise AssertionError\n", encoding="utf-8"
                )

            real_walk = __import__("os").walk
            visited: list[Path] = []

            def observed_walk(path):
                for current, directories, files in real_walk(path):
                    visited.append(Path(current))
                    yield current, directories, files

            with patch("aria.integrity.os.walk", side_effect=observed_walk):
                started = time.perf_counter()
                identity = engine_state(root)
                elapsed = time.perf_counter() - started

            self.assertEqual(identity["count"], 4)
            self.assertLess(
                elapsed,
                2.0,
                f"engine identity exceeded the 2.0s regression limit: {elapsed:.3f}s",
            )
            self.assertFalse(
                any(".venv" in path.parts for path in visited),
                f"engine identity visited excluded dependency tree: {visited}",
            )


if __name__ == "__main__":
    unittest.main()
