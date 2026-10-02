# ARIA activity contract

Status: locally implemented end-to-end activity lifecycle: developer delivery, offline outbox,
provider-verified GitHub PR stages, coordinator completion, durable recovery, and bounded active
receipt ledger with a hash-chained archive. Live private GitHub multi-user acceptance remains.

`ACTIVITY.yaml` is a non-canonical snapshot of active work. It never publishes an
unfinished capability into `STATE.yaml` and never proves that tests passed.

## Authority

- `local_aria` may publish `analysis`, `planning`, `implementation`, `testing`,
  `blocked`, and `ready_for_pr`.
- `github` may publish `in_review` and `waiting_for_ci` with a PR number.
- `coordinator` alone may publish `completed`, and only after `waiting_for_ci`.
- The coordinator supplies the authenticated transport source and provider identity. The
  event cannot authorize its own source or actor; provider username read-back replaces a
  stale username snapshot.

## Snapshot behavior

The snapshot has one entry per active backlog task and is sorted by task id. Meaningful
stage changes increment its optimistic revision. A completed event removes the active
entry; canonical completion belongs in backlog/state/history.

The last 256 event ids provide a bounded snapshot idempotency window. The local coordinator
also keeps a durable request receipt ledger under the runtime root, so replay protection
does not depend only on this snapshot.

An older `observed_at` cannot overwrite a newer stage. Two different events at exactly
the same observed time conflict fail-closed. `received_at` is coordinator time and remains
separate from developer-observed time.

Staleness is derived from the last received time. It changes only the `stale` flag and
does not infer task completion, blocking, test success, or developer intent.

## Local coordinator persistence

The coordinator serializes writes with a project-scoped OS lock and requires the caller's
expected activity revision. A durable receipt binds request id, correlation id, event id,
trusted request fingerprint, result, and resulting revision. Reusing a request id with
different content fails closed; an exact retry is a no-op even after the snapshot's bounded
event-id window.

`ACTIVITY.yaml` and the private receipt ledger are changed through a prepared transaction
journal. If a process stops after writing only one document, the next lock owner verifies
both files against the prepared before/after hashes and rolls the transaction forward. A
file matching neither state blocks recovery instead of being overwritten. The runtime
ledger and journal are not control-branch documents and do not contain provider tokens.

## Coordinator-host commands

```text
aria collaboration activity-list --project demo
aria collaboration activity-mine --project demo --github-client-id <client-id>
aria collaboration activity-set --project demo --github-client-id <client-id> \
  --coordinator-integration-id <app-id> --expected-revision 0 \
  --task BLG-000001 --stage implementation --branch work/yura --note "Writing code"
```

`activity-set` re-reads the GitHub actor, repository membership/role, control protection,
`ACCESS.yaml`, `ARIA_TEAM.yaml`, and `BACKLOG.yaml`. Only the active assignee may publish a
developer stage and only a role with `activity.write` may submit. The event actor is built by
ARIA from provider read-back, not accepted from the CLI.

The automatic event/request id is stable for actor, task, stage, branch, note, and expected
revision. `observed_at` is audit metadata outside the receipt fingerprint, so an ordinary retry
with a later transport timestamp remains the same request. The local activity/receipt
transaction then uses the shared recoverable Coordinator App flush for `aria-control`.

`activity-set` defaults to GitHub request-queue delivery, so a developer workstation needs only
its user device session. The coordinator uses the same `queue-process` command described for
backlog requests and applies the event only after live collaborator and assignee read-back.
`--delivery coordinator-local` is reserved for the coordinator host.

After one successful identity read-back, a provider/network failure stores the same minimized
request in a bounded machine-runtime outbox. No canonical document is changed offline. Flush
requires the current GitHub device session to resolve to the same immutable user id, and GitHub
Issue author read-back verifies that identity again:

```text
aria collaboration activity-outbox-status --project demo
aria collaboration activity-outbox-flush --project demo \
  --github-client-id <client-id>
```

`coordinator-run-once` lists bound open PRs and publishes `in_review`; a bound merged PR is
published as `waiting_for_ci` before state acceptance. Repository id, author id, branch,
membership, assignee, permissions, and control protection are all read back. Failed or missing
required checks leave backlog/state unchanged and activity waiting. Successful merge plus strict
required checks closes backlog, publishes state/history, and emits coordinator `completed`,
which removes the task from the active snapshot.

The active receipt ledger compacts after 1024 entries to 512. Older receipts move into an
atomic hash-chained runtime archive and remain part of duplicate-request detection. A crash
between archive and ledger installation is normalized on the next request; conflicting or
tampered active/archive records fail closed.

## Data minimization

Events contain project/task ids, provider identity, stage, branch, optional PR number,
one short single-line note, observed time, and event id. Multiline content and obvious
GitHub/OpenAI token or private-key patterns are rejected. Source code, file contents,
full prompts, raw logs, credentials, and browser sessions are outside the contract.
