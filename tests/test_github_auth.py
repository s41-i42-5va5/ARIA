from __future__ import annotations

import unittest

from aria.github_auth import (
    GITHUB_DEVICE_CODE_URL,
    GITHUB_TOKEN_URL,
    GitHubAuthError,
    complete_device_authorization,
    refresh_user_token,
    request_device_authorization,
)


class _Transport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post_form(self, url: str, fields: dict[str, str]) -> dict[str, object]:
        self.calls.append((url, fields))
        return self.responses.pop(0)


def _device() -> dict[str, object]:
    return {
        "device_code": "device-secret-code",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "interval": 5,
    }


def _token() -> dict[str, object]:
    return {
        "access_token": "ghu_access-secret",
        "refresh_token": "ghr_refresh-secret",
        "token_type": "bearer",
        "expires_in": 28800,
        "refresh_token_expires_in": 15897600,
    }


class GitHubDeviceAuthTests(unittest.TestCase):
    def test_device_request_is_repository_bound_and_hides_device_secret(self) -> None:
        transport = _Transport([_device()])
        authorization = request_device_authorization(
            transport,
            client_id="Iv1.client123",
            repository_id="123456789",
        )
        self.assertEqual(authorization.user_code, "ABCD-EFGH")
        self.assertNotIn("device-secret-code", repr(authorization))
        self.assertEqual(transport.calls[0][0], GITHUB_DEVICE_CODE_URL)
        self.assertEqual(transport.calls[0][1]["repository_id"], "123456789")

    def test_poll_respects_pending_and_slow_down_then_returns_hidden_tokens(self) -> None:
        transport = _Transport(
            [
                _device(),
                {"error": "authorization_pending"},
                {"error": "slow_down"},
                _token(),
            ]
        )
        authorization = request_device_authorization(
            transport,
            client_id="Iv1.client123",
        )
        clock = 0.0
        sleeps: list[float] = []

        def monotonic() -> float:
            return clock

        def sleep(seconds: float) -> None:
            nonlocal clock
            sleeps.append(seconds)
            clock += seconds

        token = complete_device_authorization(
            transport,
            client_id="Iv1.client123",
            authorization=authorization,
            monotonic=monotonic,
            sleep=sleep,
        )
        self.assertEqual(sleeps, [5, 5, 10])
        self.assertNotIn("ghu_access-secret", repr(token))
        self.assertNotIn("ghr_refresh-secret", repr(token))
        self.assertTrue(all(call[0] == GITHUB_TOKEN_URL for call in transport.calls[1:]))

    def test_terminal_device_error_is_safe(self) -> None:
        transport = _Transport([_device(), {"error": "access_denied", "device_code": "secret"}])
        authorization = request_device_authorization(transport, client_id="Iv1.client123")
        with self.assertRaises(GitHubAuthError) as caught:
            complete_device_authorization(
                transport,
                client_id="Iv1.client123",
                authorization=authorization,
                monotonic=lambda: 0,
                sleep=lambda _: None,
            )
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("access_denied", str(caught.exception))

    def test_refresh_rotates_without_client_secret(self) -> None:
        transport = _Transport([_token()])
        token = refresh_user_token(
            transport,
            client_id="Iv1.client123",
            refresh_token="old-refresh-secret",
        )
        fields = transport.calls[0][1]
        self.assertEqual(fields["grant_type"], "refresh_token")
        self.assertNotIn("client_secret", fields)
        self.assertEqual(token.expires_in, 28800)

    def test_expired_device_code_stops_without_token_request(self) -> None:
        transport = _Transport([_device()])
        authorization = request_device_authorization(transport, client_id="Iv1.client123")
        authorization = authorization.__class__(
            device_code=authorization.device_code,
            user_code=authorization.user_code,
            verification_uri=authorization.verification_uri,
            expires_in=5,
            interval=5,
        )
        with self.assertRaisesRegex(GitHubAuthError, "expired"):
            complete_device_authorization(
                transport,
                client_id="Iv1.client123",
                authorization=authorization,
                monotonic=lambda: 0,
                sleep=lambda _: None,
            )
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
