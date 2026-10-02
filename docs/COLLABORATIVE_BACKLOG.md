# ARIA collaborative backlog v2

Status: local schema, deterministic authorization/transition engine, file-backed coordinator
persistence, GitHub request-queue delivery, recoverable protected-branch flush, and one bounded
coordinator polling pass, and current-user Windows scheduling are implemented. Migration from
offline backlog v1 remains a separate unfinished stage.

## Identity and authorship

Every identity contains provider, immutable provider user id, and mutable username/display
name snapshots. The authenticated actor is matched to the active membership projection by
provider plus user id; stored mutable snapshots come from that projection, not from request
content.

An item stores its real `creator` and optional `assignee`. Every audit event separately stores
`requested_by` and `committed_by`. The coordinator therefore remains the technical writer of
canonical state and never replaces the human author of an idea.

## Requests and permissions

Schema v1 requests support `add`, `assign`, `claim`, `block`, and `complete`. The coordinator
supplies authenticated identity, active members, action permissions, expected revision, its
service identity, and commit time. A request cannot authorize itself.

An added idea receives a stable sequential `BLG-......` id, `open` status, and null assignee.
Assign resolves the target through active membership. Claim, block, and complete enforce the
assignee boundary. Evidence-required items cannot complete without evidence references.

## Integrity and idempotency

The backlog maintains optimistic revision, unique request ids, deterministic item/source ids,
an item-state hash, and a chained event hash. Exact request replay is a no-op; reuse of the same
request id with changed content fails closed. Tampering with current item text, actor identity,
or the event chain is detected during validation.

The hash chain provides document integrity checks but is not an absolute same-user write
boundary: protected control-branch commits and coordinator-only GitHub permissions remain
required for the complete collaborative trust model.

## Local coordinator persistence

`BACKLOG.yaml` v2 writes are serialized by a project-scoped OS lock and expected revision.
Before changing the canonical file, the coordinator atomically writes a transaction journal
containing validated before/after documents and hashes. Recovery rolls forward from either a
verified preimage or an already-installed result. A missing preimage or bytes matching neither
prepared state fail closed.

Because request id and fingerprint are part of the backlog audit chain, retry after either
crash window cannot create a second item or event. The request timestamp is audit metadata,
not part of the idempotency fingerprint, so a later retry of the same authenticated request
does not become a conflicting request solely because its transport timestamp changed.

## Coordinator-host commands

The registered collaborative project exposes:

```text
aria collaboration backlog-list --project demo
aria collaboration backlog-mine --project demo --github-client-id <client-id>
aria collaboration backlog-add --project demo --github-client-id <client-id> \
  --coordinator-integration-id <app-id> --expected-revision 0 \
  --title "Idea" --description "Unassigned idea" --priority P1
aria collaboration backlog-assign --project demo ... --item BLG-000001 \
  --assignee-user-id <immutable-github-user-id>
aria collaboration backlog-claim --project demo ... --item BLG-000001
aria collaboration backlog-block --project demo ... --item BLG-000001 --reason "Waiting"
aria collaboration backlog-complete --project demo ... --item BLG-000001 \
  --evidence-ref ci:run-123
```

The mutation commands default to `--delivery github-queue`: a developer user access token
creates an issue whose canonical JSON body is limited to 64 KiB and bound to project,
request id, expected revision, operation, and submission time. The issue read-back must retain
the exact body and immutable GitHub author id. An exact retry reuses the existing open issue;
the developer never receives or uses the Coordinator App private key.

On the coordinator host:

```text
aria collaboration queue-process --project demo --github-client-id <client-id> \
  --coordinator-integration-id <app-id> --max-requests 20
```

For unattended orchestration, the scheduler invokes one bounded, retry-safe pass:

```text
aria collaboration coordinator-run-once --project demo \
  --github-client-id <client-id> --coordinator-integration-id <app-id> \
  --max-requests 20 --max-pull-requests 20
```

The pass first fast-forwards the clean local control worktree to the App-confirmed remote head,
then processes queued requests, and finally discovers merged `dev` pull requests with an exact
`ARIA-Backlog` binding. Pull requests are applied in merge order through the same required-check,
ancestry, identity, backlog, state/history, and recoverable control-commit boundary as manual
`state-sync`. An invalid earlier pull request stops later state publication instead of skipping a
gap. The coordinator administrator installs its recurring invocation with:

```text
aria coordinator install --project demo --github-client-id <client-id> \
  --coordinator-integration-id <app-id> --interval-minutes 1
aria coordinator status --project demo
```

The task is hash-bound to strict local configuration and the installed executable, uses the
current user's Credential Manager, ignores overlapping launches, and stores only a secret-free
last-run receipt. `coordinator trigger` requests an immediate Task Scheduler run; `coordinator
remove` removes scheduling without deleting project/runtime data. Because it deliberately uses
`InteractiveToken`, the Windows coordinator host must remain signed in.

The processor requires a live admin session, a coordinator-only ruleset, the App installation
session, and the complete live collaborator projection. It re-reads each issue immediately
before processing, resolves its author by immutable user id against current collaborators, and
then runs the same backlog authorization/transaction/remote-flush path. Success is commented
with request id, accepted body SHA-256, and control commit SHA before the issue is closed.
If application fails, the issue remains open so the same idempotent request can be retried.

Each mutation re-reads the authenticated GitHub user, immutable repository id, live role, and
coordinator-only ruleset. Permissions come from validated `ACCESS.yaml`; active identities and
assignment targets come from `ARIA_TEAM.yaml`. The caller cannot supply creator or
`requested_by`. If `--request-id` is omitted, ARIA derives a stable id from actor, action,
payload, and expected revision so an ordinary retry resumes the same operation.

After local crash-safe installation, the full nine-document control inventory is committed by
the Coordinator App with expected-head CAS, remote read-back, exact fetch, and local worktree
reset. The command returns the item, backlog revision, request id, and control commit SHA.

`--delivery coordinator-local` is an explicit coordinator-host maintenance path. Copying the
App key to developer workstations is unsupported. Both the queue-only command and the combined
run-once worker use Credential Manager AskPass for private Git fetches; tokens never enter Git
arguments or repository configuration. Webhook wake-up remains optional; recurring polling is
provided by the installed Windows task.
