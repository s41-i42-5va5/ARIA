# A.R.I.A. 1.5.5 publication verification

Date: 2026-10-02.

- Shared source snapshot: `81a258ba62c554cee714592d4986a184c7677328` from the local framework history.
- All source engine files match the Claude 1.5.5 distribution after normalizing CRLF/LF line endings.
- `aria doctor` passes with Python 3.12.10 and version 1.5.5, using an isolated runtime directory.
- Current engine/integration regression: 460 tests, zero failures/errors/skips, 377.313 seconds.
- Source installer/uninstaller static checks: 2 tests passed.
- Historical 1.5.4 artifact-manifest check: 1 test passed using the existing local 1.5.4 fixture; historical binary fixtures are not part of this source publication.
- Codex portable manifest: 113 immutable files, zero failures.
- Claude portable manifest: 117 immutable files, zero failures.
- Both download archives pass ZIP integrity verification; every extracted byte matches the original distribution files.
- Credential-pattern scan of the staged source found no matching private-key, GitHub-token or Anthropic-key patterns.

These checks verify the publication contents. They do not certify live multi-user GitHub acceptance, a live Claude Code session, macOS compatibility or an independent physical-machine installation. Existing release manifests retain their local test candidate status and pending external gates.
