---
name: aria-project
description: Manage an ARIA collaborative project's shared backlog, assignments, activity, GitHub login, and accepted state from natural-language requests, including Russian task and backlog requests. Use when the user asks about ARIA project tasks, ideas, authors, assignees, work stages, or project status.
---

# ARIA Project

Translate the user's request into the installed `aria` CLI. Keep GitHub identity, permissions,
optimistic revision, idempotency, and coordinator enforcement inside ARIA; never edit
`BACKLOG.yaml`, `ACTIVITY.yaml`, `STATE.yaml`, `ARIA_TEAM.yaml`, or `HISTORY.jsonl` directly.

## Establish context

1. If the user did not name a project, run `aria projects`. Continue automatically only when
   exactly one collaborative project is registered; otherwise ask which project to use.
2. Run `aria codex status`. Require `ok: true` and `provider_profile_configured: true` before an
   authenticated GitHub action. Read the public `github_client_id` and
   `coordinator_integration_id` from its JSON; do not ask the user to re-enter them.
3. Read current revisions immediately before a mutation. Never reuse a revision from an older
   turn or retry a stale mutation by guessing a new revision.

## Read requests

- My tasks: `aria collaboration backlog-mine --project <id> --github-client-id <client> --coordinator-integration-id <app>`.
- Free tasks or shared backlog: `aria collaboration backlog-list --project <id>` and filter
  active items with `assignee: null` when the user asked for free tasks.
- Author and assignee: use `backlog-list`; report `creator`, `assignee`, status, and item id.
- Project status: combine `state-status`, `team-status`, and `activity-list`. Keep accepted state
  separate from unfinished activity.
- My current activity: `activity-mine` with the authenticated profile identifiers.

## Mutations

Before every backlog mutation, run `backlog-list` and use its exact current revision. Use the
default GitHub queue delivery so developer machines never need the Coordinator App private key.

- Add an unassigned idea: `backlog-add` with title, concise description, priority when supplied,
  and current `--expected-revision`. Do not assign it unless the user explicitly asks.
- Claim: `backlog-claim --item <BLG-ID>` with current revision.
- Assign: resolve the requested person from `team-status` and pass the immutable
  `--assignee-user-id`; never accept a typed numeric id without team read-back.
- Block: `backlog-block --item <BLG-ID> --reason <short reason>` with current revision.
- Set developer stage: read `activity-list`, use its current revision, determine the actual Git
  branch from the registered checkout, then call `activity-set`. Only publish a meaningful stage
  change. Never claim test success from the `testing` stage.

Do not call `backlog-complete` for ordinary development completion. Canonical completion and
accepted state are published by the coordinator only after a verified merge and required CI.

## GitHub device login

When ARIA reports that no valid GitHub session exists, obtain the repository id from the
registered collaborative project and run `aria github-auth login-begin` with the configured
client id. Open the returned official verification URL in Codex's in-app browser, enter the
returned user code, and let the user complete GitHub confirmation. Do not inspect or reuse browser
cookies. Then run `login-complete` and repeat the original action once. If the in-app browser tool
is unavailable, show the official URL and code instead of opening an external browser.

## Errors and safety

- Permission denial: explain which action is not allowed; do not retry as another identity.
- Stale revision or competing claim: refresh once and report the actual winner/current state.
- Offline activity delivery: report that the event is in the local outbox; after connectivity
  returns, use `activity-outbox-flush` under the same GitHub identity.
- Provider or CI failure: do not edit canonical files and do not describe the task as accepted.
- Never print tokens, private keys, credential records, raw authenticated URLs, or full prompts.
