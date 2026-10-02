# ARIA in Claude Code

ARIA 1.5.5 ships a dedicated `aria-project` skill and a local `PreToolUse` protection hook for
Claude Code. It uses the same ARIA engine, GitHub device login, one-repository branch model, local
Coordinator, durable request queue, and replay protection as the Codex package. It does not use or
install an MCP server.

## Install

Claude Code itself must already be installed and authenticated. On Windows, Claude Code also
requires a supported Git Bash or WSL environment. The portable ARIA bundle includes Python and
MinGit for ARIA's own runtime; it does not redistribute Claude Code, Anthropic credentials, or a
Git Bash shell.

Install the integration for the current Windows user:

```powershell
aria claude install `
  --github-client-id <public-client-id> `
  --coordinator-integration-id <public-app-id>
```

The skill is copied to `%USERPROFILE%\.claude\skills\aria-project\SKILL.md`. When
`CLAUDE_CONFIG_DIR` is set, ARIA uses that absolute directory instead. The installer safely merges
one ARIA `PreToolUse` hook into `settings.json`, preserves unrelated user settings and hooks, and
records the exact expected hook in ARIA's private runtime receipt.

`aria claude status` verifies the skill SHA-256, the exact installed hook entry, and the optional
public GitHub provider profile. Repeating install with identical inputs is a no-op. Drift requires
explicit `--replace`.

Remove only the ARIA skill and hook:

```powershell
aria claude remove
```

The public provider profile remains available for reinstall. A locally modified skill requires
`--force` before removal.

## Protection boundary

The hook blocks direct Claude tool writes to ARIA's canonical control documents and direct shell
mutations of the `aria-control` branch. Invalid or malformed matched hook requests fail closed.
Normal source edits and read-only inspection remain allowed. The hook never logs the received
command, token, prompt, or file content.

This is defense in depth, not an operating-system sandbox. Obfuscated commands or another process
running as the same OS user may be outside the hook's visibility. The authoritative boundaries
remain GitHub identity and permissions, protected branch rules, the Coordinator App identity,
signed/hash-bound state, idempotent receipts, and Coordinator-only canonical writes.

## GitHub login

If a GitHub session is missing, Claude runs ARIA's device login commands and shows the official
verification URL and one-time code to the user. Tokens stay in Windows Credential Manager and are
not written to the repository, settings, skill, logs, test artifacts, or release manifest.

## Verification boundary

The release includes deterministic integration, settings-preservation, hook-adversarial, portable
install, full source regression, and synthetic multi-user tests. A live Claude CLI smoke test and
multi-account GitHub acceptance require Claude Code to be installed/authenticated and the owner to
authorize the isolated private GitHub repositories and App settings.
