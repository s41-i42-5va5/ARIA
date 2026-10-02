# Source tests

Use Python 3.12 with the dependencies declared in `pyproject.toml` installed. The source suite contains 463 tests.

`test_setup_installer.py` contains a historical artifact check for the 1.5.4 wheelhouse. A complete `releases/1.5.4` fixture is required to run that specific test; historical runtime binaries are not included in this 1.5.5 source checkout. Its two installer-script tests can run without that fixture.

For the current engine and integrations:

```python
import unittest

def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item

suite = unittest.defaultTestLoader.discover("tests")
tests = [test for test in flatten(suite)
         if not test.id().endswith("WindowsSetupInstallerTests.test_release_manifest_matches_every_offline_artifact")]
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(tests))
raise SystemExit(not result.wasSuccessful())
```

These tests use disposable local projects and synthetic services. They do not prove live GitHub or live Claude Code acceptance.
