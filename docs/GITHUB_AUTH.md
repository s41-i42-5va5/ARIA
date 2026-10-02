# ARIA GitHub App authentication

Status: local device-flow protocol, Windows Credential Manager vault, token refresh, session
token source, and public begin/complete/status/logout CLI commands are implemented. A
registered production GitHub App client id, automatic Codex in-app-browser handoff, and live
provider E2E are not complete.

ARIA requests a repository-bound device code from `https://github.com/login/device/code`,
shows only the official verification URL and user code, and polls
`https://github.com/login/oauth/access_token`. Polling observes GitHub's minimum interval,
adds five seconds after `slow_down`, stops at expiry, and handles terminal errors without
including response bodies, device codes, or tokens in safe errors.

The browser is only a user confirmation screen. It does not supply cookies, DOM data, or a
browser session to ARIA. Codex may open the returned official URL in its in-app browser; ARIA
continues polling through its own HTTPS transport.

Access and rotating refresh tokens are serialized only into a Windows generic credential under
`ARIA-Codex/github/<client-id>`. The vault uses `CredWriteW`, `CredReadW`, and `CredDeleteW`;
tokens are not written to project/runtime files. Near expiry, the session manager refreshes
without a client secret, atomically replaces the stored token pair, and supplies only the
in-memory access token to `GitHubHttpClient`.

An actual login still requires a GitHub App registration with device flow enabled, expiring
user tokens enabled, least-privilege repository permissions, and a release-owned public client
id. The client id is not a secret; no client secret belongs in the desktop client.

Current developer CLI workflow:

```text
aria github-auth login-begin --client-id <public-client-id> --repository-id <numeric-id>
aria github-auth login-complete --client-id <public-client-id> --repository-id <numeric-id>
aria github-auth status --client-id <public-client-id>
aria github-auth logout --client-id <public-client-id> --repository-id <numeric-id>
```

`login-begin` returns `browser_handoff: codex-in-app-required`; it does not claim the browser
was opened. Codex must open the returned official verification URL, then invoke
`login-complete`. A release-owned client id will remove the developer-only `--client-id` step.

`collaboration plan/enable` accept the same developer client id and construct their GitHub
provider adapter from the stored session. Remote credentials are rejected, access tokens are
never command arguments, and missing/expired login becomes a safe provider blocker before any
remote mutation.
