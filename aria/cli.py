from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aria.access import (
    access_status,
    authorize_access,
    bootstrap_access,
    grant_access,
    revoke_access,
    verify_access_audit,
)
from aria.assurance import load_system_map
from aria.backlog import (
    add_backlog_item,
    assign_backlog_item,
    backlog_audit,
    block_backlog_item,
    claim_backlog_item,
    complete_backlog_item,
    list_backlog_items,
    reconcile_accepted_decisions,
    show_backlog_item,
    sync_backlog,
)
from aria.ci import (
    attest_ci_result,
    execute_ci_job,
    import_ci_result,
    prepare_ci_job,
    write_github_workflow,
)
from aria.collaboration import (
    collaboration_plan,
    enable_collaboration,
    load_control_contract,
)
from aria.collaborative_backlog_runtime import (
    authenticated_backlog_status,
    collaborative_backlog_status,
    submit_authenticated_backlog_action,
)
from aria.collaborative_migration import (
    collaborative_migration_plan,
    collaborative_migration_status,
    migrate_offline_project,
    parse_actor_mappings,
    rollback_collaborative_migration,
)
from aria.collaborative_activity_runtime import (
    authenticated_activity_status,
    collaborative_activity_status,
    submit_authenticated_activity,
)
from aria.activity_outbox import activity_outbox_status, flush_activity_outbox
from aria.collaborative_request_queue import (
    enqueue_activity,
    enqueue_backlog_action,
    process_github_request_queue,
)
from aria.collaborative_team_runtime import (
    collaborative_team_status,
    invite_authenticated_member,
    revoke_authenticated_member,
    sync_authenticated_team,
)
from aria.collaborative_worker import run_collaborative_coordinator_once
from aria.coordinator_scheduler import (
    coordinator_schedule_status,
    install_coordinator_schedule,
    remove_coordinator_schedule,
    resolve_aria_executable,
    run_configured_coordinator,
    trigger_coordinator_schedule,
)
from aria.codex_integration import (
    codex_integration_status,
    install_codex_integration,
    remove_codex_integration,
)
from aria.claude_integration import (
    claude_integration_status,
    install_claude_integration,
    remove_claude_integration,
)
from aria.collaborative_state_runtime import (
    collaborative_state_status,
    sync_accepted_pull_request,
)
from aria.errors import AriaError, WorkflowError
from aria.evidence_package import (
    create_review_attestation,
    export_run_package,
    inspect_evidence,
    verify_evidence,
)
from aria.execution import verify_project_run
from aria.framework_doctor import run_framework_doctor
from aria.governance import (
    decision_reconciliation_plan,
    governance_diagnostics,
    governance_preflight,
)
from aria.github_login import default_github_login_service
from aria.github_app import (
    configure_github_app_key,
    github_app_key_status,
    remove_github_app_key,
)
from aria.github_runtime import (
    build_authenticated_github_adapter,
    build_authenticated_github_request_queue,
    build_github_app_request_queue,
    build_github_app_integration_verifier,
    build_github_control_writer,
    build_github_collaborator_manager,
    build_github_git_environment,
)
from aria.github_session import WindowsCredentialBackend
from aria.integration_gate import run_integration_gate
from aria.lifecycle import (
    amend_feature_contract,
    begin_implementation,
    converge_feature,
    export_spec_kit,
    import_spec_kit,
    lifecycle_status,
    start_feature,
    submit_lifecycle_phase,
)
from aria.identity import enroll_identity, identity_status
from aria.migration_1_4 import upgrade_project_to_1_4
from aria.migration_1_5 import upgrade_project_to_1_5
from aria.project import (
    default_runtime_root,
    git_snapshot,
    load_project,
    run_project_doctor,
    verify_history,
)
from aria.project_activation import activate_project, run_project_canary
from aria.project_create import (
    create_collaborative_project,
    create_collaborative_project_plan,
)
from aria.project_connect import (
    connect_existing_project,
    connect_existing_project_plan,
)
from aria.project_init import initialize_project
from aria.project_join import join_collaborative_project
from aria.registry import list_registered_projects, register_project
from aria.release_check import run_release_check
from aria.signing import generate_keypair
from aria.simple_run import (
    approve_project_spec,
    close_project_run,
    lock_project_feature_contract,
    project_role_target,
    project_status,
    start_project_run,
)
from aria.team import claim_task, release_task, team_status


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _authorize_project_command(project: object, args: argparse.Namespace) -> None:
    if getattr(project, "framework_version", None) not in {
        "1.5.0",
        "1.5.1",
        "1.5.2",
        "1.5.3",
        "1.5.4",
        "1.5.5",
    }:
        return
    command = args.command
    if command in {"doctor", "preflight", "upgrade-1-4", "upgrade-1-5"}:
        return
    if command == "access":
        if args.access_action in {"bootstrap", "status", "audit", "grant", "revoke"}:
            return
    if command in {"backlog", "governance"}:
        return
    read_commands = {"lifecycle", "status", "history", "map-status", "preflight"}
    create_commands = {"run", "feature", "spec", "next-task-new", "next-task_new"}
    verify_commands = {"verify"}
    team_commands = {"team"}
    release_commands = {"gate", "_project-canary", "_project-activate"}
    permission = (
        "project.read"
        if command in read_commands
        else "run.create"
        if command in create_commands
        else "verify.execute"
        if command in verify_commands
        else "team.claim"
        if command in team_commands
        else "release.manage"
        if command in release_commands
        else "run.advance"
    )
    snapshot = git_snapshot(project.code_root, project.git_ignore_prefixes)
    authorize_access(
        project,
        permission=permission,
        actor_id=args.identity_actor,
        device_id=args.identity_device,
        version=project.framework_version,
        branch=snapshot.get("branch"),
    )


def _verified_backlog_branch(
    project: object, requested_branch: str | None
) -> str | None:
    snapshot = git_snapshot(project.code_root, project.git_ignore_prefixes)
    actual_branch = snapshot.get("branch")
    branch = str(actual_branch) if isinstance(actual_branch, str) else None
    if requested_branch is not None and requested_branch != branch:
        raise WorkflowError(
            "Backlog branch context does not match the registered Git checkout: "
            f"requested={requested_branch!r}, actual={branch!r}"
        )
    return branch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aria",
        description="LLM-first project engineering and assurance for Codex",
    )
    parser.add_argument(
        "--framework-root",
        "--project-root",
        dest="project_root",
        type=Path,
        help=(
            "Operational ARIA framework root; required for a wheel-installed CLI "
            "that runs outside that root"
        ),
    )
    parser.add_argument(
        "--identity-actor",
        help="Select the local ARIA actor identity for protected project commands",
    )
    parser.add_argument(
        "--identity-device",
        help="Select the local ARIA device identity for protected project commands",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    doctor = commands.add_parser(
        "doctor", help="Validate the framework or one registered project"
    )
    doctor.add_argument("--project", help="Registered project id")

    commands.add_parser("projects", help="List locally registered projects")

    upgrade_1_4 = commands.add_parser(
        "upgrade-1-4", help="Migrate one registered ARIA 1.3 project to 1.4 metadata"
    )
    upgrade_1_4.add_argument("--project", required=True)

    upgrade_1_5 = commands.add_parser(
        "upgrade-1-5", help="Migrate one registered ARIA 1.4 project to 1.5 metadata"
    )
    upgrade_1_5.add_argument("--project", required=True)

    register = commands.add_parser("register", help="Register or update one project")
    register.add_argument("--project", required=True)
    register.add_argument("--docs-root", required=True, type=Path)
    register.add_argument("--code-root", required=True, type=Path)

    init = commands.add_parser(
        "init",
        help=(
            "Inventory a Git checkout and create/register deterministic ARIA "
            "bootstrap docs for later Codex review"
        ),
    )
    init.add_argument("--project", required=True)
    init.add_argument("--code-root", required=True, type=Path)
    init.add_argument("--docs-root", type=Path)
    init.add_argument("--display-name")

    project_command = commands.add_parser(
        "project", help="Create or join a collaborative Git project"
    )
    project_actions = project_command.add_subparsers(
        dest="project_action", required=True
    )
    for action, help_text in (
        ("create-plan", "Inspect a proposed new GitHub collaborative project"),
        ("create", "Create and register a new GitHub collaborative project"),
    ):
        create = project_actions.add_parser(action, help=help_text)
        create.add_argument("--project", required=True)
        create.add_argument("--owner", required=True)
        create.add_argument("--repository-name", required=True)
        create.add_argument("--code-root", required=True, type=Path)
        create.add_argument("--docs-root", type=Path)
        create.add_argument(
            "--public", action="store_false", dest="private", default=True
        )
        create.add_argument("--github-client-id", required=True)
        create.add_argument(
            "--coordinator-integration-id", required=True, type=int
        )
        if action == "create":
            create.add_argument("--expected-plan-sha256", required=True)
            create.add_argument("--confirm", action="store_true", required=True)
    for action, help_text in (
        ("connect-plan", "Inspect a safe connection to an existing GitHub repository"),
        ("connect", "Connect and register an existing GitHub repository"),
    ):
        connect = project_actions.add_parser(action, help=help_text)
        connect.add_argument("--project", required=True)
        connect.add_argument("--repository-url", required=True)
        connect.add_argument("--code-root", required=True, type=Path)
        connect.add_argument("--docs-root", type=Path)
        connect.add_argument("--base-branch")
        connect.add_argument("--github-client-id", required=True)
        connect.add_argument("--coordinator-integration-id", required=True, type=int)
        if action == "connect":
            connect.add_argument("--expected-plan-sha256", required=True)
            connect.add_argument("--confirm", action="store_true", required=True)
    project_join = project_actions.add_parser(
        "join", help="Clone and register an existing collaborative project"
    )
    project_join.add_argument("--project", required=True)
    project_join.add_argument("--repository-url", required=True)
    project_join.add_argument("--code-root", required=True, type=Path)
    project_join.add_argument("--docs-root", type=Path)
    project_join.add_argument(
        "--code-branch",
        help="Optional work/<github-username> override; defaults to the authenticated user",
    )
    project_join.add_argument("--github-client-id", required=True)
    project_join.add_argument(
        "--coordinator-integration-id", required=True, type=int
    )

    codex = commands.add_parser(
        "codex", help="Install and inspect the natural-language ARIA project skill"
    )
    codex_actions = codex.add_subparsers(dest="codex_action", required=True)
    codex_install = codex_actions.add_parser(
        "install", help="Install the bundled ARIA project skill for the current user"
    )
    codex_install.add_argument("--github-client-id")
    codex_install.add_argument("--coordinator-integration-id", type=int)
    codex_install.add_argument("--replace", action="store_true")
    codex_actions.add_parser("status", help="Verify the installed skill and provider profile")
    codex_remove = codex_actions.add_parser(
        "remove", help="Remove the installed ARIA project skill"
    )
    codex_remove.add_argument("--force", action="store_true")

    claude = commands.add_parser(
        "claude", help="Install and inspect the ARIA integration for Claude Code"
    )
    claude_actions = claude.add_subparsers(dest="claude_action", required=True)
    claude_install = claude_actions.add_parser(
        "install", help="Install the bundled ARIA skill and protective hook for Claude Code"
    )
    claude_install.add_argument("--github-client-id")
    claude_install.add_argument("--coordinator-integration-id", type=int)
    claude_install.add_argument("--replace", action="store_true")
    claude_actions.add_parser(
        "status", help="Verify the installed Claude skill, hook, and provider profile"
    )
    claude_remove = claude_actions.add_parser(
        "remove", help="Remove the ARIA skill and hook from Claude Code"
    )
    claude_remove.add_argument("--force", action="store_true")

    coordinator = commands.add_parser(
        "coordinator", help="Install and operate the collaborative background coordinator"
    )
    coordinator_actions = coordinator.add_subparsers(
        dest="coordinator_action", required=True
    )
    coordinator_install = coordinator_actions.add_parser(
        "install", help="Install the current-user Windows polling task"
    )
    coordinator_install.add_argument("--project", required=True)
    coordinator_install.add_argument("--github-client-id", required=True)
    coordinator_install.add_argument(
        "--coordinator-integration-id", required=True, type=int
    )
    coordinator_install.add_argument("--runtime-root", type=Path)
    coordinator_install.add_argument("--aria-executable", type=Path)
    coordinator_install.add_argument(
        "--interval-minutes", type=int, default=1
    )
    coordinator_install.add_argument("--max-requests", type=int, default=20)
    coordinator_install.add_argument("--max-pull-requests", type=int, default=20)
    for action, help_text in (
        ("status", "Verify task, configuration, executable, and last run"),
        ("remove", "Remove the task without deleting project data"),
        ("trigger", "Ask Windows Task Scheduler to run the coordinator now"),
    ):
        action_parser = coordinator_actions.add_parser(action, help=help_text)
        action_parser.add_argument("--project", required=True)
        if action in {"status", "remove"}:
            action_parser.add_argument("--runtime-root", type=Path)
    coordinator_run = coordinator_actions.add_parser(
        "run", help="Execute one hash-bound scheduled coordinator pass"
    )
    coordinator_run.add_argument("--config", required=True, type=Path)
    coordinator_run.add_argument("--expected-config-sha256", required=True)

    collaboration = commands.add_parser(
        "collaboration",
        help="Plan and enable the protected collaborative control plane",
    )
    collaboration_actions = collaboration.add_subparsers(
        dest="collaboration_action", required=True
    )
    collaboration_plan_parser = collaboration_actions.add_parser(
        "plan",
        help="Build a read-only plan without changing local or remote Git state",
    )
    collaboration_plan_parser.add_argument("--project", required=True)
    collaboration_plan_parser.add_argument("--code-root", required=True, type=Path)
    collaboration_plan_parser.add_argument("--docs-root", type=Path)
    collaboration_plan_parser.add_argument("--provider", required=True)
    collaboration_plan_parser.add_argument("--repository-id", required=True)
    collaboration_plan_parser.add_argument("--remote", default="origin")
    collaboration_plan_parser.add_argument("--integration-branch", default="dev")
    collaboration_plan_parser.add_argument("--control-branch", default="aria-control")
    collaboration_plan_parser.add_argument("--github-client-id")
    collaboration_plan_parser.add_argument("--coordinator-integration-id", type=int)
    collaboration_enable_parser = collaboration_actions.add_parser(
        "enable",
        help="Apply one unchanged plan after explicit confirmation",
    )
    collaboration_enable_parser.add_argument("--project", required=True)
    collaboration_enable_parser.add_argument("--code-root", required=True, type=Path)
    collaboration_enable_parser.add_argument("--docs-root", type=Path)
    collaboration_enable_parser.add_argument("--provider", required=True)
    collaboration_enable_parser.add_argument("--repository-id", required=True)
    collaboration_enable_parser.add_argument("--remote", default="origin")
    collaboration_enable_parser.add_argument("--integration-branch", default="dev")
    collaboration_enable_parser.add_argument("--control-branch", default="aria-control")
    collaboration_enable_parser.add_argument("--github-client-id")
    collaboration_enable_parser.add_argument("--coordinator-integration-id", type=int)
    collaboration_enable_parser.add_argument(
        "--expected-plan-sha256", required=True
    )
    collaboration_enable_parser.add_argument(
        "--confirm", action="store_true", required=True
    )
    for action, help_text in (
        (
            "migrate-plan",
            "Build a read-only offline-to-collaborative migration plan",
        ),
        (
            "migrate",
            "Migrate one registered offline ARIA 1.5.5 project after confirmation",
        ),
    ):
        migration_parser = collaboration_actions.add_parser(action, help=help_text)
        migration_parser.add_argument("--project", required=True)
        migration_parser.add_argument("--provider", default="github")
        migration_parser.add_argument("--repository-id", required=True)
        migration_parser.add_argument("--docs-root", type=Path)
        migration_parser.add_argument("--remote", default="origin")
        migration_parser.add_argument("--integration-branch", default="dev")
        migration_parser.add_argument("--control-branch", default="aria-control")
        migration_parser.add_argument("--github-client-id", required=True)
        migration_parser.add_argument(
            "--coordinator-integration-id", required=True, type=int
        )
        migration_parser.add_argument(
            "--actor-map",
            action="append",
            default=[],
            metavar="LEGACY_ACTOR=GITHUB_USER_ID",
        )
        migration_parser.add_argument("--runtime-root", type=Path)
        if action == "migrate":
            migration_parser.add_argument("--expected-plan-sha256", required=True)
            migration_parser.add_argument(
                "--confirm", action="store_true", required=True
            )
    migration_status_parser = collaboration_actions.add_parser(
        "migration-status", help="Read local migration journal and receipt state"
    )
    migration_status_parser.add_argument("--project", required=True)
    migration_status_parser.add_argument("--runtime-root", type=Path)
    migration_rollback_parser = collaboration_actions.add_parser(
        "migration-rollback",
        help="Restore the verified offline registration before coordinator use",
    )
    migration_rollback_parser.add_argument("--project", required=True)
    migration_rollback_parser.add_argument(
        "--coordinator-integration-id", required=True, type=int
    )
    migration_rollback_parser.add_argument(
        "--expected-control-commit", required=True
    )
    migration_rollback_parser.add_argument("--runtime-root", type=Path)
    migration_rollback_parser.add_argument(
        "--confirm", action="store_true", required=True
    )
    collaboration_team_status = collaboration_actions.add_parser(
        "team-status",
        help="Read the local collaborative team projection",
    )
    collaboration_team_status.add_argument("--project", required=True)
    collaboration_team_sync = collaboration_actions.add_parser(
        "team-sync",
        help="Refresh ARIA_TEAM.yaml from the authenticated GitHub repository",
    )
    collaboration_team_sync.add_argument("--project", required=True)
    collaboration_team_sync.add_argument("--github-client-id", required=True)
    collaboration_team_sync.add_argument(
        "--coordinator-integration-id", required=True, type=int
    )
    collaboration_team_sync.add_argument(
        "--expected-revision", required=True, type=int
    )
    collaboration_team_sync.add_argument("--sync-id")
    for action in ("invite", "revoke"):
        team_mutation = collaboration_actions.add_parser(
            f"team-{action}", help=f"{action.title()} one GitHub project member"
        )
        team_mutation.add_argument("--project", required=True)
        team_mutation.add_argument("--github-client-id", required=True)
        team_mutation.add_argument("--coordinator-integration-id", required=True, type=int)
        team_mutation.add_argument("--expected-revision", required=True, type=int)
        team_mutation.add_argument("--username", required=True)
        team_mutation.add_argument("--request-id", required=True)
        if action == "invite":
            team_mutation.add_argument(
                "--permission",
                choices=("pull", "triage", "push", "maintain", "admin"),
                default="push",
            )

    collaboration_state_status = collaboration_actions.add_parser(
        "state-status", help="Read the accepted collaborative integration state"
    )
    collaboration_state_status.add_argument("--project", required=True)
    collaboration_state_sync = collaboration_actions.add_parser(
        "state-sync", help="Accept one merged pull request after required GitHub checks"
    )
    collaboration_state_sync.add_argument("--project", required=True)
    collaboration_state_sync.add_argument("--github-client-id", required=True)
    collaboration_state_sync.add_argument(
        "--coordinator-integration-id", required=True, type=int
    )
    collaboration_state_sync.add_argument("--item", required=True)
    collaboration_state_sync.add_argument("--pull-request", required=True, type=int)
    collaboration_state_sync.add_argument(
        "--expected-backlog-revision", required=True, type=int
    )
    collaboration_state_sync.add_argument(
        "--expected-state-revision", required=True, type=int
    )

    collaboration_backlog_list = collaboration_actions.add_parser(
        "backlog-list",
        help="List the shared collaborative backlog",
    )
    collaboration_backlog_list.add_argument("--project", required=True)
    collaboration_backlog_list.add_argument("--include-done", action="store_true")
    collaboration_backlog_mine = collaboration_actions.add_parser(
        "backlog-mine",
        help="List tasks assigned to the authenticated GitHub user",
    )
    collaboration_backlog_mine.add_argument("--project", required=True)
    collaboration_backlog_mine.add_argument("--github-client-id", required=True)
    collaboration_backlog_mine.add_argument(
        "--coordinator-integration-id", type=int
    )
    collaboration_backlog_mine.add_argument("--include-done", action="store_true")

    backlog_mutations: dict[str, argparse.ArgumentParser] = {}
    for action in (
        "add", "triage", "assign", "claim", "block", "cancel", "amend-scope", "recover"
    ):
        action_parser = collaboration_actions.add_parser(
            f"backlog-{action}",
            help=f"Submit an authenticated collaborative backlog {action} request",
        )
        action_parser.add_argument("--project", required=True)
        action_parser.add_argument("--github-client-id", required=True)
        action_parser.add_argument(
            "--coordinator-integration-id", required=True, type=int
        )
        action_parser.add_argument("--expected-revision", required=True, type=int)
        action_parser.add_argument("--request-id")
        action_parser.add_argument(
            "--delivery",
            choices=("github-queue", "coordinator-local"),
            default="github-queue",
        )
        backlog_mutations[action] = action_parser
    backlog_mutations["add"].add_argument("--title", required=True)
    backlog_mutations["add"].add_argument("--description", required=True)
    backlog_mutations["add"].add_argument(
        "--priority", choices=("P0", "P1", "P2", "P3"), default="P2"
    )
    backlog_mutations["add"].add_argument("--source-id")
    backlog_mutations["add"].add_argument(
        "--dependency", action="append", default=[]
    )
    backlog_mutations["add"].add_argument(
        "--evidence-required", action="store_true"
    )
    backlog_mutations["triage"].add_argument("--item", required=True)
    backlog_mutations["triage"].add_argument("--assignee-user-id", required=True)
    backlog_mutations["triage"].add_argument(
        "--priority", choices=("P0", "P1", "P2", "P3"), required=True
    )
    backlog_mutations["triage"].add_argument(
        "--requirement", action="append", required=True
    )
    backlog_mutations["triage"].add_argument(
        "--acceptance", action="append", required=True
    )
    backlog_mutations["triage"].add_argument("--dependency", action="append", default=[])
    backlog_mutations["triage"].add_argument(
        "--scope-path", action="append", required=True
    )
    backlog_mutations["triage"].add_argument(
        "--evidence-required", action="store_true"
    )
    backlog_mutations["assign"].add_argument("--item", required=True)
    backlog_mutations["assign"].add_argument(
        "--assignee-user-id", required=True
    )
    for action in ("claim", "block", "cancel", "amend-scope", "recover"):
        backlog_mutations[action].add_argument("--item", required=True)
    backlog_mutations["block"].add_argument("--reason", required=True)
    backlog_mutations["cancel"].add_argument("--reason", required=True)
    backlog_mutations["amend-scope"].add_argument(
        "--scope-path", action="append", required=True
    )
    backlog_mutations["recover"].add_argument(
        "--requirement", action="append", required=True
    )
    backlog_mutations["recover"].add_argument(
        "--acceptance", action="append", required=True
    )
    backlog_mutations["recover"].add_argument(
        "--scope-path", action="append", required=True
    )
    backlog_mutations["recover"].add_argument("--branch", required=True)
    backlog_mutations["recover"].add_argument(
        "--target-status", choices=("assigned", "in_progress", "blocked"), required=True
    )

    collaboration_activity_list = collaboration_actions.add_parser(
        "activity-list", help="List the shared live activity snapshot"
    )
    collaboration_activity_list.add_argument("--project", required=True)
    collaboration_activity_mine = collaboration_actions.add_parser(
        "activity-mine", help="List activity for the authenticated GitHub user"
    )
    collaboration_activity_mine.add_argument("--project", required=True)
    collaboration_activity_mine.add_argument("--github-client-id", required=True)
    collaboration_activity_mine.add_argument(
        "--coordinator-integration-id", type=int
    )
    collaboration_activity_set = collaboration_actions.add_parser(
        "activity-set", help="Publish the authenticated assignee's work stage"
    )
    collaboration_activity_set.add_argument("--project", required=True)
    collaboration_activity_set.add_argument("--github-client-id", required=True)
    collaboration_activity_set.add_argument(
        "--coordinator-integration-id", required=True, type=int
    )
    collaboration_activity_set.add_argument(
        "--expected-revision", required=True, type=int
    )
    collaboration_activity_set.add_argument("--task", required=True)
    collaboration_activity_set.add_argument(
        "--stage",
        required=True,
        choices=(
            "analysis",
            "planning",
            "implementation",
            "testing",
            "blocked",
            "ready_for_pr",
        ),
    )
    collaboration_activity_set.add_argument("--branch", required=True)
    collaboration_activity_set.add_argument("--note")
    collaboration_activity_set.add_argument("--event-id")
    collaboration_activity_set.add_argument(
        "--delivery",
        choices=("github-queue", "coordinator-local"),
        default="github-queue",
    )
    collaboration_activity_outbox_status = collaboration_actions.add_parser(
        "activity-outbox-status", help="Read the local offline activity outbox"
    )
    collaboration_activity_outbox_status.add_argument("--project", required=True)
    collaboration_activity_outbox_flush = collaboration_actions.add_parser(
        "activity-outbox-flush",
        help="Deliver locally queued activity through the authenticated GitHub session",
    )
    collaboration_activity_outbox_flush.add_argument("--project", required=True)
    collaboration_activity_outbox_flush.add_argument(
        "--github-client-id", required=True
    )
    collaboration_activity_outbox_flush.add_argument(
        "--coordinator-integration-id", type=int
    )
    collaboration_activity_outbox_flush.add_argument(
        "--max-requests", type=int, default=20
    )
    collaboration_queue_process = collaboration_actions.add_parser(
        "queue-process",
        help="Process authenticated GitHub requests on the coordinator host",
    )
    collaboration_queue_process.add_argument("--project", required=True)
    collaboration_queue_process.add_argument("--github-client-id", required=True)
    collaboration_queue_process.add_argument(
        "--coordinator-integration-id", required=True, type=int
    )
    collaboration_queue_process.add_argument(
        "--max-requests", type=int, default=20
    )
    collaboration_worker = collaboration_actions.add_parser(
        "coordinator-run-once",
        help="Refresh control state, process requests, and accept verified merged PRs",
    )
    collaboration_worker.add_argument("--project", required=True)
    collaboration_worker.add_argument("--github-client-id", required=True)
    collaboration_worker.add_argument(
        "--coordinator-integration-id", required=True, type=int
    )
    collaboration_worker.add_argument("--max-requests", type=int, default=20)
    collaboration_worker.add_argument("--max-pull-requests", type=int, default=20)

    github_auth = commands.add_parser(
        "github-auth",
        help="Manage the GitHub App device session without browser cookies",
    )
    github_auth_actions = github_auth.add_subparsers(
        dest="github_auth_action", required=True
    )
    for action in ("login-begin", "login-complete", "logout"):
        action_parser = github_auth_actions.add_parser(action)
        action_parser.add_argument("--client-id", required=True)
        action_parser.add_argument("--repository-id", required=True)
    github_auth_status = github_auth_actions.add_parser("status")
    github_auth_status.add_argument("--client-id", required=True)

    github_app = commands.add_parser(
        "github-app",
        help="Manage the local ARIA Coordinator GitHub App key",
    )
    github_app_actions = github_app.add_subparsers(
        dest="github_app_action", required=True
    )
    github_app_configure = github_app_actions.add_parser("configure")
    github_app_configure.add_argument("--app-id", required=True, type=int)
    github_app_configure.add_argument(
        "--private-key", required=True, type=Path, dest="private_key"
    )
    for action in ("status", "remove"):
        action_parser = github_app_actions.add_parser(action)
        action_parser.add_argument("--app-id", required=True, type=int)

    feature = commands.add_parser(
        "feature", help="Start one managed specify-to-converge feature lifecycle"
    )
    feature.add_argument("--project", required=True)
    feature.add_argument("--task", required=True)
    feature.add_argument("--mode", choices=["standard", "deep"], default="standard")
    feature.add_argument("--spec")
    feature.add_argument("--changed-path", action="append", default=[])
    feature.add_argument("--risk", action="append", default=[])
    feature.add_argument("--backlog-item")

    preflight = commands.add_parser(
        "preflight", help="Fail closed before a governed read, write or resume operation"
    )
    preflight.add_argument("--project", required=True)
    preflight.add_argument(
        "--operation", choices=["read", "write", "resume"], required=True
    )
    preflight.add_argument("--run", dest="run_id")

    governance = commands.add_parser(
        "governance", help="Inspect and reconcile project governance invariants"
    )
    governance_actions = governance.add_subparsers(
        dest="governance_action", required=True
    )
    governance_check = governance_actions.add_parser(
        "check", help="Read governance state without changing project files"
    )
    governance_check.add_argument("--project", required=True)
    governance_plan = governance_actions.add_parser(
        "plan", help="Build a read-only accepted-decision reconciliation plan"
    )
    governance_plan.add_argument("--project", required=True)
    governance_reconcile = governance_actions.add_parser(
        "reconcile", help="Apply an explicit accepted-decision reconciliation plan"
    )
    governance_reconcile.add_argument("--project", required=True)
    governance_reconcile.add_argument("--item", action="append", required=True)
    governance_reconcile.add_argument("--expected-revision", required=True, type=int)
    governance_reconcile.add_argument(
        "--confirm-acceptance", action="store_true", required=True
    )
    governance_reconcile.add_argument("--version")
    governance_reconcile.add_argument("--branch")

    lifecycle = commands.add_parser("lifecycle", help="Read one feature lifecycle status")
    lifecycle.add_argument("--project", required=True)
    lifecycle.add_argument("--run", required=True, dest="run_id")

    for phase in ("specify", "clarify", "plan", "tasks"):
        phase_parser = commands.add_parser(phase, help=f"Submit the {phase} lifecycle artifact")
        phase_parser.add_argument("--project", required=True)
        phase_parser.add_argument("--run", required=True, dest="run_id")
        phase_parser.add_argument("--input", required=True, type=Path, dest="input_path")
        if phase == "tasks":
            phase_parser.add_argument("--contract", required=True, type=Path)

    implement_feature = commands.add_parser(
        "implement", help="Lock the Feature Contract and enter implementation"
    )
    implement_feature.add_argument("--project", required=True)
    implement_feature.add_argument("--run", required=True, dest="run_id")

    verify = commands.add_parser(
        "verify", help="Execute immutable project commands and create an Evidence Bundle"
    )
    verify.add_argument("--project", required=True)
    verify.add_argument("--run", required=True, dest="run_id")
    verify.add_argument("--command", action="append", dest="command_ids")
    verify.add_argument("--links", type=Path, dest="links_path")
    verify.add_argument("--no-resume", action="store_false", dest="resume", default=True)

    converge = commands.add_parser(
        "converge", help="Validate convergence evidence and close a feature run"
    )
    converge.add_argument("--project", required=True)
    converge.add_argument("--run", required=True, dest="run_id")
    converge.add_argument("--result", required=True, type=Path)

    contract = commands.add_parser(
        "contract", help="Amend or exchange a run Feature Contract"
    )
    contract_actions = contract.add_subparsers(dest="contract_action", required=True)
    amend = contract_actions.add_parser("amend", help="Create a chained contract revision")
    amend.add_argument("--project", required=True)
    amend.add_argument("--run", required=True, dest="run_id")
    amend.add_argument("--contract", required=True, type=Path, dest="contract_path")
    amend.add_argument("--reason", required=True)
    import_kit = contract_actions.add_parser(
        "import-spec-kit", help="Import spec.md, plan.md and tasks.md"
    )
    import_kit.add_argument("--project", required=True)
    import_kit.add_argument("--run", required=True, dest="run_id")
    import_kit.add_argument("--spec-dir", required=True, type=Path)
    export_kit = contract_actions.add_parser(
        "export-spec-kit", help="Export a Feature Contract as Spec Kit Markdown"
    )
    export_kit.add_argument("--project", required=True)
    export_kit.add_argument("--run", required=True, dest="run_id")
    export_kit.add_argument("--output-dir", required=True, type=Path)

    release_check = commands.add_parser(
        "release-check", help="Run clean-wheel, installed-CLI, smoke and regression acceptance"
    )
    release_check.add_argument("--output", type=Path)

    key = commands.add_parser("key", help="Create an Ed25519 evidence signing identity")
    key_actions = key.add_subparsers(dest="key_action", required=True)
    key_generate = key_actions.add_parser("generate", help="Generate one Ed25519 key pair")
    key_generate.add_argument("--private-key", required=True, type=Path)
    key_generate.add_argument("--public-key", required=True, type=Path)

    identity = commands.add_parser(
        "identity", help="Manage the private identity bound to this device"
    )
    identity_actions = identity.add_subparsers(dest="identity_action", required=True)
    identity_enroll = identity_actions.add_parser(
        "enroll", help="Create a protected device key and public enrollment request"
    )
    identity_enroll.add_argument("--actor", required=True, dest="actor_id")
    identity_enroll.add_argument("--device", dest="device_id")
    identity_enroll.add_argument("--display-name")
    identity_enroll.add_argument("--email")
    identity_enroll.add_argument("--output", type=Path, dest="request_path")
    identity_whoami = identity_actions.add_parser(
        "whoami", help="List local public identity metadata"
    )
    identity_whoami.add_argument("--actor", dest="actor_id")

    access = commands.add_parser(
        "access", help="Bootstrap and administer signed project access"
    )
    access_actions = access.add_subparsers(dest="access_action", required=True)
    access_bootstrap = access_actions.add_parser(
        "bootstrap", help="Activate access with an existing project maintainer"
    )
    access_bootstrap.add_argument("--project", required=True)
    access_status_parser = access_actions.add_parser(
        "status", help="Read the active access policy"
    )
    access_status_parser.add_argument("--project", required=True)
    access_grant = access_actions.add_parser(
        "grant", help="Approve one signed device enrollment request"
    )
    access_grant.add_argument("--project", required=True)
    access_grant.add_argument("--request", required=True, type=Path)
    access_grant.add_argument("--permission", action="append", required=True)
    access_grant.add_argument("--version", action="append", default=[])
    access_grant.add_argument("--branch", action="append", default=[])
    access_grant.add_argument("--expected-revision", required=True, type=int)
    access_revoke = access_actions.add_parser(
        "revoke", help="Revoke one actor or device"
    )
    access_revoke.add_argument("--project", required=True)
    access_revoke.add_argument("--actor", required=True, dest="target_actor_id")
    access_revoke.add_argument("--device", dest="target_device_id")
    access_revoke.add_argument("--expected-revision", required=True, type=int)
    access_audit = access_actions.add_parser(
        "audit", help="Verify the signed access history"
    )
    access_audit.add_argument("--project", required=True)

    backlog = commands.add_parser(
        "backlog", help="Manage the signed, user-owned project backlog"
    )
    backlog_actions = backlog.add_subparsers(dest="backlog_action", required=True)
    backlog_add = backlog_actions.add_parser("add", help="Add one backlog item")
    backlog_add.add_argument("--project", required=True)
    backlog_add.add_argument("--title", required=True)
    backlog_add.add_argument("--type", required=True, dest="item_type")
    backlog_add.add_argument("--priority", default="normal")
    backlog_add.add_argument("--target-version", action="append", required=True)
    backlog_add.add_argument("--acceptance", required=True)
    backlog_add.add_argument("--source-kind", default="manual")
    backlog_add.add_argument("--source-ref")
    backlog_add.add_argument("--requirement", action="append", default=[])
    backlog_add.add_argument("--ref", action="append", default=[])
    backlog_add.add_argument("--dependency", action="append", default=[])
    backlog_add.add_argument("--assignee")
    backlog_add.add_argument("--branch")
    backlog_add.add_argument("--expected-revision", required=True, type=int)
    backlog_list = backlog_actions.add_parser("list", help="List backlog items")
    backlog_list.add_argument("--project", required=True)
    backlog_list.add_argument("--status")
    backlog_list.add_argument("--assignee")
    backlog_list.add_argument("--version")
    backlog_list.add_argument("--branch")
    backlog_show = backlog_actions.add_parser("show", help="Show one backlog item")
    backlog_show.add_argument("--project", required=True)
    backlog_show.add_argument("--item", required=True, dest="item_id")
    backlog_show.add_argument("--branch")
    backlog_assign = backlog_actions.add_parser("assign", help="Assign one backlog item")
    backlog_assign.add_argument("--project", required=True)
    backlog_assign.add_argument("--item", required=True, dest="item_id")
    backlog_assign.add_argument("--assignee", required=True)
    backlog_assign.add_argument("--branch")
    backlog_assign.add_argument("--expected-revision", required=True, type=int)
    backlog_claim = backlog_actions.add_parser("claim", help="Claim one backlog item")
    backlog_claim.add_argument("--project", required=True)
    backlog_claim.add_argument("--item", required=True, dest="item_id")
    backlog_claim.add_argument("--branch")
    backlog_claim.add_argument("--expected-revision", required=True, type=int)
    backlog_block = backlog_actions.add_parser("block", help="Block one backlog item")
    backlog_block.add_argument("--project", required=True)
    backlog_block.add_argument("--item", required=True, dest="item_id")
    backlog_block.add_argument("--reason", required=True)
    backlog_block.add_argument("--branch")
    backlog_block.add_argument("--expected-revision", required=True, type=int)
    backlog_done = backlog_actions.add_parser("done", help="Complete one backlog item")
    backlog_done.add_argument("--project", required=True)
    backlog_done.add_argument("--item", required=True, dest="item_id")
    backlog_done.add_argument("--evidence", action="append", required=True)
    backlog_done.add_argument("--branch")
    backlog_done.add_argument("--expected-revision", required=True, type=int)
    backlog_sync = backlog_actions.add_parser(
        "sync", help="Discover runs and findings and add missing backlog items"
    )
    backlog_sync.add_argument("--project", required=True)
    backlog_sync.add_argument("--version")
    backlog_sync.add_argument("--branch")
    backlog_sync.add_argument("--expected-revision", required=True, type=int)
    backlog_audit_parser = backlog_actions.add_parser(
        "audit", help="Verify the signed backlog event chain"
    )
    backlog_audit_parser.add_argument("--project", required=True)
    backlog_audit_parser.add_argument("--version")
    backlog_audit_parser.add_argument("--branch")

    evidence = commands.add_parser(
        "evidence", help="Export, verify or inspect a portable Evidence Package"
    )
    evidence_actions = evidence.add_subparsers(dest="evidence_action", required=True)
    evidence_export = evidence_actions.add_parser(
        "export", help="Export one run as a signed Evidence Package v2"
    )
    evidence_export.add_argument("--project", required=True)
    evidence_export.add_argument("--run", required=True, dest="run_id")
    evidence_export.add_argument("--output", required=True, type=Path)
    evidence_export.add_argument("--private-key", required=True, type=Path)
    evidence_export.add_argument("--actor")
    evidence_verify = evidence_actions.add_parser(
        "verify", help="Verify a package offline; optionally enforce a trust policy"
    )
    evidence_verify.add_argument("--package", required=True, type=Path)
    evidence_verify.add_argument("--trust-policy", type=Path)
    evidence_verify.add_argument("--policy", default="default")
    evidence_verify.add_argument(
        "--actor-role", action="append", default=[], dest="actor_roles"
    )
    evidence_verify.add_argument("--approval-count", type=int)
    evidence_inspect = evidence_actions.add_parser(
        "inspect", help="Read package metadata without asserting its signature"
    )
    evidence_inspect.add_argument("--package", required=True, type=Path)
    evidence_review = evidence_actions.add_parser(
        "review-attest",
        help="Sign an independent approval bound to an integration candidate",
    )
    evidence_review.add_argument("--project", required=True)
    evidence_review.add_argument("--target-commit", required=True)
    evidence_review.add_argument(
        "--source-package", action="append", required=True, type=Path
    )
    evidence_review.add_argument(
        "--integration-package", required=True, type=Path
    )
    evidence_review.add_argument("--output", required=True, type=Path)
    evidence_review.add_argument("--private-key", required=True, type=Path)
    evidence_review.add_argument("--reviewer", required=True)

    team = commands.add_parser(
        "team", help="Coordinate actors and exclusive task leases"
    )
    team_actions = team.add_subparsers(dest="team_action", required=True)
    team_status_parser = team_actions.add_parser(
        "status", help="Read actors, revision and active task leases"
    )
    team_status_parser.add_argument("--project", required=True)
    team_claim = team_actions.add_parser("claim", help="Claim one task atomically")
    team_claim.add_argument("--project", required=True)
    team_claim.add_argument("--task", required=True, dest="task_id")
    team_claim.add_argument("--actor", required=True, dest="actor_id")
    team_claim.add_argument("--expected-revision", required=True, type=int)
    team_claim.add_argument("--ttl-seconds", type=int, default=3600)
    team_release = team_actions.add_parser("release", help="Release an owned task lease")
    team_release.add_argument("--project", required=True)
    team_release.add_argument("--task", required=True, dest="task_id")
    team_release.add_argument("--actor", required=True, dest="actor_id")
    team_release.add_argument("--token", required=True)
    team_release.add_argument("--expected-revision", required=True, type=int)

    ci = commands.add_parser(
        "ci", help="Prepare, execute and import isolated trusted CI jobs"
    )
    ci_actions = ci.add_subparsers(dest="ci_action", required=True)
    ci_prepare = ci_actions.add_parser(
        "prepare", help="Create a commit- and contract-bound single-use CI job"
    )
    ci_prepare.add_argument("--project", required=True)
    ci_prepare.add_argument("--run", required=True, dest="run_id")
    ci_prepare.add_argument("--output", required=True, type=Path)
    ci_prepare.add_argument("--private-key", required=True, type=Path)
    ci_prepare.add_argument("--actor", required=True, dest="actor_id")
    ci_prepare.add_argument("--command", action="append", dest="command_ids")
    ci_prepare.add_argument(
        "--integration-source",
        action="append",
        type=Path,
        default=[],
        dest="integration_sources",
    )
    ci_prepare.add_argument("--ttl-seconds", type=int, default=3600)
    ci_execute = ci_actions.add_parser(
        "execute", help="Execute a CI job in an isolated Git worktree"
    )
    ci_execute.add_argument("--job", required=True, type=Path)
    ci_execute.add_argument("--checkout", required=True, type=Path)
    ci_execute.add_argument("--output", required=True, type=Path)
    ci_execute.add_argument("--job-trust-policy", required=True, type=Path)
    ci_execute.add_argument("--job-policy", default="job")
    ci_attest = ci_actions.add_parser(
        "attest", help="Sign a validated unsigned CI result outside the test sandbox"
    )
    ci_attest.add_argument("--job", required=True, type=Path)
    ci_attest.add_argument("--result", required=True, type=Path)
    ci_attest.add_argument("--output", required=True, type=Path)
    ci_attest.add_argument("--private-key", required=True, type=Path)
    ci_attest.add_argument("--actor", required=True, dest="actor_id")
    ci_attest.add_argument("--job-trust-policy", required=True, type=Path)
    ci_attest.add_argument("--job-policy", default="job")
    ci_import = ci_actions.add_parser(
        "import", help="Verify and import a signed CI result"
    )
    ci_import.add_argument("--project", required=True)
    ci_import.add_argument("--run", required=True, dest="run_id")
    ci_import.add_argument("--job", required=True, type=Path)
    ci_import.add_argument("--package", required=True, type=Path)
    ci_import.add_argument("--trust-policy", required=True, type=Path)
    ci_import.add_argument("--policy", default="ci")
    ci_import.add_argument("--job-trust-policy", required=True, type=Path)
    ci_import.add_argument("--job-policy", default="job")
    ci_github = ci_actions.add_parser(
        "github", help="Generate the GitHub Actions reference adapter"
    )
    ci_github.add_argument("--project", required=True)
    ci_github.add_argument("--output", required=True, type=Path)

    gate = commands.add_parser(
        "gate", help="Produce a fail-closed integration or release verdict"
    )
    gate_actions = gate.add_subparsers(dest="gate_action", required=True)
    gate_integration = gate_actions.add_parser(
        "integration", help="Require fresh integration CI evidence for source packages"
    )
    gate_integration.add_argument("--project", required=True)
    gate_integration.add_argument(
        "--source-package", action="append", required=True, type=Path
    )
    gate_integration.add_argument("--integration-package", required=True, type=Path)
    gate_integration.add_argument("--target-commit", required=True)
    gate_integration.add_argument("--trust-policy", required=True, type=Path)
    gate_integration.add_argument("--source-policy", default="contributor")
    gate_integration.add_argument("--integration-policy", default="release")
    gate_integration.add_argument("--review-package", required=True, type=Path)
    gate_integration.add_argument("--review-policy", default="reviewer")
    gate_integration.add_argument("--output", required=True, type=Path)

    run = commands.add_parser("run", help="Prepare one project-aware Codex task")
    run.add_argument("--project", required=True)
    run.add_argument(
        "--task",
        help="Plain-language task; optional only for a roadmap next task",
    )
    run.add_argument(
        "--intent", choices=["auto", "design", "build", "review"], default="auto"
    )
    run.add_argument(
        "--mode", choices=["auto", "quick", "standard", "deep"], default="auto"
    )
    run.add_argument("--changed-path", action="append", default=[])
    run.add_argument("--risk", action="append", default=[])
    run.add_argument("--spec")
    run.add_argument("--target-type", choices=["spec", "component", "repository"])
    run.add_argument("--target")
    run.add_argument("--backlog-item")

    spec = commands.add_parser("spec", help="Prepare a deep design proposal")
    spec.add_argument("--project", required=True)
    spec.add_argument("--task", required=True)
    spec.add_argument("--spec")

    implement = commands.add_parser(
        "next-task-new",
        aliases=["next-task_new"],
        help="Implement an approved deep spec",
    )
    implement.add_argument("--project", required=True)
    implement.add_argument("--task", required=True)
    implement.add_argument("--spec", required=True)
    implement.add_argument("--backlog-item")

    status = commands.add_parser("status", help="Read compact project status")
    status.add_argument("--project", required=True)
    status.add_argument("--task")

    history = commands.add_parser("history", help="Verify the project history chain")
    history.add_argument("--project", required=True)
    history.add_argument("--verify", action="store_true")

    map_status = commands.add_parser(
        "map-status", help="Read validity and freshness of the LLM system map"
    )
    map_status.add_argument("--project", required=True)

    close_run = commands.add_parser("_close-run", help=argparse.SUPPRESS)
    close_run.add_argument("--project", required=True)
    close_run.add_argument("--run", required=True, dest="run_id")
    close_run.add_argument("--result", required=True, type=Path)

    lock_feature = commands.add_parser("_lock-feature-contract", help=argparse.SUPPRESS)
    lock_feature.add_argument("--project", required=True)
    lock_feature.add_argument("--run", required=True, dest="run_id")

    approve = commands.add_parser("_approve-spec", help=argparse.SUPPRESS)
    approve.add_argument("--project", required=True)
    approve.add_argument("--run", required=True, dest="run_id")
    approve.add_argument("--approval", required=True, type=Path)

    role_target = commands.add_parser("_role-target", help=argparse.SUPPRESS)
    role_target.add_argument("--project", required=True)
    role_target.add_argument("--run", required=True, dest="run_id")

    canary = commands.add_parser("_project-canary", help=argparse.SUPPRESS)
    canary.add_argument("--project", required=True)

    activate = commands.add_parser("_project-activate", help=argparse.SUPPRESS)
    activate.add_argument("--project", required=True)

    public = {
        "doctor",
        "projects",
        "upgrade-1-4",
        "upgrade-1-5",
        "register",
        "init",
        "project",
        "codex",
        "claude",
        "coordinator",
        "collaboration",
        "github-auth",
        "github-app",
        "feature",
        "lifecycle",
        "specify",
        "clarify",
        "plan",
        "tasks",
        "implement",
        "verify",
        "converge",
        "contract",
        "release-check",
        "key",
        "identity",
        "access",
        "backlog",
        "evidence",
        "team",
        "ci",
        "gate",
        "run",
        "spec",
        "next-task-new",
        "status",
        "history",
        "map-status",
        "preflight",
        "governance",
    }
    commands._choices_actions[:] = [
        action for action in commands._choices_actions if action.dest in public
    ]
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor" and not args.project:
            result = run_framework_doctor(args.project_root)
        elif args.command == "projects":
            result = list_registered_projects()
        elif args.command == "codex":
            if args.codex_action == "install":
                result = install_codex_integration(
                    github_client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                    replace=args.replace,
                )
            elif args.codex_action == "status":
                result = codex_integration_status()
            elif args.codex_action == "remove":
                result = remove_codex_integration(force=args.force)
            else:
                parser.error(f"unsupported Codex action: {args.codex_action}")
        elif args.command == "claude":
            if args.claude_action == "install":
                result = install_claude_integration(
                    github_client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                    replace=args.replace,
                )
            elif args.claude_action == "status":
                result = claude_integration_status()
            elif args.claude_action == "remove":
                result = remove_claude_integration(force=args.force)
            else:
                parser.error(f"unsupported Claude action: {args.claude_action}")
        elif args.command == "register":
            result = register_project(
                args.project,
                docs_root=args.docs_root,
                code_root=args.code_root,
                mode="shadow",
            )
        elif args.command == "init":
            framework = run_framework_doctor(args.project_root)
            if framework.get("ok") is not True:
                raise AriaError("Framework doctor failed before project initialization")
            result = initialize_project(
                args.project,
                code_root=args.code_root,
                docs_root=args.docs_root,
                display_name=args.display_name,
            )
        elif args.command == "project":
            if args.project_action == "create-plan":
                result = create_collaborative_project_plan(
                    project_id=args.project,
                    owner=args.owner,
                    repository_name=args.repository_name,
                    code_root=args.code_root,
                    docs_root=args.docs_root,
                    private=args.private,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
            elif args.project_action == "create":
                result = create_collaborative_project(
                    project_id=args.project,
                    owner=args.owner,
                    repository_name=args.repository_name,
                    code_root=args.code_root,
                    docs_root=args.docs_root,
                    private=args.private,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                    expected_plan_sha256=args.expected_plan_sha256,
                    confirm=args.confirm,
                )
            elif args.project_action == "join":
                result = join_collaborative_project(
                    project_id=args.project,
                    repository_url=args.repository_url,
                    code_root=args.code_root,
                    docs_root=args.docs_root,
                    code_branch=args.code_branch,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
            elif args.project_action == "connect-plan":
                result = connect_existing_project_plan(
                    project_id=args.project,
                    repository_url=args.repository_url,
                    code_root=args.code_root,
                    docs_root=args.docs_root,
                    base_branch=args.base_branch,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
            elif args.project_action == "connect":
                result = connect_existing_project(
                    project_id=args.project,
                    repository_url=args.repository_url,
                    code_root=args.code_root,
                    docs_root=args.docs_root,
                    base_branch=args.base_branch,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                    expected_plan_sha256=args.expected_plan_sha256,
                    confirm=args.confirm,
                )
            else:
                parser.error(f"unsupported project action: {args.project_action}")
        elif args.command == "coordinator":
            if args.coordinator_action == "install":
                runtime_root = (
                    args.runtime_root.resolve(strict=False)
                    if args.runtime_root is not None
                    else default_runtime_root().resolve(strict=False)
                )
                project = load_project(
                    args.project,
                    framework_root=args.project_root,
                    runtime_root=runtime_root,
                )
                login = default_github_login_service(
                    args.github_client_id
                ).status()
                if login.get("logged_in") is not True:
                    raise WorkflowError(
                        "coordinator install requires an active GitHub device session"
                    )
                app = github_app_key_status(
                    backend=WindowsCredentialBackend(),
                    app_id=args.coordinator_integration_id,
                )
                if app.get("configured") is not True:
                    raise WorkflowError(
                        "coordinator install requires the GitHub App key"
                    )
                result = install_coordinator_schedule(
                    project,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                    aria_executable=resolve_aria_executable(args.aria_executable),
                    interval_minutes=args.interval_minutes,
                    max_requests=args.max_requests,
                    max_pull_requests=args.max_pull_requests,
                )
            elif args.coordinator_action == "status":
                result = coordinator_schedule_status(
                    project_id=args.project,
                    runtime_root=args.runtime_root,
                )
            elif args.coordinator_action == "remove":
                result = remove_coordinator_schedule(
                    project_id=args.project,
                    runtime_root=args.runtime_root,
                )
            elif args.coordinator_action == "trigger":
                result = trigger_coordinator_schedule(project_id=args.project)
            elif args.coordinator_action == "run":
                result = run_configured_coordinator(
                    config_path=args.config,
                    expected_config_sha256=args.expected_config_sha256,
                )
            else:
                parser.error(
                    f"unsupported coordinator action: {args.coordinator_action}"
                )
        elif args.command == "collaboration":
            if args.collaboration_action in {"plan", "enable"}:
                provider_adapter = None
                if args.github_client_id is not None:
                    if args.provider != "github":
                        raise WorkflowError(
                            "GitHub session options require --provider github"
                        )
                    provider_adapter = build_authenticated_github_adapter(
                        code_root=args.code_root,
                        remote=args.remote,
                        client_id=args.github_client_id,
                        coordinator_integration_id=args.coordinator_integration_id,
                    )
            if args.collaboration_action == "plan":
                result = collaboration_plan(
                    project_id=args.project,
                    code_root=args.code_root,
                    docs_root=args.docs_root,
                    provider=args.provider,
                    repository_id=args.repository_id,
                    remote=args.remote,
                    integration_branch=args.integration_branch,
                    control_branch=args.control_branch,
                    provider_adapter=provider_adapter,
                )
            elif args.collaboration_action == "enable":
                if args.coordinator_integration_id is None:
                    raise WorkflowError(
                        "collaboration enable requires --coordinator-integration-id"
                    )
                control_writer = build_github_control_writer(
                    code_root=args.code_root,
                    remote=args.remote,
                    repository_id=args.repository_id,
                    app_id=args.coordinator_integration_id,
                    control_branch=args.control_branch,
                )
                git_environment = (
                    build_github_git_environment(client_id=args.github_client_id)
                    if args.github_client_id is not None
                    else None
                )
                result = enable_collaboration(
                    project_id=args.project,
                    code_root=args.code_root,
                    docs_root=args.docs_root,
                    provider=args.provider,
                    repository_id=args.repository_id,
                    remote=args.remote,
                    integration_branch=args.integration_branch,
                    control_branch=args.control_branch,
                    expected_plan_sha256=args.expected_plan_sha256,
                    confirm=args.confirm,
                    provider_adapter=provider_adapter,
                    control_writer=control_writer,
                    git_environment=git_environment,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
            elif args.collaboration_action in {"migrate-plan", "migrate"}:
                runtime_root = (
                    args.runtime_root.resolve(strict=False)
                    if args.runtime_root is not None
                    else default_runtime_root().resolve(strict=False)
                )
                project = load_project(
                    args.project,
                    framework_root=args.project_root,
                    runtime_root=runtime_root,
                )
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=args.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                mappings = parse_actor_mappings(args.actor_map)
                if args.collaboration_action == "migrate-plan":
                    result = collaborative_migration_plan(
                        project_id=args.project,
                        provider=args.provider,
                        repository_id=args.repository_id,
                        actor_mappings=mappings,
                        coordinator_integration_id=args.coordinator_integration_id,
                        provider_adapter=adapter,
                        framework_root=args.project_root,
                        runtime_root=runtime_root,
                        docs_root=args.docs_root,
                        remote=args.remote,
                        integration_branch=args.integration_branch,
                        control_branch=args.control_branch,
                    )
                else:
                    writer = build_github_control_writer(
                        code_root=project.code_root,
                        remote=args.remote,
                        repository_id=args.repository_id,
                        app_id=args.coordinator_integration_id,
                        control_branch=args.control_branch,
                    )
                    result = migrate_offline_project(
                        project_id=args.project,
                        provider=args.provider,
                        repository_id=args.repository_id,
                        actor_mappings=mappings,
                        coordinator_integration_id=args.coordinator_integration_id,
                        expected_plan_sha256=args.expected_plan_sha256,
                        confirm=args.confirm,
                        provider_adapter=adapter,
                        control_writer=writer,
                        framework_root=args.project_root,
                        runtime_root=runtime_root,
                        docs_root=args.docs_root,
                        remote=args.remote,
                        integration_branch=args.integration_branch,
                        control_branch=args.control_branch,
                        git_environment=build_github_git_environment(
                            client_id=args.github_client_id
                        ),
                    )
            elif args.collaboration_action == "migration-status":
                result = collaborative_migration_status(
                    project_id=args.project, runtime_root=args.runtime_root
                )
            elif args.collaboration_action == "migration-rollback":
                runtime_root = (
                    args.runtime_root.resolve(strict=False)
                    if args.runtime_root is not None
                    else default_runtime_root().resolve(strict=False)
                )
                project = load_project(
                    args.project,
                    framework_root=args.project_root,
                    runtime_root=runtime_root,
                )
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                writer = build_github_control_writer(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                    control_branch=control.control_branch,
                )
                result = rollback_collaborative_migration(
                    project_id=args.project,
                    expected_control_commit=args.expected_control_commit,
                    confirm=args.confirm,
                    control_writer=writer,
                    runtime_root=runtime_root,
                )
            elif args.collaboration_action == "team-status":
                project = load_project(args.project, framework_root=args.project_root)
                result = collaborative_team_status(project)
            elif args.collaboration_action == "team-sync":
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                control_writer = build_github_control_writer(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                    control_branch=control.control_branch,
                )
                git_environment = build_github_git_environment(
                    client_id=args.github_client_id
                )
                result = sync_authenticated_team(
                    project,
                    adapter=adapter,
                    coordinator_integration_id=args.coordinator_integration_id,
                    expected_revision=args.expected_revision,
                    sync_id=args.sync_id,
                    control_writer=control_writer,
                    git_environment=git_environment,
                )
            elif args.collaboration_action in {"team-invite", "team-revoke"}:
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                manager = build_github_collaborator_manager(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                )
                control_writer = build_github_control_writer(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                    control_branch=control.control_branch,
                )
                git_environment = build_github_git_environment(
                    client_id=args.github_client_id
                )
                common = {
                    "project": project,
                    "adapter": adapter,
                    "manager": manager,
                    "username": args.username,
                    "coordinator_integration_id": args.coordinator_integration_id,
                    "expected_revision": args.expected_revision,
                    "request_id": args.request_id,
                    "control_writer": control_writer,
                    "git_environment": git_environment,
                }
                result = (
                    invite_authenticated_member(permission=args.permission, **common)
                    if args.collaboration_action == "team-invite"
                    else revoke_authenticated_member(**common)
                )
            elif args.collaboration_action == "state-status":
                project = load_project(args.project, framework_root=args.project_root)
                result = collaborative_state_status(project)
            elif args.collaboration_action == "state-sync":
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                control_writer = build_github_control_writer(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                    control_branch=control.control_branch,
                )
                verifier = build_github_app_integration_verifier(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                )
                git_environment = build_github_git_environment(
                    client_id=args.github_client_id
                )
                result = sync_accepted_pull_request(
                    project,
                    coordinator_adapter=adapter,
                    verifier=verifier,
                    control_writer=control_writer,
                    coordinator_integration_id=args.coordinator_integration_id,
                    item_id=args.item,
                    pull_request_number=args.pull_request,
                    expected_backlog_revision=args.expected_backlog_revision,
                    expected_state_revision=args.expected_state_revision,
                    git_environment=git_environment,
                )
            elif args.collaboration_action == "backlog-list":
                project = load_project(args.project, framework_root=args.project_root)
                result = collaborative_backlog_status(
                    project, include_done=args.include_done
                )
            elif args.collaboration_action == "backlog-mine":
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                result = authenticated_backlog_status(
                    project,
                    adapter=adapter,
                    include_done=args.include_done,
                )
            elif args.collaboration_action.startswith("backlog-"):
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                action = args.collaboration_action.removeprefix("backlog-").replace("-", "_")
                if action == "add":
                    item_id = None
                    payload = {
                        "title": args.title,
                        "description": args.description,
                        "priority": args.priority,
                        "source_id": args.source_id,
                        "dependencies": sorted(set(args.dependency)),
                        "evidence_required": args.evidence_required,
                    }
                elif action == "triage":
                    item_id = args.item
                    payload = {
                        "assignee_provider": "github",
                        "assignee_user_id": args.assignee_user_id,
                        "priority": args.priority,
                        "requirements": sorted(set(args.requirement)),
                        "acceptance_criteria": sorted(set(args.acceptance)),
                        "dependencies": sorted(set(args.dependency)),
                        "scope_paths": sorted(set(args.scope_path)),
                        "evidence_required": args.evidence_required,
                    }
                elif action == "assign":
                    item_id = args.item
                    payload = {
                        "assignee_provider": "github",
                        "assignee_user_id": args.assignee_user_id,
                    }
                elif action == "claim":
                    item_id = args.item
                    payload = {}
                elif action == "block":
                    item_id = args.item
                    payload = {"reason": args.reason}
                elif action == "cancel":
                    item_id = args.item
                    payload = {"reason": args.reason}
                elif action == "amend_scope":
                    item_id = args.item
                    payload = {"scope_paths": sorted(set(args.scope_path))}
                elif action == "recover":
                    item_id = args.item
                    payload = {
                        "requirements": sorted(set(args.requirement)),
                        "acceptance_criteria": sorted(set(args.acceptance)),
                        "scope_paths": sorted(set(args.scope_path)),
                        "branch": args.branch,
                        "target_status": args.target_status,
                    }
                else:
                    raise WorkflowError("unsupported collaborative backlog action")
                if args.delivery == "github-queue":
                    queue = build_authenticated_github_request_queue(
                        code_root=project.code_root,
                        remote=control.remote,
                        client_id=args.github_client_id,
                    )
                    result = enqueue_backlog_action(
                        project,
                        adapter=adapter,
                        queue=queue,
                        expected_revision=args.expected_revision,
                        action=action,
                        item_id=item_id,
                        payload=payload,
                        request_id=args.request_id,
                    )
                else:
                    control_writer = build_github_control_writer(
                        code_root=project.code_root,
                        remote=control.remote,
                        repository_id=control.repository_id,
                        app_id=args.coordinator_integration_id,
                        control_branch=control.control_branch,
                    )
                    git_environment = build_github_git_environment(
                        client_id=args.github_client_id
                    )
                    result = submit_authenticated_backlog_action(
                        project,
                        adapter=adapter,
                        control_writer=control_writer,
                        coordinator_integration_id=args.coordinator_integration_id,
                        expected_revision=args.expected_revision,
                        action=action,
                        item_id=item_id,
                        payload=payload,
                        request_id=args.request_id,
                        git_environment=git_environment,
                    )
            elif args.collaboration_action == "activity-list":
                project = load_project(args.project, framework_root=args.project_root)
                result = collaborative_activity_status(project)
            elif args.collaboration_action == "activity-mine":
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                result = authenticated_activity_status(project, adapter=adapter)
            elif args.collaboration_action == "activity-set":
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                if args.delivery == "github-queue":
                    queue = build_authenticated_github_request_queue(
                        code_root=project.code_root,
                        remote=control.remote,
                        client_id=args.github_client_id,
                    )
                    result = enqueue_activity(
                        project,
                        adapter=adapter,
                        queue=queue,
                        expected_revision=args.expected_revision,
                        task_id=args.task,
                        stage=args.stage,
                        branch=args.branch,
                        note=args.note,
                        event_id=args.event_id,
                    )
                else:
                    control_writer = build_github_control_writer(
                        code_root=project.code_root,
                        remote=control.remote,
                        repository_id=control.repository_id,
                        app_id=args.coordinator_integration_id,
                        control_branch=control.control_branch,
                    )
                    git_environment = build_github_git_environment(
                        client_id=args.github_client_id
                    )
                    result = submit_authenticated_activity(
                        project,
                        adapter=adapter,
                        control_writer=control_writer,
                        expected_revision=args.expected_revision,
                        task_id=args.task,
                        stage=args.stage,
                        branch=args.branch,
                        note=args.note,
                        event_id=args.event_id,
                        git_environment=git_environment,
                    )
            elif args.collaboration_action == "activity-outbox-status":
                project = load_project(args.project, framework_root=args.project_root)
                result = activity_outbox_status(project)
            elif args.collaboration_action == "activity-outbox-flush":
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                queue = build_authenticated_github_request_queue(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                )
                result = flush_activity_outbox(
                    project,
                    adapter=adapter,
                    queue=queue,
                    maximum=args.max_requests,
                )
            elif args.collaboration_action == "queue-process":
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                queue = build_github_app_request_queue(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                )
                control_writer = build_github_control_writer(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                    control_branch=control.control_branch,
                )
                git_environment = build_github_git_environment(
                    client_id=args.github_client_id
                )
                result = process_github_request_queue(
                    project,
                    coordinator_adapter=adapter,
                    queue=queue,
                    control_writer=control_writer,
                    coordinator_integration_id=args.coordinator_integration_id,
                    max_requests=args.max_requests,
                    git_environment=git_environment,
                )
            elif args.collaboration_action == "coordinator-run-once":
                project = load_project(args.project, framework_root=args.project_root)
                control = load_control_contract(project.docs_root / "CONTROL.yaml")
                adapter = build_authenticated_github_adapter(
                    code_root=project.code_root,
                    remote=control.remote,
                    client_id=args.github_client_id,
                    coordinator_integration_id=args.coordinator_integration_id,
                )
                queue = build_github_app_request_queue(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                )
                writer = build_github_control_writer(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                    control_branch=control.control_branch,
                )
                verifier = build_github_app_integration_verifier(
                    code_root=project.code_root,
                    remote=control.remote,
                    repository_id=control.repository_id,
                    app_id=args.coordinator_integration_id,
                )
                git_environment = build_github_git_environment(
                    client_id=args.github_client_id
                )
                result = run_collaborative_coordinator_once(
                    project,
                    coordinator_adapter=adapter,
                    request_queue=queue,
                    control_writer=writer,
                    integration_verifier=verifier,
                    coordinator_integration_id=args.coordinator_integration_id,
                    max_requests=args.max_requests,
                    max_pull_requests=args.max_pull_requests,
                    git_environment=git_environment,
                )
            else:
                parser.error(
                    f"unsupported collaboration action: {args.collaboration_action}"
                )
        elif args.command == "github-auth":
            service = default_github_login_service(args.client_id)
            if args.github_auth_action == "login-begin":
                result = service.begin(repository_id=args.repository_id)
            elif args.github_auth_action == "login-complete":
                result = service.complete(repository_id=args.repository_id)
            elif args.github_auth_action == "status":
                result = service.status()
            elif args.github_auth_action == "logout":
                result = service.logout(repository_id=args.repository_id)
            else:
                parser.error(
                    f"unsupported GitHub auth action: {args.github_auth_action}"
                )
        elif args.command == "github-app":
            credential_backend = WindowsCredentialBackend()
            if args.github_app_action == "configure":
                result = configure_github_app_key(
                    backend=credential_backend,
                    app_id=args.app_id,
                    private_key_path=args.private_key,
                )
            elif args.github_app_action == "status":
                result = github_app_key_status(
                    backend=credential_backend, app_id=args.app_id
                )
            elif args.github_app_action == "remove":
                result = remove_github_app_key(
                    backend=credential_backend, app_id=args.app_id
                )
            else:
                parser.error(
                    f"unsupported GitHub App action: {args.github_app_action}"
                )
        elif args.command == "release-check":
            result = run_release_check(args.project_root, output_dir=args.output)
        elif args.command == "key":
            result = generate_keypair(args.private_key, args.public_key)
        elif args.command == "identity":
            runtime_root = default_runtime_root()
            if args.identity_action == "enroll":
                result = enroll_identity(
                    runtime_root,
                    actor_id=args.actor_id,
                    device_id=args.device_id,
                    display_name=args.display_name,
                    email=args.email,
                    request_path=args.request_path,
                )
            else:
                result = identity_status(runtime_root, actor_id=args.actor_id)
        elif args.command == "evidence" and args.evidence_action == "verify":
            result = verify_evidence(
                args.package,
                trust_policy_path=args.trust_policy,
                policy_name=args.policy,
                actor_roles=args.actor_roles,
                approval_count=args.approval_count,
            )
        elif args.command == "evidence" and args.evidence_action == "inspect":
            result = inspect_evidence(args.package)
        elif args.command == "evidence" and args.evidence_action == "review-attest":
            result = create_review_attestation(
                output_path=args.output,
                private_key_path=args.private_key,
                reviewer_id=args.reviewer,
                project_id=args.project,
                target_commit=args.target_commit,
                source_package_paths=args.source_package,
                integration_package_path=args.integration_package,
            )
        elif args.command == "ci" and args.ci_action == "execute":
            result = execute_ci_job(
                job_path=args.job,
                checkout=args.checkout,
                output_path=args.output,
                job_trust_policy_path=args.job_trust_policy,
                job_policy_name=args.job_policy,
            )
        elif args.command == "ci" and args.ci_action == "attest":
            result = attest_ci_result(
                job_path=args.job,
                unsigned_result_path=args.result,
                output_path=args.output,
                private_key_path=args.private_key,
                actor_id=args.actor_id,
                job_trust_policy_path=args.job_trust_policy,
                job_policy_name=args.job_policy,
            )
        elif args.command == "ci" and args.ci_action == "github":
            result = write_github_workflow(
                project_id=args.project,
                output_path=args.output,
            )
        else:
            project_id = getattr(args, "project", None)
            project = load_project(project_id, framework_root=args.project_root)
            _authorize_project_command(project, args)
            if args.command == "doctor":
                result = run_project_doctor(project)
            elif args.command == "preflight":
                result = governance_preflight(
                    project, operation=args.operation, run_id=args.run_id
                )
            elif args.command == "governance":
                if args.governance_action == "check":
                    result = governance_diagnostics(project)
                elif args.governance_action == "plan":
                    result = {
                        "ok": True,
                        "project": project.project_id,
                        **decision_reconciliation_plan(project),
                    }
                elif args.governance_action == "reconcile":
                    branch = _verified_backlog_branch(project, args.branch)
                    result = reconcile_accepted_decisions(
                        project,
                        item_ids=args.item,
                        expected_revision=args.expected_revision,
                        confirm_acceptance=args.confirm_acceptance,
                        actor_id=args.identity_actor,
                        device_id=args.identity_device,
                        version=args.version or project.framework_version,
                        branch=branch,
                    )
                else:
                    parser.error(
                        f"unsupported governance action: {args.governance_action}"
                    )
            elif args.command == "upgrade-1-4":
                result = upgrade_project_to_1_4(project)
            elif args.command == "upgrade-1-5":
                result = upgrade_project_to_1_5(project)
            elif args.command == "access":
                if args.access_action == "bootstrap":
                    result = bootstrap_access(
                        project,
                        actor_id=args.identity_actor,
                        device_id=args.identity_device,
                    )
                elif args.access_action == "status":
                    result = access_status(project)
                elif args.access_action == "grant":
                    result = grant_access(
                        project,
                        request_path=args.request,
                        permissions=args.permission,
                        versions=args.version or ["*"],
                        branches=args.branch or ["*"],
                        expected_revision=args.expected_revision,
                        admin_actor_id=args.identity_actor,
                        admin_device_id=args.identity_device,
                    )
                elif args.access_action == "revoke":
                    result = revoke_access(
                        project,
                        actor_id=args.target_actor_id,
                        device_id=args.target_device_id,
                        expected_revision=args.expected_revision,
                        admin_actor_id=args.identity_actor,
                        admin_device_id=args.identity_device,
                    )
                elif args.access_action == "audit":
                    result = verify_access_audit(project)
                else:
                    parser.error(f"unsupported access action: {args.access_action}")
            elif args.command == "backlog":
                identity_options = {
                    "actor_id": args.identity_actor,
                    "device_id": args.identity_device,
                }
                branch = _verified_backlog_branch(
                    project, getattr(args, "branch", None)
                )
                if args.backlog_action == "add":
                    result = add_backlog_item(
                        project,
                        title=args.title,
                        item_type=args.item_type,
                        priority=args.priority,
                        target_versions=args.target_version,
                        acceptance=args.acceptance,
                        source_kind=args.source_kind,
                        source_ref=args.source_ref,
                        requirements=args.requirement,
                        refs=args.ref,
                        dependencies=args.dependency,
                        assignee=args.assignee,
                        expected_revision=args.expected_revision,
                        branch=branch,
                        **identity_options,
                    )
                elif args.backlog_action == "list":
                    result = list_backlog_items(
                        project,
                        status=args.status,
                        assignee=args.assignee,
                        version=args.version,
                        branch=branch,
                        **identity_options,
                    )
                elif args.backlog_action == "show":
                    result = show_backlog_item(
                        project,
                        item_id=args.item_id,
                        branch=branch,
                        **identity_options,
                    )
                elif args.backlog_action == "assign":
                    result = assign_backlog_item(
                        project,
                        item_id=args.item_id,
                        assignee=args.assignee,
                        expected_revision=args.expected_revision,
                        branch=branch,
                        **identity_options,
                    )
                elif args.backlog_action == "claim":
                    result = claim_backlog_item(
                        project,
                        item_id=args.item_id,
                        expected_revision=args.expected_revision,
                        branch=branch,
                        **identity_options,
                    )
                elif args.backlog_action == "block":
                    result = block_backlog_item(
                        project,
                        item_id=args.item_id,
                        reason=args.reason,
                        expected_revision=args.expected_revision,
                        branch=branch,
                        **identity_options,
                    )
                elif args.backlog_action == "done":
                    result = complete_backlog_item(
                        project,
                        item_id=args.item_id,
                        evidence=args.evidence,
                        expected_revision=args.expected_revision,
                        branch=branch,
                        **identity_options,
                    )
                elif args.backlog_action == "sync":
                    result = sync_backlog(
                        project,
                        expected_revision=args.expected_revision,
                        version=args.version or project.framework_version,
                        branch=branch,
                        **identity_options,
                    )
                elif args.backlog_action == "audit":
                    result = backlog_audit(
                        project,
                        version=args.version or project.framework_version,
                        branch=branch,
                        **identity_options,
                    )
                else:
                    parser.error(f"unsupported backlog action: {args.backlog_action}")
            elif args.command == "run":
                result = start_project_run(
                    project,
                    task=args.task,
                    intent=args.intent,
                    mode=args.mode,
                    changed_paths=args.changed_path,
                    risk_flags=args.risk,
                    spec=args.spec,
                    target_type=args.target_type,
                    target=args.target,
                    backlog_item_id=args.backlog_item,
                    actor_id=args.identity_actor,
                    device_id=args.identity_device,
                )
            elif args.command == "feature":
                result = start_feature(
                    project,
                    task=args.task,
                    mode=args.mode,
                    spec=args.spec,
                    changed_paths=args.changed_path,
                    risk_flags=args.risk,
                    backlog_item_id=args.backlog_item,
                    actor_id=args.identity_actor,
                    device_id=args.identity_device,
                )
            elif args.command == "lifecycle":
                result = lifecycle_status(project, run_id=args.run_id)
            elif args.command in {"specify", "clarify", "plan", "tasks"}:
                result = submit_lifecycle_phase(
                    project,
                    run_id=args.run_id,
                    phase=args.command,
                    input_path=args.input_path,
                    contract_path=getattr(args, "contract", None),
                )
            elif args.command == "implement":
                result = begin_implementation(project, run_id=args.run_id)
            elif args.command == "verify":
                result = verify_project_run(
                    project,
                    run_id=args.run_id,
                    command_ids=args.command_ids,
                    links_path=args.links_path,
                    resume=args.resume,
                    actor_id=args.identity_actor,
                    device_id=args.identity_device,
                )
            elif args.command == "converge":
                result = converge_feature(
                    project,
                    run_id=args.run_id,
                    result_path=args.result,
                    actor_id=args.identity_actor,
                    device_id=args.identity_device,
                )
            elif args.command == "evidence" and args.evidence_action == "export":
                result = export_run_package(
                    project=project,
                    run_id=args.run_id,
                    output_path=args.output,
                    private_key_path=args.private_key,
                    actor_id=args.actor,
                )
            elif args.command == "team":
                if args.team_action == "status":
                    result = team_status(project)
                elif args.team_action == "claim":
                    result = claim_task(
                        project,
                        task_id=args.task_id,
                        actor_id=args.actor_id,
                        expected_revision=args.expected_revision,
                        ttl_seconds=args.ttl_seconds,
                    )
                elif args.team_action == "release":
                    result = release_task(
                        project,
                        task_id=args.task_id,
                        actor_id=args.actor_id,
                        token=args.token,
                        expected_revision=args.expected_revision,
                    )
                else:
                    parser.error(f"unsupported team action: {args.team_action}")
            elif args.command == "ci":
                if args.ci_action == "prepare":
                    result = prepare_ci_job(
                        project,
                        run_id=args.run_id,
                        output_path=args.output,
                        private_key_path=args.private_key,
                        actor_id=args.actor_id,
                        command_ids=args.command_ids,
                        integration_source_packages=args.integration_sources,
                        ttl_seconds=args.ttl_seconds,
                    )
                elif args.ci_action == "import":
                    result = import_ci_result(
                        project,
                        run_id=args.run_id,
                        job_path=args.job,
                        package_path=args.package,
                        trust_policy_path=args.trust_policy,
                        policy_name=args.policy,
                        job_trust_policy_path=args.job_trust_policy,
                        job_policy_name=args.job_policy,
                    )
                else:
                    parser.error(f"unsupported ci action: {args.ci_action}")
            elif args.command == "gate" and args.gate_action == "integration":
                result = run_integration_gate(
                    project,
                    source_packages=args.source_package,
                    integration_package=args.integration_package,
                    target_commit=args.target_commit,
                    trust_policy_path=args.trust_policy,
                    source_policy=args.source_policy,
                    integration_policy=args.integration_policy,
                    review_package=args.review_package,
                    review_policy=args.review_policy,
                    output_path=args.output,
                )
            elif args.command == "contract":
                if args.contract_action == "amend":
                    result = amend_feature_contract(
                        project,
                        run_id=args.run_id,
                        contract_path=args.contract_path,
                        reason=args.reason,
                    )
                elif args.contract_action == "import-spec-kit":
                    result = import_spec_kit(
                        project, run_id=args.run_id, spec_dir=args.spec_dir
                    )
                elif args.contract_action == "export-spec-kit":
                    result = export_spec_kit(
                        project, run_id=args.run_id, output_dir=args.output_dir
                    )
                else:
                    parser.error(f"unsupported contract action: {args.contract_action}")
            elif args.command == "spec":
                result = start_project_run(
                    project,
                    task=args.task,
                    intent="design",
                    mode="deep",
                    spec=args.spec,
                    actor_id=args.identity_actor,
                    device_id=args.identity_device,
                )
            elif args.command in {"next-task-new", "next-task_new"}:
                result = start_project_run(
                    project,
                    task=args.task,
                    intent="build",
                    mode="deep",
                    spec=args.spec,
                    backlog_item_id=args.backlog_item,
                    actor_id=args.identity_actor,
                    device_id=args.identity_device,
                )
            elif args.command == "status":
                result = project_status(project, task_id=args.task)
            elif args.command == "history":
                result = verify_history(project)
            elif args.command == "map-status":
                result = load_system_map(
                    project,
                    git_snapshot(project.code_root, project.git_ignore_prefixes),
                )
                result.pop("content", None)
                result["ok"] = result.get("valid") is True
            elif args.command == "_close-run":
                result = close_project_run(
                    project,
                    run_id=args.run_id,
                    result_path=args.result,
                    actor_id=args.identity_actor,
                    device_id=args.identity_device,
                )
            elif args.command == "_lock-feature-contract":
                result = lock_project_feature_contract(project, run_id=args.run_id)
            elif args.command == "_approve-spec":
                result = approve_project_spec(
                    project, run_id=args.run_id, approval_path=args.approval
                )
            elif args.command == "_role-target":
                result = project_role_target(project, run_id=args.run_id)
            elif args.command == "_project-canary":
                result = run_project_canary(
                    project,
                    actor_id=args.identity_actor,
                    device_id=args.identity_device,
                )
            elif args.command == "_project-activate":
                result = activate_project(
                    project,
                    actor_id=args.identity_actor,
                    device_id=args.identity_device,
                )
            else:
                parser.error(f"unsupported command: {args.command}")
        _print(result)
        return 0 if not isinstance(result, dict) or result.get("ok", True) is True else 1
    except AriaError as error:
        _print({"ok": False, "error": type(error).__name__, "message": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
