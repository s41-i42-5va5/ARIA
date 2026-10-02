from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from aria.collaboration import (
    build_control_contract,
    collaboration_plan,
    dump_control_contract,
    enable_collaboration,
    load_control_contract,
    parse_control_contract,
)
from aria.collaborative_documents import build_initial_collaborative_documents
from aria.errors import ConfigurationError, ProviderCapabilityError, WorkflowError
from aria.provider import (
    ProviderActor,
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


class _FakeProviderAdapter:
    provider_id = "github"

    def __init__(self, inspection: ProviderInspection) -> None:
        self.inspection = inspection

    def inspect_collaboration(
        self,
        *,
        repository_id: str,
        control_branch: str,
    ) -> ProviderInspection:
        return self.inspection


class _CapabilityUnavailableAdapter:
    provider_id = "github"

    def inspect_collaboration(
        self,
        *,
        repository_id: str,
        control_branch: str,
    ) -> ProviderInspection:
        raise ProviderCapabilityError("GitHub rulesets are not enforced")


class _HeadWriter:
    def __init__(self, head: str) -> None:
        self.head = head

    def read_head(self) -> str:
        return self.head


class _ConfigurableProviderAdapter(_FakeProviderAdapter):
    def __init__(self, inspection: ProviderInspection) -> None:
        super().__init__(inspection)
        self.ensure_calls = 0

    def ensure_control_protection(self, *, repository_id: str, control_branch: str):
        self.ensure_calls += 1
        self.inspection = ProviderInspection(
            provider=self.inspection.provider,
            repository_id=self.inspection.repository_id,
            actor=self.inspection.actor,
            membership=self.inspection.membership,
            protection=ProviderBranchProtection(True, False, "aria-coordinator"),
        )
        return {"created": True}


class ControlContractTests(unittest.TestCase):
    def test_contract_has_deterministic_round_trip(self) -> None:
        contract = build_control_contract(
            project_id="demo",
            provider="github",
            repository_id="123456789",
        )
        first = dump_control_contract(contract)
        second = dump_control_contract(parse_control_contract(yaml.safe_load(first)))
        self.assertEqual(first, second)
        self.assertIn("canonical_writer: coordinator", first)
        self.assertIn("direct_control_push: false", first)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "CONTROL.yaml"
            path.write_text(first, encoding="utf-8")
            self.assertEqual(load_control_contract(path), contract)

    def test_mixed_or_weakened_mode_fails_closed(self) -> None:
        raw = build_control_contract(
            project_id="demo",
            provider="gitlab",
            repository_id="gid://gitlab/Project/42",
        ).as_mapping()
        raw["mode"] = "offline"
        with self.assertRaisesRegex(ConfigurationError, "must be 'collaborative'"):
            parse_control_contract(raw)

        raw = build_control_contract(
            project_id="demo",
            provider="gitlab",
            repository_id="gid://gitlab/Project/42",
        ).as_mapping()
        authority = raw["authority"]
        assert isinstance(authority, dict)
        authority["direct_control_push"] = True
        with self.assertRaisesRegex(ConfigurationError, "fail-closed"):
            parse_control_contract(raw)

    def test_unknown_fields_and_bool_schema_versions_are_rejected(self) -> None:
        raw = build_control_contract(
            project_id="demo",
            provider="github",
            repository_id="42",
        ).as_mapping()
        raw["future_policy"] = "silently ignored"
        with self.assertRaisesRegex(ConfigurationError, "unexpected"):
            parse_control_contract(raw)

        raw = build_control_contract(
            project_id="demo",
            provider="github",
            repository_id="42",
        ).as_mapping()
        raw["schema_version"] = True
        with self.assertRaisesRegex(ConfigurationError, "schema_version"):
            parse_control_contract(raw)


class CollaborationPlanTests(unittest.TestCase):
    def _repository(self, parent: Path) -> Path:
        root = parent / "product"
        root.mkdir()
        _git(root, "init")
        _git(root, "config", "user.name", "ARIA Test")
        _git(root, "config", "user.email", "aria@example.invalid")
        (root / "README.md").write_text("demo\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-m", "initial")
        _git(root, "branch", "dev")
        _git(root, "remote", "add", "origin", str(parent / "remote.git"))
        return root

    def test_plan_is_read_only_and_reports_unprotected_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._repository(Path(temporary))
            _git(
                root,
                "remote",
                "set-url",
                "origin",
                "https://user:super-secret-token@example.invalid/org/product.git",
            )
            before_refs = _git(root, "for-each-ref", "--format=%(refname):%(objectname)")
            before_worktrees = _git(root, "worktree", "list", "--porcelain")
            before_status = _git(root, "status", "--porcelain=v1")

            plan = collaboration_plan(
                project_id="demo",
                code_root=root,
                provider="github",
                repository_id="123456789",
            )

            self.assertTrue(plan["ok"])
            self.assertTrue(plan["read_only"])
            self.assertFalse(plan["ready"])
            blocker_codes = {item["code"] for item in plan["blockers"]}
            self.assertEqual(
                blocker_codes,
                {"PROVIDER_ADAPTER_UNAVAILABLE", "UNPROTECTED"},
            )
            self.assertNotIn("super-secret-token", json.dumps(plan))
            self.assertEqual(
                plan["docs_root"], str(root.with_name("product-aria-control"))
            )
            control = plan["git"]["control_branch"]
            self.assertFalse(control["local"])
            self.assertFalse(control["remote_tracking"])
            self.assertEqual(
                before_refs,
                _git(root, "for-each-ref", "--format=%(refname):%(objectname)"),
            )
            self.assertEqual(
                before_worktrees, _git(root, "worktree", "list", "--porcelain")
            )
            self.assertEqual(before_status, _git(root, "status", "--porcelain=v1"))

    def test_plan_reports_required_provider_capability_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._repository(Path(temporary))

            plan = collaboration_plan(
                project_id="demo",
                code_root=root,
                provider="github",
                repository_id="123456789",
                provider_adapter=_CapabilityUnavailableAdapter(),
            )

            blockers = {
                item["code"]: item["message"] for item in plan["blockers"]
            }
            self.assertIn("PROVIDER_CAPABILITY_UNAVAILABLE", blockers)
            self.assertNotIn("PROVIDER_ADAPTER_UNAVAILABLE", blockers)
            self.assertEqual(
                plan["actions"][0]["reason"],
                "PROVIDER_CAPABILITY_UNAVAILABLE",
            )
            self.assertFalse(plan["ready"])

    def test_plan_is_deterministic_and_detects_local_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = self._repository(parent)
            docs_root = parent / "occupied"
            docs_root.mkdir()
            (docs_root / "foreign.txt").write_text("data", encoding="utf-8")
            (root / "untracked.txt").write_text("dirty", encoding="utf-8")

            first = collaboration_plan(
                project_id="demo",
                code_root=root,
                docs_root=docs_root,
                provider="github",
                repository_id="123456789",
            )
            second = collaboration_plan(
                project_id="demo",
                code_root=root,
                docs_root=docs_root,
                provider="github",
                repository_id="123456789",
            )

            self.assertEqual(first["plan_sha256"], second["plan_sha256"])
            blocker_codes = {item["code"] for item in first["blockers"]}
            self.assertEqual(
                blocker_codes,
                {
                    "CODE_WORKTREE_DIRTY",
                    "DOCS_ROOT_OCCUPIED",
                    "PROVIDER_ADAPTER_UNAVAILABLE",
                    "UNPROTECTED",
                },
            )

    def test_enable_requires_exact_plan_and_stops_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._repository(Path(temporary))
            plan = collaboration_plan(
                project_id="demo",
                code_root=root,
                provider="github",
                repository_id="123456789",
            )
            before_refs = _git(root, "for-each-ref", "--format=%(refname):%(objectname)")
            before_worktrees = _git(root, "worktree", "list", "--porcelain")

            with self.assertRaisesRegex(WorkflowError, "UNPROTECTED"):
                enable_collaboration(
                    project_id="demo",
                    code_root=root,
                    provider="github",
                    repository_id="123456789",
                    expected_plan_sha256=str(plan["plan_sha256"]),
                    confirm=True,
                )

            self.assertEqual(
                before_refs,
                _git(root, "for-each-ref", "--format=%(refname):%(objectname)"),
            )
            self.assertEqual(
                before_worktrees, _git(root, "worktree", "list", "--porcelain")
            )

            with self.assertRaisesRegex(WorkflowError, "stale"):
                enable_collaboration(
                    project_id="demo",
                    code_root=root,
                    provider="github",
                    repository_id="different-repository",
                    expected_plan_sha256=str(plan["plan_sha256"]),
                    confirm=True,
                )

    def test_verified_provider_readback_removes_identity_and_protection_blockers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._repository(Path(temporary))
            inspection = ProviderInspection(
                provider="github",
                repository_id="123456789",
                actor=ProviderActor(
                    user_id="provider-user-42",
                    username_snapshot="aram",
                    display_name_snapshot="Aram",
                ),
                membership=ProviderMembership(active=True, roles=("owner",)),
                protection=ProviderBranchProtection(
                    protected=True,
                    direct_user_push=False,
                    canonical_writer="aria-coordinator",
                ),
            )
            adapter = _FakeProviderAdapter(inspection)

            plan = collaboration_plan(
                project_id="demo",
                code_root=root,
                provider="github",
                repository_id="123456789",
                provider_adapter=adapter,
            )

            self.assertTrue(plan["ready"])
            self.assertEqual(plan["blockers"], [])
            actor = plan["provider_readback"]["actor"]
            self.assertEqual(actor["user_id"], "provider-user-42")
            self.assertEqual(actor["username_snapshot"], "aram")
            with self.assertRaisesRegex(WorkflowError, "Coordinator writer"):
                enable_collaboration(
                    project_id="demo",
                    code_root=root,
                    provider="github",
                    repository_id="123456789",
                    provider_adapter=adapter,
                    expected_plan_sha256=str(plan["plan_sha256"]),
                    confirm=True,
                )

    def test_ready_enable_delegates_exact_plan_to_recoverable_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._repository(Path(temporary))
            inspection = ProviderInspection(
                provider="github",
                repository_id="123456789",
                actor=ProviderActor("42", "aram", "Aram"),
                membership=ProviderMembership(True, ("admin",)),
                protection=ProviderBranchProtection(
                    True, False, "aria-coordinator"
                ),
            )
            adapter = _FakeProviderAdapter(inspection)
            plan = collaboration_plan(
                project_id="demo",
                code_root=root,
                provider="github",
                repository_id="123456789",
                provider_adapter=adapter,
            )
            writer = object()
            runtime = Path(temporary) / "runtime"
            with mock.patch(
                "aria.collaboration_apply.collaboration_apply_paths",
                return_value=SimpleNamespace(transaction=runtime / "missing.json"),
            ), mock.patch(
                "aria.collaboration_apply.apply_collaboration_transaction",
                return_value={"ok": True, "collaboration": "enabled"},
            ) as apply:
                result = enable_collaboration(
                    project_id="demo",
                    code_root=root,
                    provider="github",
                    repository_id="123456789",
                    provider_adapter=adapter,
                    control_writer=writer,
                    runtime_root=runtime,
                    expected_plan_sha256=str(plan["plan_sha256"]),
                    confirm=True,
                )
            self.assertEqual(result["collaboration"], "enabled")
            self.assertIs(apply.call_args.kwargs["writer"], writer)
            self.assertEqual(
                apply.call_args.kwargs["plan_sha256"], plan["plan_sha256"]
            )

    def test_enable_configures_and_reads_back_protection_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._repository(Path(temporary))
            adapter = _ConfigurableProviderAdapter(
                ProviderInspection(
                    provider="github",
                    repository_id="123456789",
                    actor=ProviderActor("42", "aram", "Aram"),
                    membership=ProviderMembership(True, ("admin",)),
                    protection=ProviderBranchProtection(False, True, None),
                )
            )
            plan = collaboration_plan(
                project_id="demo",
                code_root=root,
                provider="github",
                repository_id="123456789",
                provider_adapter=adapter,
            )
            self.assertTrue(plan["ready"], plan)
            self.assertEqual(plan["actions"][-1]["status"], "planned")
            runtime = Path(temporary) / "runtime"
            with mock.patch(
                "aria.collaboration_apply.apply_collaboration_transaction",
                return_value={"ok": True, "collaboration": "enabled"},
            ):
                result = enable_collaboration(
                    project_id="demo",
                    code_root=root,
                    provider="github",
                    repository_id="123456789",
                    provider_adapter=adapter,
                    control_writer=object(),
                    runtime_root=runtime,
                    expected_plan_sha256=str(plan["plan_sha256"]),
                    confirm=True,
                )
            self.assertEqual(result["collaboration"], "enabled")
            self.assertEqual(adapter.ensure_calls, 1)

    def test_repeated_enable_returns_verified_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = root / "code"
            docs = root / "control"
            runtime = root / "runtime"
            code.mkdir()
            docs.mkdir()
            contract = build_control_contract(
                project_id="demo",
                provider="github",
                repository_id="123456789",
            )
            document_set = build_initial_collaborative_documents(
                contract, display_name="code"
            )
            for name, content in document_set.documents.items():
                (docs / name).write_text(content, encoding="utf-8")
            current = {
                "plan_sha256": "a" * 64,
                "docs_root": str(docs),
                "blockers": [],
                "provider_readback": {
                    "protection": {"coordinator_only": True}
                },
                "git": {"control_branch": {"worktree": str(docs)}},
            }
            registry = {
                "schema_version": 1,
                "projects": {
                    "demo": {
                        "docs_root": docs.as_posix(),
                        "code_root": code.as_posix(),
                        "mode": "shadow",
                    }
                },
            }
            with mock.patch(
                "aria.collaboration.collaboration_plan", return_value=current
            ), mock.patch(
                "aria.collaboration._git_value", return_value="c" * 40
            ), mock.patch(
                "aria.collaboration.read_registry", return_value=registry
            ):
                result = enable_collaboration(
                    project_id="demo",
                    code_root=code,
                    docs_root=docs,
                    provider="github",
                    repository_id="123456789",
                    expected_plan_sha256="a" * 64,
                    confirm=True,
                    provider_adapter=object(),
                    control_writer=_HeadWriter("c" * 40),
                    runtime_root=runtime,
                )
            self.assertTrue(result["already_enabled"])
            self.assertEqual(result["control_commit"], "c" * 40)


if __name__ == "__main__":
    unittest.main()
