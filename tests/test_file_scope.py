from __future__ import annotations

import unittest

from aria.file_scope import path_allowed, scopes_overlap


class FileScopeTests(unittest.TestCase):
    def test_single_star_never_crosses_repository_directory_boundary(self) -> None:
        self.assertTrue(path_allowed("src/public.py", ["src/*"]))
        self.assertFalse(path_allowed("src/private/secret.py", ["src/*"]))

    def test_double_star_is_explicitly_recursive(self) -> None:
        self.assertTrue(path_allowed("src/private/secret.py", ["src/**"]))
        self.assertTrue(scopes_overlap("src/**", "src/private"))


if __name__ == "__main__":
    unittest.main()
