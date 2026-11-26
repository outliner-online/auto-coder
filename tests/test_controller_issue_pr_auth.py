from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from auto_coder.automation_config import AutomationConfig
from auto_coder.execution_trace import get_trace_collector
from auto_coder.github_controller_auth import ControllerCredential, ControllerIdentity
from auto_coder.issue_processor import _create_pr_for_issue
from auto_coder.pr_processor import (
    _is_dependabot_pr,
    _is_empty_pr,
    _is_local_llm_pr,
    _start_mergeability_remediation,
    evaluate_dependency_bot_admission,
)
from auto_coder.pr_repair_dispatch import PRRepairResult, RepairDisposition
from auto_coder.util.gh_cache import GitHubClient


def test_strict_pr_readers_use_current_installation_at_each_send():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    payload = {"number": 7, "state": "open", "body": "", "head": {"sha": "abc", "ref": "branch", "repo": {"full_name": "owner/repo"}}, "base": {"ref": "main"}}
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/7")
    with patch("auto_coder.util.gh_cache._strict_request", side_effect=lambda *a, **kw: httpx.Response(200, json=payload, request=request)) as send:
        assert client.get_pull_request_metadata_strict("owner/repo", 7) == payload
        assert client.get_pull_request_head_sha_strict("owner/repo", 7) == "abc"
        assert client.get_pull_request_repair_metadata_strict("owner/repo", 7).head_sha == "abc"
        assert client.get_pull_request_routing_metadata_strict("owner/repo", 7).head_sha == "abc"
    assert [call.kwargs["headers"]["Authorization"] for call in send.call_args_list] == ["Bearer installation-one", "Bearer installation-two", "Bearer installation-two", "Bearer installation-two"]
    assert provider.requests == [("owner/repo", {"pull_requests": "read"})] * 4


def test_provider_link_projection_uses_installation_for_read_and_update():
    from auto_coder.codex_pr_attribution import AttributionDisposition
    from auto_coder.pr_processor import _link_codex_cloud_pr_to_issue

    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    origin = SimpleNamespace(repository="owner/repo", pr_number=7, provider="codex-cloud", task_id="task-123")
    attribution = SimpleNamespace(disposition=AttributionDisposition.VERIFIED, origin=origin)
    bindings = Mock()
    bindings.get.return_value = attribution
    transport = Mock()
    request = httpx.Request("PATCH", "https://api.github.com/repos/owner/repo/pulls/7")
    transport.request.return_value = httpx.Response(200, json={"number": 7}, request=request)
    read_request = httpx.Request("GET", str(request.url))
    with (
        patch("auto_coder.util.gh_cache._strict_request", return_value=httpx.Response(200, json={"number": 7, "body": "body"}, request=read_request)) as read,
        patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport),
        patch("auto_coder.pr_processor.CodexPrAttributionRepository", return_value=bindings),
        patch("auto_coder.pr_processor.resolve_codex_pr_origin", return_value=attribution),
        patch("auto_coder.pr_processor.canonical_codex_cloud_task_url", return_value="https://example.test/task-123"),
        patch("auto_coder.pr_processor.validate_issue_references"),
    ):
        result = _link_codex_cloud_pr_to_issue("owner/repo", {"number": 7}, client)
    assert result.status == "updated"
    assert read.call_args.kwargs["headers"]["Authorization"] == "Bearer installation-one"
    assert transport.request.call_args.kwargs["headers"]["Authorization"] == "Bearer installation-two"
    assert provider.requests == [("owner/repo", {"pull_requests": "read"}), ("owner/repo", {"pull_requests": "write"})]


class RenewableProvider:
    def __init__(self) -> None:
        self.tokens = ["installation-one", "installation-two"]
        self.calls = 0
        self.requests = []

    def credential_for(self, repo_name, required_permissions):
        self.requests.append((repo_name, dict(required_permissions)))
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


def test_linked_pr_reconciliation_uses_installation_for_both_reads(monkeypatch):
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    sent = []

    def wire(_transport, request):
        sent.append(request)
        if request.url.path == "/graphql":
            return httpx.Response(200, json={"data": {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": [{"number": 7, "repository": {"owner": {"login": "owner"}, "name": "repo"}}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}, request=request)
        return httpx.Response(200, json={"number": 7, "state": "open"}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", wire)
    assert client.has_linked_pr("owner/repo", 42) is True
    assert [request.headers["Authorization"] for request in sent] == ["bearer installation-one", "Bearer installation-two"]
    assert provider.requests == [("owner/repo", {"issues": "read", "pull_requests": "read"}), ("owner/repo", {"pull_requests": "read"})]


def test_issue_list_and_pr_commits_use_installation_credentials():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues")
    transport.request.side_effect = [httpx.Response(200, json=[], request=request), httpx.Response(200, json=[], request=request)]
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        assert client.get_open_issues("owner/repo") == []
        assert client.get_pr_commits("owner/repo", 7) == []
    assert [call.kwargs["headers"]["Authorization"] for call in transport.request.call_args_list] == ["Bearer installation-one", "Bearer installation-two"]
    assert provider.requests == [("owner/repo", {"issues": "read"}), ("owner/repo", {"pull_requests": "read"})]


def test_live_open_entities_and_pr_lookup_use_installation_credentials():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues")
    response = httpx.Response(200, json=[], request=request)
    transport = Mock()
    transport.request.return_value = httpx.Response(200, json={"number": 7}, request=request)
    sent = []

    def strict_send(*args, **kwargs):
        sent.append(kwargs["headers"]["Authorization"])
        return response

    with patch("auto_coder.util.gh_cache._strict_request", side_effect=strict_send), patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        entities = client.get_open_entities_strict("owner/repo")
        assert entities.issues == [] and entities.pull_requests == []
        assert client.get_pull_request("owner/repo", 7)["number"] == 7
    assert sent == ["Bearer installation-one", "Bearer installation-two"]
    assert transport.request.call_args.kwargs["headers"]["Authorization"] == "Bearer installation-two"
    assert provider.requests == [("owner/repo", {"issues": "read"}), ("owner/repo", {"pull_requests": "read"}), ("owner/repo", {"pull_requests": "read"})]


def test_candidate_and_review_reads_use_renewed_installation_credentials():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls")
    transport.request.side_effect = [
        httpx.Response(200, json=[{"number": 7}], request=request),
        httpx.Response(200, json={"number": 7, "head": {"ref": "issue-7"}, "base": {}, "user": {"login": "auto-coder[bot]", "id": 101}}, request=request),
        httpx.Response(200, json=[], request=request),
    ]
    sent = []
    responses = iter(transport.request.side_effect)
    transport.request.side_effect = lambda *args, **kwargs: (sent.append(kwargs["headers"]["Authorization"]), next(responses))[1]
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        candidate = client.get_open_prs_json("owner/repo")[0]
        assert candidate["number"] == 7
        assert _is_dependabot_pr(candidate, ControllerIdentity("auto-coder[bot]", account_id=101, app_id=7)) is False
        assert client.get_pr_reviews_strict("owner/repo", 7) == []
    assert sent == ["Bearer installation-one", "Bearer installation-two", "Bearer installation-two"]
    assert provider.requests == [("owner/repo", {"pull_requests": "read"})] * 3


def test_sub_issue_link_reads_and_writes_use_installation_credentials():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/42/sub_issues")
    transport.request.side_effect = [httpx.Response(200, json=[], request=request), httpx.Response(201, json={}, request=request)]
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        assert client.add_sub_issue("owner/repo", 42, 43, sub_issue_id=101) is True
    assert [call.kwargs["headers"]["Authorization"] for call in transport.request.call_args_list] == ["bearer installation-one", "bearer installation-two"]
    assert provider.requests == [("owner/repo", {"issues": "read"}), ("owner/repo", {"issues": "write"})]


def test_issue_declaration_discovery_uses_installation_credentials():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues")
    transport.request.side_effect = [httpx.Response(200, json=[{"number": 42}], request=request), httpx.Response(200, json=[], request=request)]
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        assert client.get_open_issue_declarations("owner/repo", limit=1) == [{"number": 42}]
    assert [call.kwargs["headers"]["Authorization"] for call in transport.request.call_args_list] == ["Bearer installation-one", "Bearer installation-two"]
    assert provider.requests == [("owner/repo", {"issues": "read"})] * 2


def test_jules_projection_and_timeline_use_installation_credentials():
    from auto_coder.pr_processor import _update_jules_pr_body

    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("PATCH", "https://api.github.com/repos/owner/repo/pulls/7")
    transport.request.return_value = httpx.Response(200, json={}, request=request)
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport), patch("auto_coder.pr_processor.validate_issue_references"):
        assert _update_jules_pr_body("owner/repo", 7, "body", 42, client) is True
    assert transport.request.call_args.kwargs["headers"]["Authorization"] == "Bearer installation-one"
    with patch("auto_coder.util.gh_cache._strict_request", return_value=httpx.Response(200, json=[], request=request)) as send:
        assert client.get_issue_timeline_strict("owner/repo", 7) == []
    assert send.call_args.kwargs["headers"]["Authorization"] == "Bearer installation-two"
    assert provider.requests == [("owner/repo", {"pull_requests": "write"}), ("owner/repo", {"issues": "read"})]


def test_open_pr_membership_observation_renews_on_each_page():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls")
    responses = iter([httpx.Response(200, json=[{"number": 7}], headers={"Link": '<https://api.github.com/repos/owner/repo/pulls?page=2>; rel="next"'}, request=request), httpx.Response(200, json=[{"number": 8}], request=request)])
    sent = []
    transport.request.side_effect = lambda *args, **kwargs: (sent.append(kwargs["headers"]["Authorization"]), next(responses))[1]
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        assert client.get_open_pull_requests("owner/repo") == [{"number": 7}, {"number": 8}]
    assert sent == ["Bearer installation-one", "Bearer installation-two"]


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


def test_conflict_remediation_metadata_read_uses_injected_installation_credential():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/128")
    transport.request.return_value = httpx.Response(
        200,
        json={
            "number": 128,
            "user": {"login": "auto-coder[bot]"},
            "body": "",
            "head": {
                "ref": "issue-128",
                "sha": "d" * 40,
                "repo": {"full_name": "owner/repo"},
            },
            "base": {
                "ref": "main",
                "sha": "e" * 40,
                "repo": {"full_name": "owner/repo"},
            },
        },
        request=request,
    )
    admitted = PRRepairResult(RepairDisposition.ACCEPTED_OR_UNCERTAIN, reason="queued")

    with (
        patch(
            "auto_coder.pr_processor.check_pr_repair_exhaustion",
            return_value=None,
        ),
        patch(
            "auto_coder.pr_processor._offer_selected_local_pr_repair",
            return_value=admitted,
        ),
        patch(
            "auto_coder.util.gh_cache.get_caching_client",
            return_value=transport,
        ),
    ):
        actions = _start_mergeability_remediation(128, "DIRTY", "owner/repo", client)

    sent = transport.request.call_args
    assert sent.kwargs["method"].upper() == "GET"
    assert sent.kwargs["url"] == str(request.url)
    assert sent.kwargs["headers"]["Authorization"] == "Bearer installation-one"
    assert provider.requests == [("owner/repo", {"pull_requests": "read"})]
    assert actions.repair_result is admitted


def test_conflict_remediation_app_credential_failure_never_sends_personal_fallback():
    provider = Mock()
    provider.credential_for.side_effect = RuntimeError("installation unavailable")
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()

    with (
        patch(
            "auto_coder.pr_processor.check_pr_repair_exhaustion",
            return_value=None,
        ),
        patch(
            "auto_coder.util.gh_cache.get_caching_client",
            return_value=transport,
        ),
    ):
        actions = _start_mergeability_remediation(128, "DIRTY", "owner/repo", client)

    transport.request.assert_not_called()
    assert any("installation unavailable" in action for action in actions)


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


def test_management_issue_and_changed_file_reads_use_installation_credentials():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    transport.request.side_effect = [
        httpx.Response(200, json={"number": 42}, request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/42")),
        httpx.Response(200, json={"number": 43}, request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/43")),
        httpx.Response(200, json={"changed_files": 1}, request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/7")),
        httpx.Response(200, json=[{"filename": "src/app.py"}], request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/7/files")),
    ]

    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        assert client.get_issue("owner/repo", 42)["number"] == 42
        assert client.get_issue_strict("owner/repo", 43)["number"] == 43
        assert client.get_pr_changed_file_count("owner/repo", 7) == 1
        assert client.get_pr_changed_files("owner/repo", 7) == [{"filename": "src/app.py"}]

    assert [call.kwargs["headers"]["Authorization"] for call in transport.request.call_args_list] == [
        "Bearer installation-one",
        "Bearer installation-two",
        "Bearer installation-two",
        "Bearer installation-two",
    ]
    assert provider.requests == [
        ("owner/repo", {"issues": "read"}),
        ("owner/repo", {"issues": "read"}),
        ("owner/repo", {"pull_requests": "read"}),
        ("owner/repo", {"pull_requests": "read"}),
    ]


def test_strict_management_readers_refresh_installation_credentials_per_request():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    first_page = [{"id": number + 1, "body": f"comment {number + 1}", "user": {"id": 101, "login": "writer"}} for number in range(100)]
    responses = [
        httpx.Response(
            200,
            json=first_page,
            headers={"Link": '<https://api.github.com/repos/owner/repo/issues/7/comments?per_page=100&page=2>; rel="next"'},
            request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/7/comments"),
        ),
        httpx.Response(200, json=[], request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/7/comments")),
        httpx.Response(200, text="diff --git a/a b/a", request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/7")),
        httpx.Response(200, json={"changed_files": 1}, request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/7")),
        httpx.Response(200, json=[{"filename": "a"}], request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/7/files")),
    ]

    with patch("auto_coder.util.gh_cache._strict_request", side_effect=responses) as strict_request:
        assert len(client.get_pr_comments_strict("owner/repo", 7)) == 100
        assert client.get_pr_diff_strict("owner/repo", 7).startswith("diff --git")
        assert client.get_pr_changed_file_count_strict("owner/repo", 7) == 1
        assert client.get_pr_changed_files_strict("owner/repo", 7) == [{"filename": "a"}]

    assert [call.kwargs["headers"]["Authorization"] for call in strict_request.call_args_list] == [
        "Bearer installation-one",
        "Bearer installation-two",
        "bearer installation-two",
        "Bearer installation-two",
        "Bearer installation-two",
    ]
    assert provider.requests == [("owner/repo", {"issues": "read"})] * 2 + [("owner/repo", {"pull_requests": "read"})] * 3


def test_pr_diff_uses_repository_installation_credential():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/7")
    transport.request.return_value = httpx.Response(200, text="diff --git a/a b/a", request=request)

    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        assert client.get_pr_diff("owner/repo", 7) == "diff --git a/a b/a"

    sent = transport.request.call_args
    assert sent.kwargs["headers"]["Authorization"] == "bearer installation-one"
    assert sent.kwargs["headers"]["Accept"] == "application/vnd.github.v3.diff"
    assert provider.requests == [("owner/repo", {"pull_requests": "read"})]


def test_empty_pr_fallback_uses_app_diff_and_unavailable_diff_is_not_empty():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/7")
    transport.request.side_effect = [
        httpx.Response(200, text="diff --git a/a b/a\n+content", request=request),
        RuntimeError("installation read unavailable"),
    ]
    pr_data = {"number": 7}

    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        assert _is_empty_pr(pr_data, repo_name="owner/repo", github_client=client) is False
        assert _is_empty_pr(pr_data, repo_name="owner/repo", github_client=client) is False

    assert [call.kwargs["headers"]["Authorization"] for call in transport.request.call_args_list] == [
        "bearer installation-one",
        "bearer installation-two",
    ]
    assert provider.requests == [("owner/repo", {"pull_requests": "read"})] * 2


def test_review_thread_mutations_use_repository_installation_credentials():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()

    def respond(*args, **kwargs):
        method = kwargs["method"] if "method" in kwargs else args[0]
        url = kwargs["url"] if "url" in kwargs else args[1]
        request = httpx.Request(method, url)
        if url.endswith("/graphql"):
            operation = "unresolveReviewThread" if "unresolveReviewThread" in kwargs["json"]["query"] else "resolveReviewThread"
            body = {"data": {operation: {"thread": {"id": "thread-1", "isResolved": operation == "resolveReviewThread"}}}}
        else:
            body = {"id": 22, "body": kwargs["json"]["body"]}
        return httpx.Response(200, json=body, request=request)

    transport.request.side_effect = respond
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        client.reply_to_review_thread("owner/repo", 7, 11, "reply")
        client.update_review_thread_comment("owner/repo", 22, "updated")
        client.resolve_review_thread("thread-1")
        client.unresolve_review_thread("thread-1")

    assert [call.kwargs["headers"]["Authorization"] for call in transport.request.call_args_list] == [
        "Bearer installation-one",
        "Bearer installation-two",
        "bearer installation-two",
        "bearer installation-two",
    ]
    assert provider.requests == [("owner/repo", {"pull_requests": "write"})] * 4


def test_issue_pr_publication_retains_server_author_observation():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("POST", "https://api.github.com/repos/owner/repo/pulls")
    transport.request.return_value = httpx.Response(
        201,
        json={
            "number": 73,
            "html_url": "https://github.com/owner/repo/pull/73",
            "user": {"login": "server-observed[bot]", "id": 909},
        },
        request=request,
    )
    config = AutomationConfig()
    config.PR_LABEL_COPYING_ENABLED = False

    with (
        patch("auto_coder.issue_processor.validate_issue_references"),
        patch("auto_coder.issue_processor.get_commit_log", return_value="commit"),
        patch(
            "auto_coder.issue_processor.run_llm_noedit_prompt",
            return_value='{"title":"Controller PR","body":"Publication body"}',
        ),
        patch(
            "auto_coder.issue_processor.parse_llm_output_as_json",
            return_value={"title": "Controller PR", "body": "Publication body"},
        ),
        patch("time.sleep"),
        patch.object(client, "find_pr_by_head_branch", return_value=None),
        patch.object(client, "get_pr_closing_issues", return_value=[42]),
        patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport),
        get_trace_collector().start_execution("owner/repo", "issue", 42, origin="worker"),
    ):
        result = _create_pr_for_issue(
            "owner/repo",
            {"number": 42, "title": "Route controller auth", "body": "Body", "labels": []},
            "issue-42",
            "main",
            "Implemented",
            client,
            config,
        )

    assert result.startswith("Successfully created PR")
    sent = transport.request.call_args
    assert sent.kwargs["headers"]["Authorization"] == "Bearer installation-one"
    assert sent.kwargs["json"] == {
        "title": "Controller PR",
        "body": "<!-- auto-coder:local-llm -->\n\nCloses #42\n\nPublication body",
        "head": "issue-42",
        "base": "main",
    }
    snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=42)
    publication = [event for event in snapshot.events if event.stage_id == "issue.pr-publication"]
    assert len(publication) == 1
    assert publication[0].facts == {
        "issue_number": 42,
        "pr_number": 73,
        "pr_url": "https://github.com/owner/repo/pull/73",
        "pr_author": {"login": "server-observed[bot]", "id": 909},
    }


def test_issue_authority_read_renews_repository_credential_on_each_page():
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    transport = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/1/sub_issues")
    transport.request.side_effect = [
        httpx.Response(200, json=[{"number": n} for n in range(2, 102)], request=request),
        httpx.Response(200, json=[{"number": 102}], request=request),
    ]
    sent = []

    def capture(*args, **kwargs):
        sent.append(kwargs["headers"]["Authorization"])
        return transport.request(*args, **kwargs)

    with patch("auto_coder.util.gh_cache.get_caching_client") as factory:
        factory.return_value.request.side_effect = capture
        result = client.get_direct_sub_issues_strict("owner/repo", 1)

    assert len(result) == 101
    assert sent == ["Bearer installation-one", "Bearer installation-two"]


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


def test_strict_relationship_mutations_send_renewed_installation_credentials(monkeypatch):
    provider = RenewableProvider()
    client = GitHubClient(auth_provider=provider, repository="owner/repo")
    sent = []

    def wire(_transport, request):
        sent.append(request)
        return httpx.Response(201, json={}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", wire)
    client.mutate_blocked_by_strict("owner/repo", 42, 100, add=True)
    client.add_sub_issue_strict("owner/repo", 42, 43, 101)

    assert [request.headers["Authorization"] for request in sent] == ["Bearer installation-one", "Bearer installation-two"]
    assert [request.url.path for request in sent] == [
        "/repos/owner/repo/issues/42/dependencies/blocked_by",
        "/repos/owner/repo/issues/42/sub_issues",
    ]
    assert provider.requests == [("owner/repo", {"issues": "write"})] * 2
