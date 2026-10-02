# Changelog

All notable ARIA changes are recorded here. Versions follow semantic versioning.

## 1.5.5 portable test candidate — 2026-09-02

### Included

- Added a separate Claude Code distribution integration: packaged `aria-project` skill,
  `aria claude install/status/remove`, safe merge into user `settings.json`, and a fail-closed
  `PreToolUse` hook protecting Coordinator-owned control files and the `aria-control` branch.
- Claude integration contains no MCP server and does not store Anthropic or GitHub secrets.

- Complete P0 implementation developed on top of 1.5.4: one-repository collaboration with
  `main`, `dev`, `work/<github-username>`, and `aria-control`; local Coordinator; durable GitHub
  Issue queue and outbox; team, backlog, activity, PR, CI, merge, restart, and replay protection.
- Runtime, package metadata, installer identity, GitHub user agent, migration target, and project
  compatibility tables updated to 1.5.5. Existing 1.5.4 project metadata remains readable and can
  be upgraded through the normal 1.5 migration path.
- Autonomous Windows x64 packaging is produced separately under the distribution `releases/1.5.5`
  directory with bundled Python, MinGit, dependencies, framework, commands, instructions, and
  SHA-256 manifests.

### Verification

- Version-focused migration, registry, and collaborative-control regression: 15/15 passed.
- Full source regression under a clean 1.5.5 distribution identity: 454/454 passed in 419.514
  seconds, zero failures or skips.
- Live multi-account GitHub acceptance is intentionally not claimed by this portable test build.

## 1.5.4 P0 working copy — 2026-09-02

### Added

- Safe existing-repository connect plan/apply flow with exact GitHub admin read-back,
  preservation of the existing default branch and code, and recoverable creation of only
  missing `main`, `dev`, `aria-control` state.
- One-repository branch contract `main`, `dev`, `work/<github-username>`, `aria-control`;
  project join derives the working branch from authenticated GitHub identity.
- GitHub collaborator invitation/revocation with immutable identity read-back and durable
  `invited`, `active`, `revoked` team lifecycle.
- Authored free ideas, owner-only triage, dependency-aware task recommendations, automatic
  `work/<github-username>` claim, and durable file-scope leases.
- Coordinator-only task completion after same-repository PR source-branch read-back, strict
  required checks, merge reachability, and complete changed-path enforcement against scope.
- Immutable GitHub Issue queue audit through GraphQL `lastEditedAt`, per-request membership
  refresh, durable poison-request rejection, and a common offline outbox for backlog/activity.
- Pre-merge `ARIA integration` check on the exact signed PR head, including immutable assignee,
  source branch, rename source paths, segment-safe globs, file scope, and Coordinator App pinning.
- Canonical `in_review` backlog transition and recoverable multi-document closure across backlog,
  activity, accepted state, history, remote control commit, and coordinator restart.
- Existing 1.5.4 access/backlog compatibility, explicit legacy-item triage, exact branch inventory
  in connect plans, and attachment of an already-valid `aria-control` worktree.
- Persist-before-mutation control journals, exact crash recovery after activity completion, and
  phase-aware invite/revoke reconciliation without repeating external GitHub mutations.
- Immutable `base...head` PR comparison with final head read-back, identical verified author and
  committer identity, automatic App-pinned `dev` protection, and protected policy read-back.
- Owner-confirmed legacy control upgrade plus task `recover`, `amend_scope`, and `cancel` actions;
  explicit `pending_sync`, terminal request rejection, and claim-time project preflight.
- Fail-closed handling of saturated GitHub compare responses, preservation of existing `dev`
  checks during App pinning, and enforced P0-before-P1/P2/P3 claim ordering per assignee.
- Target-bound legacy policy recovery, safe restart of journal-owned partial clones, validated and
  hashed team operation journals, and invitation-permission read-back.
- Outbox delivery receipts remain pending until the Issue reaches a terminal coordinator result;
  duplicate lookup includes closed Issues, and transient coordinator failures remain retryable.
- Coordinator processing includes user-closed but authority-unapplied Issues; local outbox entries
  clear only after control-document proof, reopened terminal requests fail closed, and durable local
  rejection receipts prevent poison Issues from starving later queue entries.
- Connect/join clone recovery uses nonce-named, ownership-marked staging directories and never
  removes an unowned destination; claim preflight requires the App-pinned repository contract.
- Immediate-predecessor connect/join journals remain recoverable without weakening nonce-owned
  staging; schema-1 transactions created before the staging extension are migrated in place.
- Queue acceptance and rejection use immutable Coordinator App receipts bound to request id,
  Issue body SHA-256 and control commit/outcome; local control edits alone cannot clear outbox.
- Terminal receipts are consulted before any reapplication; acceptance commits must be ancestors
  of the verified control head and contain the exact request proof at that historical commit.
- Owner create/connect provisions and reads back `aria:request`, `aria:backlog`, and
  `aria:activity` labels; new Issues require exact labels while predecessor Issues remain readable.
- Receipt discovery ignores prefix-spoofing comments from users or other Apps and validates
  immutable receipt shape only for the configured Coordinator App.
- Terminal outbox entries move atomically to a durable receipt store so later events continue;
  queue pagination has no 1000-Issue lifetime cutoff and processed history does not spend the
  current run's action limit.
- Queue acceptance proof is bound to the complete canonical request, expected revision,
  submitted timestamp and immutable GitHub user id; reuse of an existing request id with a
  different operation, payload or actor is rejected instead of being accepted by id alone.
- Activity receipts persist a full request fingerprint through active and archived ledgers;
  predecessor receipts remain readable and can finish only an existing App-authored acceptance
  whose historical activity entry matches content, timestamp, revision and immutable actor.
- Non-applied activity outcomes, including late events, receive a durable no-apply receipt unless
  the exact request already has complete control proof; the Issue compatibility API keeps labels
  optional for predecessor callers.
- If the Coordinator stops after its App acceptance comment but before Issue close, restart reads
  the comment as a non-terminal intent, verifies historical proof, and completes the close without
  invoking the request handler again; developer outboxes still wait for `closed/completed`.

### Verification

- Repository/team/join focused regression: 84/84 tests, zero failures.
- Task/integration focused regression: 85/85 tests; coordinator/recovery: 42/42 tests.
- Full source regression after request-proof and acceptance-intent crash hardening: 454/454 tests
  in 383.614 seconds,
  zero failures or skips.
- Isolated source framework doctor: `ok=true`, version `1.5.4`, 94 engine files,
  SHA-256 `66443107d222387c073e97fb92269f9f8852fd72422f9dd4d4513e5c4e1a9583`.
- No live GitHub mutation was performed; isolated private-repository E2E remains required.

## 1.5.4 — 2026-08-24

### Added

- Collaborative GitHub control plane with a protected `aria-control` branch, immutable provider
  identities, team projection, authored backlog, activity snapshots, merge/CI state publication,
  recoverable coordinator writes, project create/join, and offline-project migration.
- GitHub device login with Windows Credential Manager, separate Coordinator App credentials,
  Issues request queue, recurring Windows coordinator task, and Codex natural-language skill.
- Offline Windows schema 3 installer candidate with an official hash-bound side-by-side Python
  3.12.10 runtime, ownership-safe reinstall/uninstall, and no secret-bearing installer state.

### Verification

- Full source regression: 392/392 tests.
- Continuous release check for source commit `f2a27e3`: 91/91 checks, zero failures/skips.
- Two wheel builds are byte-identical at SHA-256
  `f87f6d64e5b9d69bb1635ad4e6d1a9a4aca5cc092bf8e7e6a9989885e4162ba8`.
- Windows PowerShell 5.1 schema 3 validation, clean install, doctor/CLI/skill read-back,
  idempotent reinstall, and uninstall passed with project runtime preserved.

### Release boundary

- The tracked schema 3 bundle is a locally accepted pre-provider candidate. Release GitHub App
  identifiers, exact final manifest read-back, live private GitHub testing with four identities,
  and publication remain pending.

### Fixed

- GitHub ruleset read/create denials now produce a distinct fail-closed
  `PROVIDER_CAPABILITY_UNAVAILABLE` blocker with an actionable plan/permission explanation;
  ARIA never treats a private repository without enforced coordinator-only protection as ready.
- Added an executable project governance contract with one declared status authority,
  mandatory run-to-backlog binding for governed builds and a fail-closed write preflight.
- Project diagnostics now classify dirty product changes without an active build run as
  `RECOVERY_REQUIRED` instead of allowing a later run to legitimize an unknown baseline.
- Accepted `DEC-*` rows in an explicitly configured Markdown decision registry produce a
  deterministic read-only reconciliation plan; applying selected actions records signed
  backlog evidence and keeps optimistic revision checks.
- Bound completed/blocked runs reconcile their existing backlog item instead of creating a
  duplicate run-derived task. Retrying a closed managed lifecycle retries backlog recovery.
- Existing 1.5 projects can add the governance contract through the recoverable
  `upgrade-1-5` path; arbitrary same-user filesystem writes remain outside ARIA's trust
  boundary and are not misrepresented as technically intercepted.

## 1.5.3 — 2026-08-04

### Fixed

- Repository review no longer excludes product source and test packages merely because a
  path segment is named `coverage`. Review inventory now relies on Git's tracked and
  non-ignored boundary, so generated coverage output remains excludable through `.gitignore`.
- Framework integrity hashing no longer drops an operational ARIA package named `coverage`.
- Installed-wheel provenance accepts standard LF/CRLF checkout differences while still
  requiring the exact same Python file inventory and normalized source content.
- Regression coverage proves nested Java/Python product packages are captured, Git-ignored
  coverage reports stay outside the inventory, and operational framework packages affect the
  engine hash.

### Verification

- Focused regression: 7 passed.
- Full source regression after the version cut: 186 passed, zero failures.
- End-to-end release acceptance: 85/85 checks, zero failures or skips.

## 1.5.2 — 2026-07-30

### Added

- Windows `setup.ps1` performs a fail-closed, offline installation from the immutable
  1.5.2 release wheelhouse, creates a separate local venv and runtime, verifies hashes,
  dependencies and import provenance, and finishes with framework doctor.
- `releases/1.5.2` now contains the accepted ARIA wheel, all Windows CPython 3.12
  dependency wheels and a SHA-256 manifest, so installation does not depend on `.aria-work`.

### Fixed

- New-project instructions now use the executable order `init → identity enroll →
  access bootstrap → doctor → feature` and explicitly define separate framework, code,
  docs and runtime roots; `workspaces` has no special meaning.
- Init is described and reported as a deterministic Git inventory bootstrap that requires
  a subsequent Codex semantic code review.
- Backlog authorization derives the actual registered Git branch at the Python API boundary,
  rejects spoofed branch context and restores the exact preimage after both read errors and
  semantic read-back mismatches.
- Backlog claim fails closed until every declared dependency is `done`; manual completion
  accepts only a verified completed local project run or the full current Git HEAD from a
  clean registered checkout. Automatic run reconciliation reads `result.json` and verifies
  its SHA instead of trusting `manifest.status` alone.
- ARIA 1.4 → 1.5 migration uses a recoverable preimage journal for every partial-write window.
- Release acceptance requires a clean committed candidate, preserves hashed raw logs, runs
  real focused/integration/E2E/adversarial behavior, a two-device claim collision,
  version/branch rejection, evidence completion, performance regression and active cutover.
- GitHub Actions adapters pin the current ARIA package version instead of a stale literal.

### Verification

- The final acceptance result must be regenerated from the clean 1.5.2 candidate; evidence
  produced for 1.5.1 is not reused.

## 1.5.1 — 2026-07-30

### Fixed

- Backlog commands now authorize against the verified branch of the registered Git
  checkout instead of passing `branch=None`.
- `show`, `assign`, `claim`, `block`, `done`, `sync` and `audit` accept branch context;
  a requested branch that differs from the checkout fails closed.
- Automatic backlog synchronization during run start, verification and closure preserves
  the same version and Git-branch access scope.
- Clean-wheel acceptance now exercises a real `feature/*` contributor claim instead of
  hiding the defect behind a wildcard branch grant.

## 1.5.0 — 2026-07-29

### Added

- Per-device Ed25519 identities with Windows DPAPI protection and public enrollment requests.
- Signed `ACCESS.yaml` policies with project permissions and version/branch scopes.
- Signed `BACKLOG.yaml` with ownership, optimistic concurrency, provenance and evidence closure.
- Automatic backlog discovery and reconciliation for runs, review findings, failures and blockers.
- Team schema v2 and recoverable, idempotent 1.4 → 1.5 migration.
- Public identity, access and backlog CLI command families.

### Security

- Protected CLI actions fail closed for unknown, revoked, out-of-scope or tampered identities.
- Access mutations roll back policy state when audit persistence fails.
- Engine hashing prunes virtual environments, dependency trees, caches and VCS data.

## 1.4.0 — 2026-07-28

- Added portable Evidence Package v2 with Ed25519 signatures and offline verification.
- Added trust policies with trust levels, key allowlists, actor roles, assurance classes,
  approvals, expiry and revocation.
- Added isolated `ci prepare`, `ci execute`, separate `ci attest` and `ci import` protocol bound to job nonce,
  source commit and immutable contracts.
- Added actor roles, atomic task leases and optimistic project revision.
- Added integration gate requiring fresh CI evidence for the exact source package set and
  a signed independent review attestation and Git ancestry.
- Added a GitHub Actions reference adapter and ARIA 1.3 Evidence Bundle local-only reader.

## 1.3.0 — 2026-07-28

### Added

- Structured `VERIFY.yaml` contracts and safe Python, Node, Rust and Go adapters.
- `aria verify` with timeouts, output bounds, secret redaction and deterministic resume.
- SHA-bound execution receipts and an Evidence Bundle tied to exact product Git state.
- Requirement and acceptance links from convergence proofs to trusted executions.

### Changed

- New configured projects reject manually asserted verification evidence at closure.
- `aria init` infers verification commands only from tracked manifests with real test configuration.
- Release acceptance executes the trusted verification path from the installed wheel.

### Compatibility

- Projects without a verification document preserve legacy evidence behavior. Open runs
  remain engine-bound and must be restarted after a framework upgrade.

### Verification

- 114 tests passed.
- 33/33 release-acceptance checks passed with zero failures or skipped checks.
- Two independent final reviews returned CLEAN for release-blocking P1/P2 findings.

## 1.2.0 — 2026-07-22

### Added

- Managed feature lifecycle: `specify → clarify → plan → tasks → implement → converge`.
- Feature Contract locking, verified amendments, recovery journal and SHA chain.
- Exact requirement coverage between requirements, acceptance scenarios, plan and tasks.
- GitHub Spec Kit import/export for `spec.md`, `plan.md` and `tasks.md`.
- Semantic `aria init` for existing Git repositories.
- Reproducible `aria release-check` with clean-wheel, installed-CLI and disposable-project checks.

### Changed

- Core lock and close operations now enforce managed lifecycle phases.
- Release checks verify installed provenance, route policies and isolated scratch boundaries.

### Compatibility

- Unfinished ARIA 1.0 and 1.1 run manifests remain readable without shape drift.

### Verification

- 100 tests passed.
- 28/28 release-acceptance checks passed with zero failures or skipped checks.
- Two independent final reviews returned CLEAN.
