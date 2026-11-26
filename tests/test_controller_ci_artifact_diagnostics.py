from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx

from auto_coder.github_controller_auth import ControllerCredential
from auto_coder.util.github_action import _get_playwright_artifact_logs


def _controller_client() -> MagicMock:
    client = MagicMock()
    client._credential_for.return_value = ControllerCredential("controller-secret", "app", repository="owner/repo")
    return client


def test_artifact_listing_failure_does_not_log_response_payload():
    marker = "private-response-payload"
    response = httpx.Response(403, text=marker)

    @contextmanager
    def controlled_http_client(**_kwargs):
        transport = MagicMock()
        transport.get.return_value = response
        yield transport

    logger = MagicMock()
    with (
        patch("auto_coder.util.github_action.GitHubClient.get_instance", return_value=_controller_client()),
        patch("auto_coder.util.github_action.github_http_client", controlled_http_client),
        patch("auto_coder.util.github_action.logger", logger),
    ):
        assert _get_playwright_artifact_logs("owner/repo", 7) == (None, None)

    diagnostics = " ".join(str(call) for call in logger.method_calls)
    assert marker not in diagnostics
    assert "status=403" in diagnostics


def test_foreign_artifact_exception_does_not_log_signed_url():
    marker = "signed-secret-marker"
    listing = httpx.Response(200, json={"artifacts": [{"id": 9, "name": "e2e-artifacts-basic", "expired": False}]})
    redirect = httpx.Response(302, headers={"location": f"https://downloads.example/archive?signature={marker}"})

    @contextmanager
    def controlled_http_client(**kwargs):
        transport = MagicMock()
        if kwargs.get("follow_redirects") is False:
            transport.get.side_effect = [redirect, httpx.ConnectError(f"failed {marker}")]
        else:
            transport.get.return_value = listing
        yield transport

    logger = MagicMock()
    with (
        patch("auto_coder.util.github_action.GitHubClient.get_instance", return_value=_controller_client()),
        patch("auto_coder.util.github_action.github_http_client", controlled_http_client),
        patch("auto_coder.util.github_action.logger", logger),
    ):
        assert _get_playwright_artifact_logs("owner/repo", 7) == (None, None)

    diagnostics = " ".join(str(call) for call in logger.method_calls)
    assert marker not in diagnostics
    assert "category=ConnectError" in diagnostics
