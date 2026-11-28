"""Controller migration through native feedback readers and provider transports."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from auto_coder import pr_processor as processor
from auto_coder.jules_client import JulesClient
from auto_coder.pr_processor import CloudTaskOrigin, CloudTaskOriginResolution
from auto_coder.util.gh_cache import GitHubClient, PullRequestRepairMetadata, ReviewThread, ReviewThreadComment
from tests.pr_automation_fakes import install_pr_history

REPOSITORY = "owner/repo"
PR_NUMBER = 42
HEAD = "a" * 40
TASK = "existing-session"
FINDING = "### Auto-Coder adversarial finding\n\nPreserve the exact controller operation evidence."


@dataclass
class FeedbackFixture:
    github: GitHubClient
    provider: JulesClient
    history_path: Path
    state_path: Path
    thread: ReviewThread
    pages: dict[int, object] = field(default_factory=lambda: {1: []})
    reads: list[int] = field(default_factory=list)
    provider_sends: list[str] = field(default_factory=list)
    publications: list[str] = field(default_factory=list)
    after_send: object = None

    @property
    def finding_identity(self) -> str:
        return processor._review_feedback_identity(f"{REPOSITORY}#{PR_NUMBER}:jules:{TASK}:", self.thread, 0)

    def declare(self, logins: list[str] | None) -> None:
        body = "[github-app-auto-coder-controller]\napp_id = 77\n"
        if logins is not None:
            body += "[github-controller-identity-history]\nprior_logins = " + json.dumps(logins) + "\n"
        self.history_path.write_text(body)

    def receipt(self, identity: str | None = None, author: int = 10, body: str | None = None) -> dict:
        return {
            "id": 999,
            "body": body if body is not None else processor._cloud_review_feedback_markers([identity or self.finding_identity]),
            "user": {"id": author, "login": {10: "previous", 20: "current", 30: "other"}[author]},
        }

    def invoke(self, mode: str):
        data = {"number": PR_NUMBER, "state": "open", "body": "", "head": {"ref": "feature", "sha": HEAD}, "base": {"ref": "main"}, "user": {"login": "implementer"}}
        if mode == "ordinary":
            return processor._delegate_cloud_review_thread_repair(REPOSITORY, data, github_client=self.github, unresolved_threads=(self.thread,))
        return processor._send_adversarial_validation_feedback_to_cloud_task(REPOSITORY, data, HEAD, "NEEDS_FIX\n" + FINDING, self.github, (FINDING,))


@pytest.fixture
def feedback_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FeedbackFixture:
    history = Path.home() / ".auto-coder" / "owner" / "repo" / "config.toml"
    history.parent.mkdir(parents=True, exist_ok=True)
    github = GitHubClient("current-token")
    install_pr_history(github, HEAD)
    provider = JulesClient()
    thread = ReviewThread(id="PRRT_feedback", is_resolved=False, comments=[ReviewThreadComment(database_id=501, author_login="reviewer[bot]", author_id=90, body=FINDING)])
    fixture = FeedbackFixture(github, provider, history, tmp_path / "feedback.json", thread)
    fixture.declare(["previous"])

    class Accounts:
        def __init__(self, token: str):
            self.token = token

        def __call__(self, path: str, **kwargs):
            assert kwargs["headers"]["Cache-Control"] == "no-cache"
            if path == "/user":
                return {"login": "other" if self.token == "changed-token" else "current", "id": 30 if self.token == "changed-token" else 20}
            return {"login": path.rsplit("/", 1)[-1], "id": {"previous": 10, "other": 30}[path.rsplit("/", 1)[-1]]}

    def conversation(method: str, url: str, **kwargs):
        assert method == "GET"
        assert url.endswith(f"/issues/{PR_NUMBER}/comments")
        assert kwargs["headers"]["Cache-Control"] == "no-cache"
        page = kwargs["params"]["page"]
        fixture.reads.append(page)
        payload = fixture.pages[page]
        if isinstance(payload, Exception):
            raise payload
        headers = {}
        if page + 1 in fixture.pages:
            headers["Link"] = f'<{url}?per_page=100&page={page + 1}>; rel="next"'
        return httpx.Response(200, json=payload, headers=headers, request=httpx.Request("GET", url))

    def send(_url: str, *, json: dict, timeout: int):
        fixture.provider_sends.append(json["prompt"])
        if callable(fixture.after_send):
            fixture.after_send()
        return SimpleNamespace(status_code=200, json=lambda: {"message": "accepted"})

    monkeypatch.setattr("auto_coder.util.gh_cache.get_ghapi_client", Accounts)
    monkeypatch.setattr("auto_coder.util.gh_cache._strict_request", conversation)
    monkeypatch.setattr(github, "get_pull_request_repair_metadata_strict", lambda *_: PullRequestRepairMetadata(head_ref="feature", head_sha=HEAD, base_ref="main"))
    monkeypatch.setattr(github, "get_pr_review_threads_strict", lambda *_: [thread])
    monkeypatch.setattr(github, "add_comment_to_pr", lambda _repo, _pr, body: fixture.publications.append(body))
    monkeypatch.setattr(provider.session, "post", send)
    monkeypatch.setattr(provider, "get_task", lambda *_: None)
    monkeypatch.setattr(processor, "_resolve_cloud_task_origin", lambda *_: CloudTaskOriginResolution(origin=CloudTaskOrigin("jules", TASK, provider)))
    monkeypatch.setattr(processor, "_read_review_repair_routing_metadata", lambda *_: None)
    monkeypatch.setattr(processor, "_cloud_review_repair_state_path", lambda *_: fixture.state_path)
    return fixture


@pytest.mark.parametrize("mode", ["ordinary", "adversarial"])
def test_prior_controller_receipt_on_later_native_page_suppresses_resend(feedback_fixture: FeedbackFixture, mode: str):
    f = feedback_fixture
    f.pages = {1: [{"id": index, "body": "Unrelated discussion", "user": {"id": 40, "login": "reader"}} for index in range(1, 101)], 2: [f.receipt()]}
    f.invoke(mode)
    assert f.reads == [1, 2]
    assert f.provider_sends == []


@pytest.mark.parametrize("mode", ["ordinary", "adversarial"])
@pytest.mark.parametrize("failed_page", [1, 2])
def test_unreadable_conversation_never_dispatches_provider(feedback_fixture: FeedbackFixture, mode: str, failed_page: int):
    f = feedback_fixture
    f.pages = {1: [{"id": 1, "body": "Discussion", "user": {"id": 40, "login": "reader"}}], 2: OSError("native conversation unavailable")}
    if failed_page == 1:
        f.pages = {1: OSError("native conversation unavailable")}
    result = f.invoke(mode)
    assert f.provider_sends == []
    assert f.publications == []
    assert any("unavailable" in action for action in result)


@pytest.mark.parametrize("mode", ["ordinary", "adversarial"])
@pytest.mark.parametrize("candidate", ["exact", "different-digest", "quoted", "fenced", "indented", "empty", "complete-empty-nonmember"])
def test_unknown_history_blocks_only_exact_relevant_protocol(feedback_fixture: FeedbackFixture, mode: str, candidate: str):
    f = feedback_fixture
    f.declare([] if candidate == "complete-empty-nonmember" else None)
    marker = processor._cloud_review_feedback_markers([f.finding_identity])
    if candidate == "different-digest":
        f.pages[1] = [f.receipt(identity="another-operation")]
    elif candidate == "quoted":
        f.pages[1] = [f.receipt(body="> " + marker)]
    elif candidate == "fenced":
        f.pages[1] = [f.receipt(body="```\n" + marker + "\n```")]
    elif candidate == "indented":
        f.pages[1] = [f.receipt(body="    " + marker)]
    elif candidate != "empty":
        f.pages[1] = [f.receipt()]
    result = f.invoke(mode)
    assert len(f.provider_sends) == (0 if candidate == "exact" else 1)
    if candidate == "exact":
        assert any("historical" in action for action in result)


@pytest.mark.parametrize("mode", ["ordinary", "adversarial"])
@pytest.mark.parametrize("transition", ["identity", "history", "token-refresh"])
def test_change_after_caller_check_is_checked_at_actual_provider_boundary(feedback_fixture: FeedbackFixture, monkeypatch: pytest.MonkeyPatch, mode: str, transition: str):
    f = feedback_fixture
    original = f.provider.send_message

    def between_checks(task_id: str, message: str):
        if transition == "history":
            f.declare([])
        else:
            f.github.token = "changed-token" if transition == "identity" else "refreshed-token"
        return original(task_id, message)

    monkeypatch.setattr(f.provider, "send_message", between_checks)
    result = f.invoke(mode)
    assert len(f.provider_sends) == (1 if transition == "token-refresh" else 0)
    if transition != "token-refresh":
        assert not getattr(result, "delivered", False)
        assert f.publications == []


@pytest.mark.parametrize("mode", ["ordinary", "adversarial"])
@pytest.mark.parametrize("transition", ["identity", "history"])
def test_accepted_effect_survives_authority_change_without_success_or_replay(feedback_fixture: FeedbackFixture, mode: str, transition: str):
    f = feedback_fixture

    def change_authority():
        if transition == "identity":
            f.github.token = "changed-token"
        else:
            f.declare([])

    f.after_send = change_authority
    first = f.invoke(mode)
    state = json.loads(f.state_path.read_text())
    assert len(f.provider_sends) == 1
    assert state["delivered_feedback"] == []
    assert state["observed_feedback"]
    assert set(state["observed_feedback"]).issubset(state["pending_feedback"])
    assert not getattr(first, "delivered", False)
    assert f.publications == []

    f.after_send = None
    second = f.invoke(mode)
    assert len(f.provider_sends) == 1
    settled = json.loads(f.state_path.read_text())
    assert f.finding_identity in settled["delivered_feedback"]
    assert settled["observed_feedback"] == []
    assert settled["pending_feedback"] == []
    assert not any("unavailable" in action or "unresolved" in action for action in second)


def test_adversarial_native_marker_prefix_remains_recoverable(feedback_fixture: FeedbackFixture):
    f = feedback_fixture
    body = processor.adversarial_validation_codex_feedback_marker(HEAD) + "\n" + processor._cloud_review_feedback_markers([f.finding_identity]) + "\nAccepted feedback"
    f.pages[1] = [f.receipt(body=body)]
    assert processor._load_pr_delivered_review_feedback(f.github, REPOSITORY, PR_NUMBER, [f.finding_identity]) == {f.finding_identity}


@pytest.mark.parametrize("mode", ["ordinary", "adversarial"])
def test_transition_immediately_before_local_settlement_retains_original_acceptance(feedback_fixture: FeedbackFixture, monkeypatch: pytest.MonkeyPatch, mode: str):
    f = feedback_fixture
    original = processor._record_delivered_review_feedback
    changed = False

    def before_settlement(path: Path, identities: set[str]):
        nonlocal changed
        if not changed:
            changed = True
            f.declare([])
        return original(path, identities)

    monkeypatch.setattr(processor, "_record_delivered_review_feedback", before_settlement)
    first = f.invoke(mode)
    state = json.loads(f.state_path.read_text())
    assert len(f.provider_sends) == 1
    assert state["delivered_feedback"] == []
    assert state["observed_feedback"]
    assert state["pending_feedback"]
    assert f.publications == []
    assert not getattr(first, "delivered", False)
    f.invoke(mode)
    assert len(f.provider_sends) == 1
    settled = json.loads(f.state_path.read_text())
    assert f.finding_identity in settled["delivered_feedback"]
    assert settled["pending_feedback"] == []


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("transition", ["history", "identity", "token-refresh"])
def test_native_provider_dispatch_revalidates_after_credential_preparation(feedback_fixture: FeedbackFixture, monkeypatch: pytest.MonkeyPatch, provider: str, transition: str):
    from auto_coder.controller_identity_history import ControllerIdentityHistoryError, controller_authority_scope

    f = feedback_fixture
    sends = []

    def change():
        if transition == "history":
            f.declare([])
        else:
            f.github.token = "changed-token" if transition == "identity" else "refreshed-token"

    if provider == "codex":
        from auto_coder.codex_wham_client import CodexWhamClient

        native = CodexWhamClient()

        def headers():
            change()
            return {"Authorization": "Bearer synthetic-provider-token"}

        monkeypatch.setattr(native, "_get_headers", headers)
        monkeypatch.setattr("auto_coder.codex_wham_client.httpx.post", lambda *_args, **_kwargs: sends.append("post") or SimpleNamespace(status_code=200))
        dispatch = lambda: native.send_follow_up("task_e_fixture", "turn-fixture", "Address the recorded finding")
    else:
        from auto_coder.claude_routine_client import ClaudeRoutineClient

        native = ClaudeRoutineClient()

        def credential(_token):
            change()
            return "synthetic-provider-token"

        monkeypatch.setattr("auto_coder.claude_routine_client.resolve_claude_oauth_token_non_inference", credential)
        monkeypatch.setattr("auto_coder.claude_routine_client.observe_claude_usage_strict", lambda *_args, **_kwargs: SimpleNamespace(available=True, insufficient=False))
        monkeypatch.setattr("auto_coder.claude_routine_client.CommandExecutor.run_command", lambda *_args, **_kwargs: sends.append("cli") or SimpleNamespace(returncode=0, stderr="", stdout="accepted"))
        dispatch = lambda: native.send_followup("session-fixture", "Address the recorded finding")

    if transition == "token-refresh":
        with controller_authority_scope(f.github, REPOSITORY):
            dispatch()
        assert len(sends) == 1
    else:
        with pytest.raises(ControllerIdentityHistoryError):
            with controller_authority_scope(f.github, REPOSITORY):
                dispatch()
        assert sends == []


@pytest.mark.parametrize("mode", ["ordinary", "adversarial"])
@pytest.mark.parametrize("evidence", ["prior", "current", "none"])
def test_unavailable_historical_lookup_is_scoped_to_relevant_prior_evidence(feedback_fixture: FeedbackFixture, mode: str, evidence: str):
    f = feedback_fixture
    f.declare(["missing-account"])
    if evidence != "none":
        f.pages[1] = [f.receipt(author=10 if evidence == "prior" else 20)]
    result = f.invoke(mode)
    assert len(f.provider_sends) == (1 if evidence == "none" else 0)
    if evidence == "prior":
        assert f.publications == []
        assert any("historical" in action for action in result)


@pytest.mark.parametrize("provider_name", ["codex-cloud", "claude-routine"])
@pytest.mark.parametrize("mode", ["ordinary", "adversarial"])
@pytest.mark.parametrize("boundary", ["record-acceptance", "after-serialization"])
def test_native_recovery_settlement_revalidates_controller_and_retains_acceptance(feedback_fixture: FeedbackFixture, monkeypatch: pytest.MonkeyPatch, provider_name: str, mode: str, boundary: str):
    """The supported providers' recovery writer cannot bypass receipt authority."""
    from auto_coder.pr_no_commit_recovery import NoCommitRecovery

    f = feedback_fixture
    sends = []
    if provider_name == "codex-cloud":
        from auto_coder.codex_cloud_client import CodexCloudClient
        from auto_coder.codex_wham_client import CodexWhamClient, WhamTurn

        task_id = "task_e_historyfixture"
        native = CodexCloudClient(repo_name=REPOSITORY)
        native.wham_client = CodexWhamClient()
        monkeypatch.setattr(native.wham_client, "_get_headers", lambda: {"Authorization": "Bearer synthetic-provider-token"})
        monkeypatch.setattr(native.wham_client, "get_task_turns", lambda *_: [WhamTurn(id="assistant-turn", role="assistant", status="completed", created_at="1")])
        monkeypatch.setattr("auto_coder.codex_wham_client.httpx.post", lambda *_args, **_kwargs: sends.append("post") or SimpleNamespace(status_code=200))
    else:
        from auto_coder.claude_routine_client import ClaudeRoutineClient

        task_id = "session-history-fixture"
        native = ClaudeRoutineClient(repo_name=REPOSITORY)
        monkeypatch.setattr("auto_coder.claude_routine_client.resolve_claude_oauth_token_non_inference", lambda *_: "synthetic-provider-token")
        monkeypatch.setattr("auto_coder.claude_routine_client.observe_claude_usage_strict", lambda *_args, **_kwargs: SimpleNamespace(available=True, insufficient=False))
        monkeypatch.setattr("auto_coder.claude_routine_client.CommandExecutor.run_command", lambda *_args, **_kwargs: sends.append("cli") or SimpleNamespace(returncode=0, stderr="", stdout="accepted"))
        monkeypatch.setattr(native, "get_task", lambda *_: None)

    monkeypatch.setattr(processor, "_resolve_cloud_task_origin", lambda *_: CloudTaskOriginResolution(origin=CloudTaskOrigin(provider_name, task_id, native)))
    changed = False
    if boundary == "record-acceptance":
        original_accept = NoCommitRecovery.record_acceptance

        def at_acceptance(store, *args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                f.declare([])
            return original_accept(store, *args, **kwargs)

        monkeypatch.setattr(NoCommitRecovery, "record_acceptance", at_acceptance)
    else:
        original_dump = json.dump

        def after_serialization(document, stream, *args, **kwargs):
            nonlocal changed
            result = original_dump(document, stream, *args, **kwargs)
            if not changed and isinstance(document, dict) and document.get("no_commit_series"):
                changed = True
                f.declare([])
            return result

        monkeypatch.setattr("auto_coder.pr_no_commit_recovery.json.dump", after_serialization)

    first = f.invoke(mode)
    assert len(sends) == 1
    assert changed
    state = json.loads(f.state_path.read_text())
    assert state["delivered_feedback"] == []
    assert state["observed_feedback"]
    assert set(state["observed_feedback"]).issubset(state["pending_feedback"])
    assert state["no_commit_series"] == {}
    assert not getattr(first, "delivered", False)
    assert f.publications == []

    f.invoke(mode)
    assert len(sends) == 1
    settled = json.loads(f.state_path.read_text())
    finding_identity = processor._review_feedback_identity(f"{REPOSITORY}#{PR_NUMBER}:{provider_name}:{task_id}:", f.thread, 0)
    assert finding_identity in settled["delivered_feedback"]
    assert settled["pending_feedback"] == []
    assert settled["observed_feedback"] == []
