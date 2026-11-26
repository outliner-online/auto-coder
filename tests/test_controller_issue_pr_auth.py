from datetime import datetime, timezone
from unittest.mock import Mock, patch

import httpx

from auto_coder.automation_config import AutomationConfig
from auto_coder.github_controller_auth import ControllerCredential, ControllerIdentity
from auto_coder.pr_processor import _is_dependabot_pr, _is_local_llm_pr, evaluate_dependency_bot_admission
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
    identity = ControllerIdentity("auto-coder[bot]")
    assert _is_dependabot_pr(pr, identity) is False
    assert _is_local_llm_pr(pr, identity) is True


def test_unrelated_bot_behavior_is_unchanged():
    assert _is_dependabot_pr({"user": {"login": "renovate[bot]"}, "body": "Bump requests"}) is True
    assert _is_dependabot_pr({"user": {"login": "other[bot]"}, "body": "Generated update"}) is True


def test_issue_comments_use_controller_credentials_at_each_page():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/1/comments")
    transport.request.side_effect = [
        httpx.Response(200, json=[{"id": number} for number in range(100)], request=request),
        httpx.Response(200, json=[{"id": 100}], request=request),
    ]

    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        comments = client.get_issue_comments("owner/repo", 1)

    assert len(comments) == 101
    assert [call.kwargs["headers"]["Authorization"] for call in transport.request.call_args_list] == [
        "Bearer installation-one",
        "Bearer installation-two",
    ]


def test_same_local_marker_only_exempts_proven_controller_actor():
    identity = ControllerIdentity("auto-coder[bot]", account_id=101, app_id=7)
    body = "<!-- auto-coder:local-llm -->\n\nCloses #2876"
    controller = {"user": {"login": "auto-coder[bot]", "id": 101}, "body": body}
    unrelated = {"user": {"login": "other[bot]", "id": 202}, "body": body}

    assert _is_dependabot_pr(controller, identity) is False
    assert _is_local_llm_pr(controller, identity) is True
    assert _is_dependabot_pr(unrelated, identity) is True
    assert _is_local_llm_pr(unrelated, identity) is False


def test_strict_head_lookup_preserves_no_cache_header_on_repository_adapter():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    api = Mock()
    api.pulls.list.return_value = []

    with patch.object(client, "api_for", return_value=api) as api_for:
        assert client.find_pr_by_head_branch_strict("owner/repo", "issue-2876") is None

    api_for.assert_called_once_with(
        "owner/repo",
        required_permissions={"pull_requests": "read"},
        extra_headers={"Cache-Control": "no-cache"},
    )


def test_linked_issue_cleanup_uses_injected_scoped_read_and_write_apis():
    from auto_coder.pr_processor import _close_linked_issues

    client = Mock()
    read_api = Mock()
    write_api = Mock()
    read_api.pulls.get.return_value = {"body": "Closes #42"}
    client.api_for.side_effect = [read_api, write_api]

    _close_linked_issues("owner/repo", 9, client)

    assert client.api_for.call_args_list == [
        __import__("unittest.mock").mock.call("owner/repo", required_permissions={"pull_requests": "read"}),
        __import__("unittest.mock").mock.call("owner/repo", required_permissions={"issues": "write"}),
    ]
    read_api.pulls.get.assert_called_once_with("owner", "repo", 9)
    write_api.issues.create_comment.assert_called_once_with("owner", "repo", 42, body="Closed by PR #9")
    write_api.issues.update.assert_called_once_with("owner", "repo", 42, state="closed")


def test_confirmed_merge_finalizer_keeps_success_when_scoped_cleanup_fails():
    from auto_coder.pr_processor import _finalize_merge_success

    client = Mock()
    client.api_for.side_effect = RuntimeError("installation unavailable")
    with patch("auto_coder.pr_processor._archive_jules_session"):
        assert _finalize_merge_success("owner/repo", 9, "--squash", client) is True


def test_common_admission_distinguishes_same_body_controller_and_unrelated_bot():
    client = Mock()
    identity = ControllerIdentity("auto-coder[bot]", account_id=101, app_id=7)
    client.get_authenticated_controller_identity.return_value = identity
    config = AutomationConfig()
    config.IGNORE_DEPENDABOT_PRS = True
    body = "<!-- auto-coder:local-llm -->\n\nCloses #2876"

    controller = {"number": 1, "user": {"login": "auto-coder[bot]", "id": 101}, "body": body}
    unrelated = {"number": 2, "user": {"login": "other[bot]", "id": 202}, "body": body}

    assert evaluate_dependency_bot_admission(client, "owner/repo", controller, config).allowed is True
    assert evaluate_dependency_bot_admission(client, "owner/repo", unrelated, config).allowed is False


def test_issue_comment_uses_repository_scoped_controller_write_credential():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("POST", "https://api.github.com/repos/owner/repo/issues/42/comments")
    transport.request.return_value = httpx.Response(
        201,
        json={"id": 7, "user": {"login": "auto-coder[bot]", "id": 101}},
        request=request,
    )

    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        client.add_comment_to_issue("owner/repo", 42, "provider link")

    sent = transport.request.call_args
    assert sent.kwargs["method"].upper() == "POST"
    assert sent.kwargs["url"] == "https://api.github.com/repos/owner/repo/issues/42/comments"
    assert sent.kwargs["headers"]["Authorization"] == "Bearer installation-one"
    assert sent.kwargs["json"] == {"body": "provider link"}


def test_controller_branch_origin_without_body_marker_keeps_local_classification():
    identity = ControllerIdentity("auto-coder[bot]", account_id=101, app_id=7)
    pr = {
        "user": {"login": "auto-coder[bot]", "id": 101},
        "body": "",
        "head": {"ref": "issue-123"},
    }

    assert _is_dependabot_pr(pr, identity) is False
    assert _is_local_llm_pr(pr, identity) is True


def test_issue_management_mutations_select_repository_installation_capabilities():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    api = Mock()
    api.issues.create.return_value.number = 7

    with patch.object(client, "api_for", return_value=api) as api_for:
        client.create_issue("owner/repo", "Title", "Body")
        client.close_issue("owner/repo", 7, "done")
        client.reopen_issue("owner/repo", 7)
        client.close_pr("owner/repo", 8, "done")
        client.add_comment_to_pr("owner/repo", 8, "status")
        client.add_labels("owner/repo", 7, ["managed"])
        client.remove_labels("owner/repo", 7, ["managed"])

    requested = [call.kwargs["required_permissions"] for call in api_for.call_args_list]
    assert requested == [
        {"issues": "write"},
        {"issues": "write"},
        {"issues": "write"},
        {"pull_requests": "write", "issues": "write"},
        {"issues": "write"},
        {"issues": "write"},
        {"issues": "write"},
    ]
