from __future__ import annotations

import re
import subprocess
from pathlib import Path

from aria.errors import ConfigurationError, WorkflowError
from aria.evidence_package import inspect_package, verify_package
from aria.io import atomic_write_json
from aria.project import canonical_sha, git_snapshot
from aria.team import validate_actor_role
from aria.trust import evaluate_policy_context, load_trust_policy

COMMIT_RE = re.compile(r"[0-9a-f]{40,64}")


def _require_ancestor(code_root: Path, source: str, target: str) -> None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={code_root.resolve()}",
                "merge-base",
                "--is-ancestor",
                source,
                target,
            ],
            cwd=code_root,
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkflowError("Git ancestry verification failed") from error
    if completed.returncode != 0:
        raise WorkflowError(
            f"Source commit {source} is not contained in target {target}"
        )


def _passed_subject(subject: object, label: str) -> dict[str, object]:
    if not isinstance(subject, dict):
        raise WorkflowError(f"{label} package subject is malformed")
    kind = subject.get("kind")
    if kind == "run":
        if subject.get("run_status") != "completed":
            raise WorkflowError(f"{label} run is not completed")
        if subject.get("source_changed_count") != 0:
            raise WorkflowError(
                f"{label} run is not bound to a clean committed Git tree"
            )
    elif kind == "ci-result":
        if subject.get("result_ok") is not True:
            raise WorkflowError(f"{label} CI result did not pass")
    else:
        raise WorkflowError(f"{label} has unsupported subject kind: {kind!r}")
    return subject


def run_integration_gate(
    project: object,
    *,
    source_packages: list[Path],
    integration_package: Path,
    target_commit: str,
    trust_policy_path: Path,
    source_policy: str,
    integration_policy: str,
    review_package: Path,
    review_policy: str,
    output_path: Path,
) -> dict[str, object]:
    if len(source_packages) < 2:
        raise ConfigurationError("Integration gate requires at least two source packages")
    if COMMIT_RE.fullmatch(target_commit) is None:
        raise ConfigurationError("Integration target commit must be a full Git object id")
    if output_path.exists():
        raise ConfigurationError(f"Refusing to overwrite integration verdict: {output_path}")
    snapshot = git_snapshot(project.code_root, project.git_ignore_prefixes)
    if snapshot.get("dirty") is not False or snapshot.get("head") != target_commit:
        raise WorkflowError(
            "Integration target must be the exact clean checkout HEAD"
        )
    policy = load_trust_policy(trust_policy_path)
    sources: list[dict[str, object]] = []
    hashes: set[str] = set()
    contributor_ids: set[str] = set()
    for index, package in enumerate(source_packages):
        inspected = inspect_package(package)
        inspected_actor = inspected.get("actor_id")
        if not isinstance(inspected_actor, str):
            raise WorkflowError(f"Source {index} has no actor identity")
        actor = validate_actor_role(
            project,
            actor_id=inspected_actor,
            allowed_roles={"contributor", "maintainer", "release-manager", "ci"},
        )
        verdict = verify_package(
            package,
            trust_policy_path=trust_policy_path,
            policy_name=source_policy,
            actor_roles=list(actor["roles"]),
            approval_count=0,
        )
        subject = _passed_subject(verdict.get("subject"), f"Source {index}")
        if subject.get("project_id") != project.project_id:
            raise WorkflowError(f"Source {index} belongs to another project")
        source_commit = subject.get("source_commit")
        if not isinstance(source_commit, str) or COMMIT_RE.fullmatch(source_commit) is None:
            raise WorkflowError(f"Source {index} has no full Git commit binding")
        if subject.get("kind") == "run" and (
            subject.get("source_changed_count") != 0
            or subject.get("source_working_tree_sha256") != canonical_sha({})
        ):
            raise WorkflowError(
                f"Source {index} run evidence is not bound to a clean committed tree"
            )
        actor_id = verdict.get("actor_id")
        if not isinstance(actor_id, str):
            raise WorkflowError(f"Source {index} has no actor identity")
        if actor_id != inspected_actor:
            raise WorkflowError(f"Source {index} actor identity changed")
        evaluate_policy_context(
            policy=policy,
            policy_name=source_policy,
            actor_roles=list(actor["roles"]),
            approval_count=0,
        )
        contributor_ids.add(actor_id)
        _require_ancestor(project.code_root, source_commit, target_commit)
        package_sha = str(verdict["package_sha256"])
        if package_sha in hashes:
            raise WorkflowError("Integration source packages must be distinct")
        hashes.add(package_sha)
        sources.append(
            {
                "package": str(package.resolve()),
                "package_sha256": package_sha,
                "key_id": verdict["key_id"],
                "actor_id": verdict.get("actor_id"),
                "source_commit": subject.get("source_commit"),
                "subject_kind": subject.get("kind"),
            }
        )
    integration_inspected = inspect_package(integration_package)
    integration_inspected_actor = integration_inspected.get("actor_id")
    if not isinstance(integration_inspected_actor, str):
        raise WorkflowError("Integration evidence has no actor identity")
    integration_actor_record = validate_actor_role(
        project,
        actor_id=integration_inspected_actor,
        allowed_roles={"ci", "release-manager"},
    )
    integration_verdict = verify_package(
        integration_package,
        trust_policy_path=trust_policy_path,
        policy_name=integration_policy,
        actor_roles=list(integration_actor_record["roles"]),
        approval_count=1,
    )
    integration = _passed_subject(
        integration_verdict.get("subject"), "Integration"
    )
    if integration.get("project_id") != project.project_id:
        raise WorkflowError("Integration evidence belongs to another project")
    integration_actor = integration_verdict.get("actor_id")
    if not isinstance(integration_actor, str):
        raise WorkflowError("Integration evidence has no actor identity")
    if integration_actor != integration_inspected_actor:
        raise WorkflowError("Integration actor identity changed")
    if (
        integration.get("kind") != "ci-result"
        or integration.get("purpose") != "integration"
    ):
        raise WorkflowError("A normal run cannot replace an integration CI run")
    if integration.get("source_commit") != target_commit:
        raise WorkflowError("Integration evidence is stale for the target commit")
    referenced = integration.get("source_evidence_sha256")
    if (
        not isinstance(referenced, list)
        or not all(isinstance(value, str) for value in referenced)
        or set(referenced) != hashes
        or len(referenced) != len(hashes)
    ):
        raise WorkflowError(
            "Integration evidence does not bind the exact source package set"
        )
    review_inspected = inspect_package(review_package)
    review_inspected_actor = review_inspected.get("actor_id")
    if not isinstance(review_inspected_actor, str):
        raise WorkflowError("Review evidence has no actor identity")
    reviewer = validate_actor_role(
        project,
        actor_id=review_inspected_actor,
        allowed_roles={"reviewer", "maintainer"},
    )
    review_verdict = verify_package(
        review_package,
        trust_policy_path=trust_policy_path,
        policy_name=review_policy,
        actor_roles=list(reviewer["roles"]),
        approval_count=0,
    )
    review_subject = review_verdict.get("subject")
    reviewer_id = review_verdict.get("actor_id")
    review_sources = (
        review_subject.get("source_evidence_sha256")
        if isinstance(review_subject, dict)
        else None
    )
    if (
        not isinstance(review_subject, dict)
        or review_subject.get("kind") != "review-approval"
        or review_subject.get("decision") != "approved"
        or review_subject.get("project_id") != project.project_id
        or review_subject.get("target_commit") != target_commit
        or not isinstance(review_sources, list)
        or not all(isinstance(value, str) for value in review_sources)
        or set(review_sources) != hashes
        or len(review_sources) != len(hashes)
        or review_subject.get("integration_evidence_sha256")
        != integration_verdict["package_sha256"]
        or not isinstance(reviewer_id, str)
    ):
        raise WorkflowError(
            "Review approval does not bind the exact integration candidate"
        )
    if reviewer_id != review_inspected_actor:
        raise WorkflowError("Review actor identity changed")
    if reviewer_id in contributor_ids or reviewer_id == integration_actor:
        raise WorkflowError(
            "Reviewer cannot approve evidence they contributed or attested"
        )
    review_context = evaluate_policy_context(
        policy=policy,
        policy_name=review_policy,
        actor_roles=list(reviewer["roles"]),
        approval_count=0,
    )
    integration_context = evaluate_policy_context(
        policy=policy,
        policy_name=integration_policy,
        actor_roles=list(integration_actor_record["roles"]),
        approval_count=1,
    )
    review = {
        "ok": True,
        "reviewer_id": reviewer_id,
        "reviewer_roles": reviewer["roles"],
        "review_package_sha256": review_verdict["package_sha256"],
        "independent": True,
        "policy_context": review_context,
    }
    result = {
        "schema_version": 1,
        "kind": "aria-integration-verdict",
        "ok": True,
        "project_id": project.project_id,
        "target_commit": target_commit,
        "source_policy": source_policy,
        "integration_policy": integration_policy,
        "review_policy": review_policy,
        "source_packages": sources,
        "integration_package": {
            "package": str(integration_package.resolve()),
            "package_sha256": integration_verdict["package_sha256"],
            "key_id": integration_verdict["key_id"],
            "actor_id": integration_verdict.get("actor_id"),
            "source_commit": integration.get("source_commit"),
        },
        "integration_policy_context": integration_context,
        "independent_review": review,
    }
    atomic_write_json(output_path, result)
    return {"verdict": str(output_path.resolve()), **result}
