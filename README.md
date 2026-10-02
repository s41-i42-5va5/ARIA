# // A.R.I.A. 1.5.5

Framework for managing AI development with **Codex** and **Claude Code**: task contracts, project context, verification evidence, acceptance and collaboration through one GitHub repository.

[Website / RU · EN](https://s41-i42-5va5.github.io/aria_dev/) · [Download 1.5.5](https://github.com/s41-i42-5va5/ARIA/releases/tag/v1.5.5)

## Download

| Package | Platform | Download |
|---|---|---|
| Codex | Windows 10/11 x64 | [ARIA 1.5.5 for Codex](https://github.com/s41-i42-5va5/ARIA/releases/download/v1.5.5/ARIA-1.5.5-codex-windows-x64.zip) |
| Claude Code | Windows 10/11 x64 | [ARIA 1.5.5 for Claude Code](https://github.com/s41-i42-5va5/ARIA/releases/download/v1.5.5/ARIA-1.5.5-claude-windows-x64.zip) |

Extract the entire package into its own folder. ARIA includes Python and Git; install and authenticate your AI tool separately. Claude Code also requires a supported Git Bash or WSL environment.

## Quick start

Run from the extracted package folder:

```text
CHECK_RELEASE.cmd
INSTALL.cmd
```

For Codex:

```text
RUN_ARIA.cmd doctor
RUN_ARIA.cmd --help
```

For Claude Code:

```text
RUN_ARIA.cmd claude status
RUN_ARIA.cmd doctor
```

[Codex instructions](releases/1.5.5/codex/ИНСТРУКЦИЯ.md) · [Claude Code instructions](releases/1.5.5/claude/ИНСТРУКЦИЯ.md) · [SHA-256 checksums](releases/1.5.5/SHA256SUMS.txt)

## Source code

`aria/` contains the shared 1.5.5 engine and both integrations. For a source installation, use a Python 3.12 virtual environment and `python -m pip install .`. For the portable Windows installation, use `INSTALL.cmd` from a release download; the source checkout contains release metadata, not the bundled runtime binaries. `pyproject.toml` declares version 1.5.5. The Windows downloads are separate packaged builds with their own immutable manifests; their files have been preserved exactly.

The source snapshot originates from commit `81a258ba62c554cee714592d4986a184c7677328` of the local framework repository. Its engine matches the Claude 1.5.5 package after normalizing line endings. The Codex package originates from commit `4f68e97daba6a361b71265610e6e07962476bee0`.

[Source tests](tests/README.md) · [Architecture](docs/ARCHITECTURE.md) · [User guide](docs/GUIDE.md) · [Codex integration](docs/CODEX_INTEGRATION.md) · [Claude integration](docs/CLAUDE_CODE_INTEGRATION.md) · [Framework reference](docs/FRAMEWORK_REFERENCE.md)

## Verification scope

Package integrity checks pass for both Windows distributions. Their manifests record local source, synthetic integration and portable installation checks. Live multi-user GitHub acceptance and live Claude Code smoke testing remain pending. These downloads are local test candidates; macOS packages are not included.

The original repository history remains available in earlier commits. The previous Claude command-template tree has been superseded by the Python framework source in this version.
