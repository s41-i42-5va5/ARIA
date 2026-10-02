from __future__ import annotations

import unittest

from aria.github import GitHubApiError, GitHubRepository
from aria.github_team import GitHubCollaboratorManager


class _Transport:
    def __init__(self) -> None:
        self.revoked = False

    def get_json(self, path: str):
        if path == "/users/yura":
            return {"id": 200, "login": "yura", "name": "Yura"}
        if path.endswith("/invitations?per_page=100&page=1"):
            return [{
                "id": 77,
                "invitee": {"id": 200, "login": "yura"},
                "permissions": "write",
            }]
        if path.endswith("/collaborators/yura/permission"):
            if self.revoked:
                raise GitHubApiError("missing", status=404)
            return {"permission": "write", "user": {"id": 200, "login": "yura"}}
        raise AssertionError(path)

    def put_json(self, path: str, payload: object):
        self.last_put = (path, payload)
        return {"id": 77, "invitee": {"id": 200, "login": "yura"}}

    def delete_json(self, path: str):
        self.last_delete = path
        self.revoked = True
        return {}


class GitHubCollaboratorManagerTests(unittest.TestCase):
    def test_invite_and_revoke_require_provider_readback(self) -> None:
        transport = _Transport()
        manager = GitHubCollaboratorManager(
            transport=transport, repository=GitHubRepository("acme", "product")
        )
        receipt = manager.invite(username="yura")
        self.assertEqual(receipt.state, "invited")
        self.assertEqual(receipt.actor.user_id, "200")
        self.assertEqual(receipt.invitation_id, "77")
        recovered = manager.read_invitation(username="yura", permission="push")
        self.assertEqual(recovered.permission, "push")
        revoked = manager.revoke(username="yura")
        self.assertEqual(revoked.user_id, "200")


if __name__ == "__main__":
    unittest.main()
