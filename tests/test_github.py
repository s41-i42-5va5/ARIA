from __future__ import annotations

import io
import json
import unittest
import urllib.error

from aria.errors import ConfigurationError, ProviderCapabilityError
from aria.github import (
    GITHUB_API_VERSION,
    GitHubApiError,
    GitHubHttpClient,
    GitHubProviderAdapter,
    GitHubRepository,
    parse_github_remote,
)


class _FakeTransport:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.paths: list[str] = []

    def get_json(self, path: str) -> object:
        self.paths.append(path)
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value


class _RulesetMutationTransport(_FakeTransport):
    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__(responses)
        self.posts: list[tuple[str, object]] = []

    def post_json(self, path: str, payload: object) -> object:
        self.posts.append((path, payload))
        repository_path = "/repos/acme/product"
        self.responses[f"{repository_path}/rules/branches/aria-control?per_page=100"] = [
            {"type": "creation", "ruleset_id": 11},
            {"type": "update", "ruleset_id": 11},
        ]
        self.responses[f"{repository_path}/rulesets/11?includes_parents=true"] = {
            "id": 11,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [
                {
                    "actor_id": 9001,
                    "actor_type": "Integration",
                    "bypass_mode": "always",
                }
            ],
            "rules": [{"type": "creation"}, {"type": "update"}],
        }
        return {"id": 11}


class _RulesetCreationForbiddenTransport(_FakeTransport):
    def post_json(self, path: str, payload: object) -> object:
        raise GitHubApiError("forbidden", status=403)


def _responses(*, bypass: list[dict[str, object]] | None = None) -> dict[str, object]:
    repository_path = "/repos/acme/product"
    return {
        "/user": {"id": 42, "login": "aram", "name": "Aram"},
        repository_path: {
            "id": 123456789,
            "full_name": "acme/product",
        },
        f"{repository_path}/collaborators/aram/permission": {
            "permission": "admin",
            "role_name": "admin",
            "user": {"id": 42, "login": "aram"},
        },
        f"{repository_path}/rules/branches/aria-control?per_page=100": [
            {"type": "creation", "ruleset_id": 7},
            {"type": "update", "ruleset_id": 7},
        ],
        f"{repository_path}/rulesets/7?includes_parents=true": {
            "id": 7,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": bypass
            if bypass is not None
            else [
                {
                    "actor_id": 9001,
                    "actor_type": "Integration",
                    "bypass_mode": "always",
                }
            ],
            "rules": [{"type": "creation"}, {"type": "update"}],
        },
    }


class GitHubProviderAdapterTests(unittest.TestCase):
    def test_remote_locator_supports_github_https_and_ssh(self) -> None:
        expected = GitHubRepository(owner="acme", name="product")
        self.assertEqual(
            parse_github_remote("https://github.com/acme/product.git"),
            expected,
        )
        self.assertEqual(
            parse_github_remote("git@github.com:acme/product.git"),
            expected,
        )
        self.assertEqual(
            parse_github_remote("ssh://git@github.com/acme/product.git"),
            expected,
        )
        with self.assertRaisesRegex(ConfigurationError, "github.com"):
            parse_github_remote("https://example.com/acme/product.git")
        with self.assertRaisesRegex(ConfigurationError, "embedded credentials"):
            parse_github_remote("https://token@github.com/acme/product.git")

    def test_adapter_reads_immutable_identity_membership_and_strong_ruleset(self) -> None:
        transport = _FakeTransport(_responses())
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository(owner="acme", name="product"),
            transport=transport,
            coordinator_integration_id=9001,
        )

        inspection = adapter.inspect_collaboration(
            repository_id="123456789",
            control_branch="aria-control",
        )

        self.assertEqual(inspection.actor.user_id, "42")
        self.assertEqual(inspection.actor.username_snapshot, "aram")
        self.assertEqual(inspection.membership.roles, ("admin",))
        self.assertTrue(inspection.protection.coordinator_only)
        self.assertEqual(
            transport.paths,
            [
                "/user",
                "/repos/acme/product",
                "/repos/acme/product/collaborators/aram/permission",
                "/repos/acme/product/rules/branches/aria-control?per_page=100",
                "/repos/acme/product/rulesets/7?includes_parents=true",
            ],
        )

    def test_extra_bypass_actor_and_missing_integration_id_fail_unprotected(self) -> None:
        bypass = [
            {
                "actor_id": 9001,
                "actor_type": "Integration",
                "bypass_mode": "always",
            },
            {"actor_id": 42, "actor_type": "User", "bypass_mode": "always"},
        ]
        for integration_id in (9001, None):
            with self.subTest(integration_id=integration_id):
                adapter = GitHubProviderAdapter(
                    repository=GitHubRepository(owner="acme", name="product"),
                    transport=_FakeTransport(_responses(bypass=bypass)),
                    coordinator_integration_id=integration_id,
                )
                inspection = adapter.inspect_collaboration(
                    repository_id="123456789",
                    control_branch="aria-control",
                )
                self.assertFalse(inspection.protection.protected)
                self.assertTrue(inspection.protection.direct_user_push)

    def test_ruleset_forbidden_is_reported_as_required_provider_capability(self) -> None:
        responses = _responses()
        responses[
            "/repos/acme/product/rules/branches/aria-control?per_page=100"
        ] = GitHubApiError("forbidden", status=403)
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository(owner="acme", name="product"),
            transport=_FakeTransport(responses),
            coordinator_integration_id=9001,
        )

        with self.assertRaisesRegex(
            ProviderCapabilityError,
            "requires repository rulesets to be enforced",
        ):
            adapter.inspect_collaboration(
                repository_id="123456789",
                control_branch="aria-control",
            )

    def test_repository_or_membership_identity_mismatch_fails_closed(self) -> None:
        responses = _responses()
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository(owner="acme", name="product"),
            transport=_FakeTransport(responses),
            coordinator_integration_id=9001,
        )
        with self.assertRaisesRegex(ConfigurationError, "repository_id"):
            adapter.inspect_collaboration(
                repository_id="999",
                control_branch="aria-control",
            )

        responses = _responses()
        permission = responses[
            "/repos/acme/product/collaborators/aram/permission"
        ]
        assert isinstance(permission, dict)
        permission["user"] = {"id": 77, "login": "someone-else"}
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository(owner="acme", name="product"),
            transport=_FakeTransport(responses),
            coordinator_integration_id=9001,
        )
        with self.assertRaisesRegex(ConfigurationError, "another user id"):
            adapter.inspect_collaboration(
                repository_id="123456789",
                control_branch="aria-control",
            )

    def test_revoked_membership_is_inactive_without_forging_roles(self) -> None:
        responses = _responses()
        responses["/repos/acme/product/collaborators/aram/permission"] = GitHubApiError(
            "not found",
            status=404,
        )
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository(owner="acme", name="product"),
            transport=_FakeTransport(responses),
            coordinator_integration_id=9001,
        )
        inspection = adapter.inspect_collaboration(
            repository_id="123456789",
            control_branch="aria-control",
        )
        self.assertFalse(inspection.membership.active)
        self.assertEqual(inspection.membership.roles, ())

    def test_collaborator_listing_uses_immutable_ids_and_effective_roles(self) -> None:
        responses = _responses()
        responses[
            "/repos/acme/product/collaborators?affiliation=all&per_page=100&page=1"
        ] = [
            {
                "id": 42,
                "login": "aram",
                "name": "Aram",
                "role_name": "admin",
                "permissions": {"pull": True, "push": True, "admin": True},
            },
            {
                "id": 77,
                "login": "yura",
                "role_name": "write",
                "permissions": {"pull": True, "push": True, "admin": False},
            },
        ]
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository(owner="acme", name="product"),
            transport=_FakeTransport(responses),
            coordinator_integration_id=9001,
        )
        members = adapter.list_collaborators(repository_id="123456789")
        self.assertEqual([member.actor.user_id for member in members], ["42", "77"])
        self.assertEqual(members[0].membership.roles, ("admin",))
        self.assertEqual(members[1].membership.roles, ("contributor",))

    def test_collaborator_listing_rejects_repository_and_duplicate_identity(self) -> None:
        responses = _responses()
        page = "/repos/acme/product/collaborators?affiliation=all&per_page=100&page=1"
        responses[page] = []
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository(owner="acme", name="product"),
            transport=_FakeTransport(responses),
            coordinator_integration_id=9001,
        )
        with self.assertRaisesRegex(ConfigurationError, "repository_id"):
            adapter.list_collaborators(repository_id="999")

        responses = _responses()
        collaborator = {
            "id": 42,
            "login": "aram",
            "role_name": "admin",
            "permissions": {"admin": True},
        }
        responses[page] = [collaborator, dict(collaborator)]
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository(owner="acme", name="product"),
            transport=_FakeTransport(responses),
            coordinator_integration_id=9001,
        )
        with self.assertRaisesRegex(GitHubApiError, "duplicate user id"):
            adapter.list_collaborators(repository_id="123456789")

    def test_creates_and_reads_back_coordinator_only_ruleset(self) -> None:
        responses = _responses()
        repository_path = "/repos/acme/product"
        responses[f"{repository_path}/rules/branches/aria-control?per_page=100"] = []
        transport = _RulesetMutationTransport(responses)
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository("acme", "product"),
            transport=transport,
            coordinator_integration_id=9001,
        )
        result = adapter.ensure_control_protection(
            repository_id="123456789", control_branch="aria-control"
        )
        self.assertTrue(result["created"])
        path, payload = transport.posts[0]
        self.assertEqual(path, f"{repository_path}/rulesets")
        self.assertEqual(
            payload["conditions"]["ref_name"]["include"],
            ["refs/heads/aria-control"],
        )
        self.assertEqual(payload["bypass_actors"][0]["actor_id"], 9001)

    def test_ruleset_creation_forbidden_is_provider_capability_blocker(self) -> None:
        responses = _responses()
        repository_path = "/repos/acme/product"
        responses[f"{repository_path}/rules/branches/aria-control?per_page=100"] = []
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository("acme", "product"),
            transport=_RulesetCreationForbiddenTransport(responses),
            coordinator_integration_id=9001,
        )

        with self.assertRaisesRegex(
            ProviderCapabilityError,
            "requires repository rulesets to be enforced",
        ):
            adapter.ensure_control_protection(
                repository_id="123456789",
                control_branch="aria-control",
            )

    def test_conflicting_ruleset_is_not_silently_overridden(self) -> None:
        transport = _RulesetMutationTransport(_responses(bypass=[]))
        adapter = GitHubProviderAdapter(
            repository=GitHubRepository("acme", "product"),
            transport=transport,
            coordinator_integration_id=9001,
        )
        with self.assertRaisesRegex(Exception, "conflict"):
            adapter.ensure_control_protection(
                repository_id="123456789", control_branch="aria-control"
            )
        self.assertEqual(transport.posts, [])


class _Response:
    def __init__(self, payload: object) -> None:
        self.status = 200
        self.headers = {"Content-Length": str(len(json.dumps(payload)))}
        self._content = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int) -> bytes:
        return self._content[:amount]


class _Opener:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[object] = []

    def open(self, request: object, *, timeout: float) -> _Response:
        self.requests.append(request)
        if self.fail:
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {"X-GitHub-Request-Id": "request-7"},
                io.BytesIO(b'{"message":"secret-token"}'),
            )
        return _Response({"id": 42})


class GitHubHttpClientTests(unittest.TestCase):
    def test_client_sets_versioned_headers_and_never_returns_token(self) -> None:
        opener = _Opener()
        client = GitHubHttpClient(
            token_source=lambda: "secret-token",
            opener=opener,
        )
        self.assertEqual(client.get_json("/user"), {"id": 42})
        request = opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(
            request.get_header("X-github-api-version"),
            GITHUB_API_VERSION,
        )

        failing = GitHubHttpClient(
            token_source=lambda: "secret-token",
            opener=_Opener(fail=True),
        )
        with self.assertRaises(GitHubApiError) as caught:
            failing.get_json("/user")
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertIn("request-7", str(caught.exception))

    def test_mutations_use_json_body_and_same_safe_session_boundary(self) -> None:
        opener = _Opener()
        client = GitHubHttpClient(
            token_source=lambda: "ghs-installation-token",
            opener=opener,
        )
        client.post_json("/repos/acme/product/git/trees", {"tree": []})
        client.patch_json(
            "/repos/acme/product/git/refs/heads/aria-control",
            {"sha": "a" * 40, "force": False},
        )
        post, patch = opener.requests
        self.assertEqual(post.method, "POST")
        self.assertEqual(json.loads(post.data), {"tree": []})
        self.assertEqual(patch.method, "PATCH")
        self.assertEqual(
            json.loads(patch.data), {"sha": "a" * 40, "force": False}
        )


if __name__ == "__main__":
    unittest.main()
