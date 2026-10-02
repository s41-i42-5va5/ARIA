# ARIA in Codex

ARIA 1.5.5 ships the `aria-project` skill inside its wheel. The skill translates ordinary
requests about a collaborative project's backlog, authors, assignees, activity, and accepted
state into the existing fail-closed ARIA CLI.

## Install

The installer or administrator supplies the public release GitHub App identifiers once:

```powershell
aria codex install `
  --github-client-id <public-client-id> `
  --coordinator-integration-id <public-app-id>
aria codex status
```

The skill is copied to `%USERPROFILE%\.agents\skills\aria-project\SKILL.md`. The provider
profile is stored under the ARIA machine runtime. It contains no access token, refresh token,
private key, browser cookie, or authenticated remote URL.

An exact repeat is a no-op. A changed skill or provider profile requires explicit `--replace`.
Removal refuses to delete a modified skill unless `--force` is supplied and preserves the
provider profile:

```powershell
aria codex remove
```

Codex may need to restart before a newly installed skill appears.

## Natural-language behavior

The skill handles requests such as:

- «Покажи мои задачи»;
- «Покажи свободные задачи»;
- «Добавь идею без исполнителя»;
- «Беру BLG-... »;
- «Кто автор и кто исполнитель?»;
- «Поставь стадию implementation»;
- «Покажи принятый state проекта».

Codex reads the current revision before each mutation. Actor identity comes from the live GitHub
device session, assignee identity comes from `ARIA_TEAM.yaml` after provider read-back, and
developer mutations use the GitHub Issues queue. Codex never edits control documents directly.

If login is required, the skill starts the official GitHub device flow and opens the returned
verification URL in Codex's in-app browser when that capability is available. The browser is only
the user's confirmation screen; ARIA receives the token from the GitHub API and stores it in
Windows Credential Manager. No browser cookies are read.

Canonical completion remains coordinator-only: an ordinary statement that work is finished does
not close the backlog item or update `STATE.yaml`. Merge and required CI must be verified first.

## Current release boundary

Source implementation, wheel inclusion, schema 3 pre-provider bundle, portable side-by-side
Python setup, Windows PowerShell 5.1 clean install/status/reinstall/remove, ownership-safe
uninstall, and local regression are verified. A registered release GitHub App with fixed public
identifiers, exact final manifest read-back, and live private GitHub multi-user acceptance are
still required before release publication.
