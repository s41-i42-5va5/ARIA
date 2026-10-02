from __future__ import annotations

import os
import sys

from aria.errors import AriaError, ConfigurationError
from aria.github_auth import CLIENT_ID_RE, GitHubAuthHttpTransport
from aria.github_session import (
    CredentialBackend,
    GitHubCredentialVault,
    GitHubSessionManager,
    WindowsCredentialBackend,
)


def askpass_response(
    prompt: str,
    *,
    client_id: str,
    credential_backend: CredentialBackend | None = None,
) -> str:
    if not isinstance(prompt, str) or not prompt:
        raise ConfigurationError("Git AskPass prompt is invalid")
    if CLIENT_ID_RE.fullmatch(client_id) is None:
        raise ConfigurationError("Git AskPass client id is invalid")
    normalized = prompt.casefold()
    if "username" in normalized:
        return "x-access-token"
    if "password" not in normalized:
        raise ConfigurationError("Git AskPass received an unsupported prompt")
    vault = GitHubCredentialVault(
        backend=credential_backend or WindowsCredentialBackend(),
        client_id=client_id,
    )
    return GitHubSessionManager(
        vault=vault, transport=GitHubAuthHttpTransport()
    ).access_token()


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) != 1:
            raise ConfigurationError("Git AskPass requires exactly one prompt")
        client_id = os.environ.get("ARIA_GITHUB_CLIENT_ID", "").strip()
        response = askpass_response(arguments[0], client_id=client_id)
        print(response)
        return 0
    except AriaError as error:
        print(f"ARIA Git credential error: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
