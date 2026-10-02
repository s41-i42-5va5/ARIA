from __future__ import annotations

import os
import shutil
from pathlib import Path

from aria.errors import ConfigurationError
from aria.github_auth import CLIENT_ID_RE


def resolve_github_askpass(value: Path | None = None) -> Path:
    if value is not None:
        candidate = value
    else:
        located = shutil.which("aria-github-askpass")
        if located is None:
            raise ConfigurationError(
                "aria-github-askpass is not installed beside the ARIA CLI"
            )
        candidate = Path(located)
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError("GitHub AskPass executable is unavailable") from error


def github_git_environment(*, client_id: str, askpass: Path) -> dict[str, str]:
    if CLIENT_ID_RE.fullmatch(client_id) is None or not askpass.is_file():
        raise ConfigurationError("GitHub Git authentication configuration is invalid")
    environment = dict(os.environ)
    environment.update(
        {
            "ARIA_GITHUB_CLIENT_ID": client_id,
            "GIT_ASKPASS": str(askpass),
            "GIT_ASKPASS_REQUIRE": "force",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment
