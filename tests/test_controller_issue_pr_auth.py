from datetime import datetime, timezone
from unittest.mock import Mock, patch

import httpx

from auto_coder.github_controller_auth import ControllerCredential, ControllerIdentity
from auto_coder.pr_processor import _is_dependabot_pr, _is_local_llm_pr
from auto_coder.util.gh_cache import GitHubClient


class RenewableProvider:
    def __init__(self) -> None:
        self.tokens = ["installation-one", "installation-two"]
        self.calls = 0

    def credential_for(self, repo_name, required_permissions):
        token = self.tokens[min(self.calls, 1)]
        self.calls += 1
        return ControllerCredential(
            token,
            "app",
            repository=repo_name,
            permissions=tuple(sorted(required_permissions.items())),
            expires_at=datetime.now(timezone.utc),
            app_id=7,
            installation_id=11,
            snapshot_id=str(self.calls),
        )

    def identity_for(self, repo_name):
        return ControllerIdentity("auto-coder[bot]", app_id=7)

    def invalidate(self, credential):
        return None


def test_held_api_resolves_renewed_installation_credential_for_each_send():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    transport.request.side_effect = [
        httpx.Response(200, json={"number": 1}, request=httpx.Request("GET", "https://api.github.com/one")),
        httpx.Response(200, json={"number": 2}, request=httpx.Request("GET", "https://api.github.com/two")),
    ]

    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        api = client.api_for("owner/repo", required_permissions={"pull_requests": "read"})
        assert api("/repos/owner/repo/pulls/1", verb="GET")["number"] == 1
        assert api("/repos/owner/repo/pulls/2", verb="GET")["number"] == 2

    assert transport.request.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer installation-one"
    assert transport.request.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer installation-two"
    assert provider.calls == 2


def test_controller_bot_local_origin_is_not_dependency_bot():
    pr = {
        "user": {"login": "auto-coder[bot]"},
        "body": "<!-- auto-coder:local-llm -->\n\nCloses #2876",
        "head": {"ref": "issue-2876-attempt-0-codex-cloud"},
    }
    assert _is_dependabot_pr(pr) is False
    assert _is_local_llm_pr(pr) is True


def test_unrelated_bot_behavior_is_unchanged():
    assert _is_dependabot_pr({"user": {"login": "renovate[bot]"}, "body": "Bump requests"}) is True
    assert _is_dependabot_pr({"user": {"login": "other[bot]"}, "body": "Generated update"}) is True
