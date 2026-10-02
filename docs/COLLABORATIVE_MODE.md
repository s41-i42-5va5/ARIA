# ARIA collaborative mode contract

Status: locally implemented collaborative foundation, offline-to-collaborative migration,
verified Windows recurring-task boundary, and locally accepted schema 3 pre-provider package.
Release GitHub App registration, live private GitHub acceptance, and final provider-bound
publication remain pending.

## Mode boundary

ARIA has two non-interchangeable project modes:

- `offline` is the existing ARIA 1.5.5 local identity and governance mode. Existing
  projects remain readable without `CONTROL.yaml` and are never migrated implicitly.
- `collaborative` uses provider identity, provider membership, a protected
  `aria-control` branch, and ARIA Coordinator as the only canonical writer.

A project cannot combine the local identity authority with collaborative canonical
writes. Presence of `CONTROL.yaml` declares collaborative mode and validation fails
closed when any authority value is weakened or any unknown field is present.

## `CONTROL.yaml` schema version 1

```yaml
schema_version: 1
kind: aria-collaboration-control
project_id: demo
mode: collaborative
provider:
  kind: github
  repository_id: "123456789"
git:
  remote: origin
  integration_branch: dev
  control_branch: aria-control
authority:
  identity: provider
  membership: provider
  canonical_writer: coordinator
  direct_control_push: false
documents:
  project: 1
  backlog: 2
  activity: 1
  state: 2
  team: 2
  access: 2
migration:
  from_mode: offline
  explicit: true
```

`provider.repository_id` is the provider's immutable repository identifier. A clone
URL or repository name is not a substitute. The provider adapter id is extensible,
but an adapter must exist and verify identity, membership, repository identity, and
branch protection before any remote mutation.

The provider boundary returns only safe read-back: immutable user id, mutable username
and display-name snapshots, active membership with roles, immutable repository id, and
branch-protection facts. Access tokens, refresh tokens, client secrets, and raw Git
remote URLs are not fields in this contract. Adapter implementations own authenticated
sessions and must translate provider failures into a safe ARIA error.

### GitHub adapter boundary

The first provider implementation targets GitHub.com REST API version `2026-03-10`.
It reads the authenticated user, repository metadata, effective repository permission,
active rules for `aria-control`, and the full matching ruleset. Numeric GitHub user and
repository ids are treated as immutable identity; login and display name remain snapshots.

Protection is accepted only when one active branch ruleset contains both `creation` and
`update`, and its only `always` bypass actor is the configured ARIA Coordinator GitHub App
integration id. Missing rules, incomplete bypass read-back, an extra user/team/admin bypass,
or a missing integration id all produce `UNPROTECTED`.

The HTTP transport accepts tokens only from an injected session source, disables redirects,
limits response size, pins the API version, and never includes token or response body in a
safe CLI error. OAuth device authorization, polling/refresh, and Windows Credential Manager
storage and public CLI session wiring are implemented locally. Release GitHub App registration,
bundled client configuration, and live provider E2E are not complete. With
`--github-client-id`, the public collaboration CLI now
builds the adapter from Windows Credential Manager and performs identity/membership/protection
read-back without a token argument. Without a configured session it fails safely as
`PROVIDER_ADAPTER_UNAVAILABLE`.

### Team projection boundary

The GitHub adapter reads the complete repository collaborator list with `affiliation=all`
and pagination, after re-reading the immutable numeric repository id. `ARIA_TEAM.yaml`
schema v2 keys each person by provider plus immutable user id. Login and display name are
snapshots; username changes append to history without changing ownership.

A missing previously active collaborator is retained as inactive with empty roles and a
`revoked_at` timestamp. Reappearance reactivates the same identity. Every refresh uses an
expected revision, an idempotent sync id, a provider-snapshot hash, and a chained audit event.
The team coordinator serializes local writers with a project lock and uses a prepared
transaction plus read-back for deterministic crash recovery. A repository administrator
with a saved GitHub device session can run:

```text
aria collaboration team-status --project demo
aria collaboration team-sync --project demo --github-client-id <client-id> \
  --coordinator-integration-id <integration-id> --expected-revision 0
```

`team-sync` re-verifies the authenticated actor, repository id, active admin membership,
and coordinator-only protection before writing. It then submits the complete validated
control-document set through the Coordinator GitHub App with expected-head CAS. The remote
commit SHA and document hashes are journaled before local reset; a retry after a crash rolls
the same operation forward, fetches the exact remote commit, verifies it, and makes the
control worktree clean. A generated sync id is deterministic for the target revision and
provider snapshot so an ordinary CLI retry can recover without a manually supplied id.
Tokens are not accepted as arguments.
Recurring scheduled refresh is implemented through the current-user coordinator task. A revoke
webhook wake-up remains optional because polling already revokes a missing collaborator on the
next sync. Bundled release App configuration and live GitHub E2E are still pending.

### Coordinator GitHub App session

The exact release registration fields and permission matrix are fixed in
[`GITHUB_APP_RELEASE_SETUP.md`](GITHUB_APP_RELEASE_SETUP.md). Registration and installation are
external GitHub mutations and remain confirmation-gated.

User device tokens identify people and authorize requests, but they are not used to write
the coordinator-only branch. ARIA has a separate GitHub App installation session. The App
private key is imported once by the coordinator administrator into Windows Credential
Manager and is never stored in the project, Git, CLI output, or developer workstations:

```text
aria github-app configure --app-id <app-id> --private-key <downloaded-pem>
aria github-app status --app-id <app-id>
aria github-app remove --app-id <app-id>
```

ARIA signs a bounded RS256 App JWT in memory, resolves the repository installation, and
requests a one-repository installation token with `contents: write`, `issues: write`,
`metadata: read`, `checks: write`, and read-only `administration`, `pull_requests`, and `statuses`.
Issues permission is reserved for the authenticated remote request queue; the additional
the App publishes `ARIA integration` on the exact signed PR head, then verifies the integration policy, merged PR, and exact commit checks;
the short-lived installation token is cached only in process memory.
The remote control writer uses GitHub's Git Data API with expected-head CAS, creates an
orphan `aria-control` history, never force-updates it, and verifies the resulting ref.
The writer is wired into `collaboration enable`. Initial Git objects are prepared before
the ref update, their SHA is persisted in a local transaction journal, and ref installation
is idempotent. The transaction then fetches the exact commit, creates the adjacent worktree,
verifies every control-document hash, and registers the project. Recovery resumes from the
last durable phase. A repeated enable is a verified no-op only when contract, document
inventory, local/remote head, worktree, and registry all agree.

Subsequent coordinator operations use the same prepare/install/read-back boundary. A
project-scoped transaction journal retains the operation id, previous head, prepared commit,
and exact hashes of all nine documents. The local control worktree is hard-reset only after
the App-written remote ref and a fetched remote-tracking ref both match that prepared commit.
This reset is restricted to the separately validated control worktree; product code is never
its target. A clean worktree whose HEAD differs from the coordinator remote is rejected as
drift instead of being reported as synchronized.

### Remote request queue

Developer mutations use repository Issues as an authenticated transport. The GitHub App must
be configured with `Issues: read and write` for user and installation tokens. The developer
submits strict canonical JSON with a user device session; GitHub supplies the immutable issue
author id. The coordinator polls with its App installation session, resolves that id against a
fresh collaborator list, executes through the existing permission and CAS boundaries, comments
the accepted body/control commit hashes, and closes the issue. No App private key leaves the
coordinator host. This queue is control transport, not the canonical backlog: `BACKLOG.yaml` in
protected `aria-control` remains the source of truth.

Code branches use a namespace that does not collide with the integration ref: `dev` is the
integration branch, while developer branches are `work/<username>`. `dev/<username>` is
invalid when `dev` already exists because Git cannot store a ref as both a file and directory.

## Creating a new project

After GitHub device login and Coordinator App key configuration, the administrator first runs
the read-only boundary:

```text
aria project create-plan --project demo --owner acme \
  --repository-name product --code-root C:\Projects\product \
  --github-client-id <client-id> --coordinator-integration-id <app-id>
```

The plan authenticates the GitHub actor, confirms that the repository does not exist, checks
that the separate local destinations are free, and returns a deterministic `plan_sha256`.
Creation requires the exact same arguments, that digest, and `--confirm`:

```text
aria project create --project demo --owner acme \
  --repository-name product --code-root C:\Projects\product \
  --github-client-id <client-id> --coordinator-integration-id <app-id> \
  --expected-plan-sha256 <sha256> --confirm
```

Repositories are private by default; `--public` is explicit. The authenticated user token
creates the repository with Issues enabled and requires GitHub Administration write access.
ARIA creates one verified README commit on `dev`, pushes through the same Credential Manager
AskPass boundary as join, and then invokes the protected `aria-control` workflow. The final
result includes the immutable repository id, initial commit, control commit, registration,
and a passing collaborative doctor.

An exact phased journal resumes after local initialization or `dev` push and rejects changed
arguments, a substituted checkout, or a repository receipt mismatch. A process loss in the
narrow interval after GitHub accepts repository creation but before its receipt is journaled
is deliberately fail-closed because GitHub repository creation has no idempotency key; the
existing remote is never adopted silently. Live private-repository and App-installation
acceptance remains a release gate.

## Ideas, owner triage, and task leases

Any active contributor may submit a free idea with `backlog-add`. The authenticated GitHub
user id, not a supplied username, is stored as the creator. An administrator then uses
`backlog-triage` to turn the idea into a task and must provide the assignee, priority,
requirements, acceptance criteria, dependencies, and a non-empty repository-relative file
scope. Triage is an owner/admin permission; assignment alone cannot bypass it.

`backlog-mine` returns the authenticated developer's tasks, deterministic recommended order,
and explicit dependency or scope blockers. `backlog-claim` derives
`work/<github-username>` from the provider identity. Start is rejected until dependencies are
done or while an active task has an overlapping scope. A successful start records a durable
lease containing the immutable holder, exact branch, scope, and acquisition time. Developer
activity and a bound PR must use that same lease branch.

The lease remains held through blocked work, review, CI, and merge. It is released only by the
Coordinator after provider read-back proves the PR is merged into `dev`, every required check
passed, and every GitHub-reported changed path is inside the task scope. The user-facing CLI
does not expose an unverified backlog-complete mutation.

## Accepted integration state

`STATE.yaml` changes only through an accepted integration checkpoint. The coordinator command
reads GitHub again; command-line claims about merge or CI are not trusted:

```text
aria collaboration state-sync --project demo --item BLG-000001 \
  --pull-request 17 --expected-backlog-revision 3 --expected-state-revision 0 \
  --github-client-id <client-id> --coordinator-integration-id <app-id>
```

The pull request must be closed and merged into the configured `dev` branch. Its full merge SHA
must remain reachable from the remote branch and descend from the previous accepted head. The
branch must have a non-empty strict required-status-check policy, and every exact required check
(including its App id when bound) must be `completed/success` on that merge SHA. An open PR, a
failed or missing check, an unprotected branch, or rewritten history causes no backlog/state write.
The PR body must contain exactly one `ARIA-Backlog: BLG-...` line, and it must match the item
being closed; the CLI argument cannot substitute for this provider-read binding.
The PR must come from the task lease branch in the same repository. ARIA reads the complete
GitHub PR file list and rejects any path outside the triaged file scope before changing either
backlog or accepted state.

After verification, ARIA uses the immutable PR author as `requested_by`, requires that actor to
be the active backlog assignee, closes the item with PR/commit/check evidence, and appends one
hash-chained state checkpoint. `STATE.yaml` and matching `HISTORY.jsonl` are installed with a
two-document recovery journal, then the complete control inventory is committed by the App.
The operation id is deterministic from PR number and merge SHA, so a repeated event or recovery
does not duplicate backlog, state, or history entries. `state-status` is a local read-only view.

All team, backlog, activity, and state publishing commands share one project-level operation
lock for their full read/authorize/mutate/remote-commit cycle. GitHub requests may arrive
concurrently, but the canonical writer remains sequential. `coordinator-run-once` performs one
bounded polling pass: it refreshes the control worktree, processes Issue requests, discovers
merged PRs in order, and calls the same fail-closed state publication boundary. Webhook wake-up
and live private-repository acceptance are still pending.

Every private Git fetch used by enable, team/backlog/activity/state publication, request queue,
and the combined worker uses the saved user device session through `aria-github-askpass` with
terminal prompting disabled. Tokens are not embedded in a URL, process argument, journal, or Git
configuration.

### Recurring Windows coordinator

After device login, App-key configuration, project creation, and registration, the coordinator
administrator installs a current-user task:

```text
aria coordinator install --project demo --github-client-id <client-id> \
  --coordinator-integration-id <app-id> --interval-minutes 1
aria coordinator status --project demo
aria coordinator trigger --project demo
```

The strict local JSON configuration contains only project/repository ids, paths, limits, public
client/App ids, and the SHA-256 of the installed `aria.exe`; it contains no access token, refresh
token, installation token, or private key. The Task Scheduler action carries only the config path
and its expected SHA-256. Each run re-verifies both hashes before loading the project and records
a bounded secret-free last-run receipt.

The task runs as the current user with `InteractiveToken`, `LeastPrivilege`, network required,
and `IgnoreNew`. A separate OS worker lock also makes an overlapping invocation a safe no-op.
This lets the process access the same user's Windows Credential Manager without a password, but
the coordinator runs only while that Windows user is signed in. Removal preserves Git checkouts,
control documents, project registry, receipts, and credentials:

```text
aria coordinator remove --project demo
```

## Joining an existing project

After accepting the normal GitHub repository invitation and completing device login, a
developer runs:

```text
aria project join --project demo \
  --repository-url https://github.com/acme/product.git \
  --code-root C:\Projects\product --code-branch work/yura \
  --github-client-id <client-id> --coordinator-integration-id <app-id>
```

ARIA invokes Git with `GIT_ASKPASS_REQUIRE=force` and terminal prompts disabled. The AskPass
helper obtains the user access token from Windows Credential Manager and writes it only to the
requesting Git child process. The token is never placed in the repository URL, CLI arguments,
join journal, Git config, or ARIA JSON output.

Join clones without checkout, fetches exact `dev` and `aria-control` refs, creates or tracks the
requested `work/<username>` branch from `origin/dev`, adds the adjacent control worktree, and
then validates `CONTROL.yaml`, immutable repository id, active membership, coordinator-only
protection, and presence in `ARIA_TEAM.yaml`. Registration occurs only after those checks and
is followed by collaborative doctor. A phased local journal resumes a completed clone,
branches, or worktree after interruption; an unrecognized partial destination fails closed.

Schema version 1 has exact keys and exact authority values. Unknown keys are rejected
so a newer document cannot be interpreted under an older, weaker policy.

## Planning boundary

`aria collaboration plan` reads only local Git metadata. It does not fetch, create a
branch, add a worktree, write files, register a project, push, or change provider
settings. Until a provider adapter confirms branch protection, the plan contains the
blocking codes `PROVIDER_ADAPTER_UNAVAILABLE` and `UNPROTECTED` and is not eligible
for application. The plan reports whether the configured remote exists, but never
prints its URL because Git URLs can contain credentials.

Example:

```text
aria collaboration plan --project demo --code-root C:\Projects\demo \
  --provider github --repository-id 123456789
```

The applying boundary consumes the exact `plan_sha256`, requires `--confirm`, and
re-reads all preconditions. For an active repository admin and a mutation-capable adapter,
the confirmed plan creates an active ruleset targeting only `refs/heads/aria-control`.
The configured coordinator Integration is its only bypass actor; creation, update, deletion,
and non-fast-forward changes are restricted. Existing overlapping rulesets are never silently
replaced and instead produce a hard conflict. Protection is read back before the command
creates the remote branch, worktree, collaborative documents, and registry entry through a
recoverable transaction. Live GitHub acceptance is still pending. The command never treats
Git commit author name or email as authenticated project identity.

## Local activity coordinator boundary

The local activity coordinator foundation serializes `ACTIVITY.yaml` updates with an OS
lock, expected revision, durable request receipts, atomic read-back, and a prepared recovery
journal. It is an internal persistence API and is not exposed as a public collaborative CLI
or network service. It does not fetch provider membership, authorize backlog actions, create
Git commits, push `aria-control`, or receive GitHub webhooks.

## Migration rules

ARIA 1.5.5 project documents continue to use their existing schemas in offline mode.
Collaborative target schemas are `PROJECT` 1, `BACKLOG` 2, `ACTIVITY` 1, `STATE` 2,
`TEAM` 2, and `ACCESS` 2. Moving to those schemas is a separate explicit migration workflow.
Creating `CONTROL.yaml` alone is not a migration and must not activate collaborative writes.

The migration starts with a read-only plan. Every legacy creator and assignee used by the
offline backlog must be mapped explicitly to one active immutable GitHub user id. ARIA never
infers that mapping from a login, display name, email, or Git commit metadata.

```text
aria collaboration migrate-plan --project demo --repository-id 123456789 \
  --github-client-id <client-id> --coordinator-integration-id <integration-id> \
  --actor-map local-owner=100 --actor-map yura=200

aria collaboration migrate --project demo --repository-id 123456789 \
  --github-client-id <client-id> --coordinator-integration-id <integration-id> \
  --actor-map local-owner=100 --actor-map yura=200 \
  --expected-plan-sha256 <sha256> --confirm
```

The plan verifies ARIA/project version `1.5.5`, offline doctor, signed backlog audit,
`HISTORY.jsonl`, absence of open runs, clean Git state, GitHub membership, collaborator
read-back, and coordinator-only branch protection. Apply accepts only the exact plan SHA.
Before any remote commit it copies the complete offline docs tree into a machine-runtime
backup, rejects links/reparse points and oversized inputs, and verifies every copied file by
SHA-256.

Legacy item ids, source ids, dependencies, status, creator, assignee, timestamps, completion
evidence, and source fields are retained deterministically in `BACKLOG.yaml` v2. The new
collaborative `STATE.yaml` remains empty until a GitHub PR and required checks are accepted;
the legacy state, history, access policy, signatures, and all other documents remain intact in
the verified backup and unchanged original docs root. Migration never represents an offline
state assertion as provider-verified GitHub state.

A hash-bound local journal reuses the exact documents and backup after interruption. Rollback
requires the original migration commit at both remote and clean local `aria-control`, no
installed coordinator task, and intact source/backup hashes. It restores only the registered
authority; the protected control branch is preserved as an audit artifact. Direct writes by
the same unrestricted OS user remain outside ARIA's enforcement boundary.

```text
aria collaboration migration-status --project demo
aria collaboration migration-rollback --project demo \
  --coordinator-integration-id <integration-id> \
  --expected-control-commit <40-character-sha> --confirm
```
