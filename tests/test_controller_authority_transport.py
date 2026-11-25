"""Production transport regressions for controller identity-history authority."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from auto_coder.controller_identity_history import controller_author_set_for_client, controller_authority_scope
from auto_coder.util.gh_cache import GitHubClient
from auto_coder.util.github_request_outcome import DeliveryCertainty, GitHubRequestRefused, configure_github_request_boundary


@dataclass
class NativeController:
    client: GitHubClient
    history_path: Path
    change: str = ""
    armed: bool = False
    mutated: list[httpx.Request] = field(default_factory=list)
    observed: list = field(default_factory=list)


@pytest.fixture
def native_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()
    path = tmp_path / ".auto-coder" / "owner" / "repo" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("[github-controller-identity-history]\nprior_logins = []\n", encoding="utf-8")
    fixture = NativeController(GitHubClient("first-token"), path)

    def admission(context):
        if context.kind == "mutation" and fixture.armed:
            fixture.armed = False
            if fixture.change == "history":
                path.write_text('[github-controller-identity-history]\nprior_logins = ["prior-controller"]\n', encoding="utf-8")
            else:
                fixture.client.token = "replacement-token"
        return True

    def wire(_transport, request):
        if request.method == "GET" and request.url.path == "/user":
            replacement = request.headers.get("Authorization", "").endswith("replacement-token")
            account_id = 30 if fixture.change == "identity" and replacement else 20
            if fixture.change == "during-proof" and replacement:
                fixture.client.token = "third-token"
            payload = {"login": "replacement" if account_id == 30 else "current", "id": account_id}
        elif request.method == "GET" and request.url.path == "/users/prior-controller":
            payload = {"login": "prior-controller", "id": 10}
        else:
            fixture.mutated.append(request)
            body = json.loads(request.read())
            if request.url.path == "/graphql":
                operation = "unresolveReviewThread" if "unresolveReviewThread" in body["query"] else "resolveReviewThread"
                payload = {"data": {operation: {"thread": {"id": "thread-1", "isResolved": operation == "resolveReviewThread"}}}}
            else:
                payload = {"id": 22, "body": body["body"]}
        return httpx.Response(200, json=payload, headers={"Cache-Control": "no-store"}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", wire)
    configure_github_request_boundary(admission, fixture.observed.append)
    try:
        yield fixture
    finally:
        configure_github_request_boundary()


def _mutate(client: GitHubClient, effect: str) -> None:
    if effect == "reply":
        client.reply_to_review_thread("owner/repo", 7, 11, "receipt")
    elif effect == "update":
        client.update_review_thread_comment("owner/repo", 22, "receipt")
    elif effect == "comment":
        client.add_comment_to_pr("owner/repo", 7, "feedback receipt")
    elif effect == "comment-update":
        client.update_comment_for_issue("owner/repo", 22, "feedback receipt")
    elif effect == "resolve":
        client.resolve_review_thread("thread-1")
    else:
        client.unresolve_review_thread("thread-1")


@pytest.mark.parametrize("effect", ["reply", "update", "comment", "comment-update", "resolve", "unresolve"])
@pytest.mark.parametrize("change", ["identity", "history", "during-proof"])
def test_changed_authority_at_native_dispatch_sends_no_mutation(native_controller, effect, change):
    """The admission barrier occurs after caller checks and request assembly."""
    fixture = native_controller
    authors = controller_author_set_for_client(fixture.client, "owner/repo")
    with pytest.raises(GitHubRequestRefused):
        with controller_authority_scope(fixture.client, "owner/repo", authors):
            fixture.change, fixture.armed = change, True
            _mutate(fixture.client, effect)

    assert fixture.mutated == []
    refusals = [item for item in fixture.observed if item.context.kind == "mutation"]
    assert len(refusals) == (1 if change == "history" else 2)
    assert all(item.delivery is DeliveryCertainty.DEFINITELY_NOT_SENT for item in refusals)
    assert all("authority" in item.message.lower() for item in refusals)


@pytest.mark.parametrize("effect", ["reply", "update", "comment", "comment-update", "resolve", "unresolve"])
def test_same_account_token_refresh_binds_validated_token_to_native_request(native_controller, effect):
    fixture = native_controller
    authors = controller_author_set_for_client(fixture.client, "owner/repo")
    with controller_authority_scope(fixture.client, "owner/repo", authors):
        fixture.change, fixture.armed = "refresh", True
        _mutate(fixture.client, effect)

    assert len(fixture.mutated) == 1
    assert fixture.mutated[0].headers["Authorization"] == "Bearer replacement-token"
    assert controller_author_set_for_client(fixture.client, "owner/repo").fingerprint == authors.fingerprint


def test_nested_native_scope_cannot_replace_outer_authority(native_controller):
    fixture = native_controller
    original = controller_author_set_for_client(fixture.client, "owner/repo")
    with pytest.raises(GitHubRequestRefused):
        with fixture.client.controller_authority_scope("owner/repo", original):
            fixture.history_path.write_text('[github-controller-identity-history]\nprior_logins = ["prior-controller"]\n', encoding="utf-8")
            replacement = controller_author_set_for_client(fixture.client, "owner/repo")
            with fixture.client.controller_authority_scope("owner/repo", replacement):
                _mutate(fixture.client, "reply")
    assert fixture.mutated == []


def test_native_guard_does_not_leak_after_critical_section(native_controller):
    fixture = native_controller
    original = controller_author_set_for_client(fixture.client, "owner/repo")
    with fixture.client.controller_authority_scope("owner/repo", original):
        pass
    fixture.history_path.write_text('[github-controller-identity-history]\nprior_logins = ["prior-controller"]\n', encoding="utf-8")
    _mutate(fixture.client, "reply")
    assert len(fixture.mutated) == 1


@pytest.mark.parametrize("change", ["refresh", "history"])
def test_authority_revalidation_coexists_with_single_slot_governor(native_controller, tmp_path, change):
    """Proof never waits on its own reservation, including a token refresh."""
    from auto_coder.github_request_governor import GitHubRequestGovernor

    fixture = native_controller
    fixture.history_path.write_text('[github-controller-identity-history]\nprior_logins = ["prior-controller"]\n', encoding="utf-8")
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3")

    def admit(context):
        # A recursive proof while a mutation holds this slot fails immediately,
        # making the regression deterministic rather than waiting for a timeout.
        result = governor.admit(context)
        if context.kind == "mutation" and fixture.armed:
            fixture.armed = False
            if change == "refresh":
                fixture.client.token = "replacement-token"
            else:
                fixture.history_path.write_text("[github-controller-identity-history]\nprior_logins = []\n", encoding="utf-8")
        return result

    configure_github_request_boundary(admit, governor.observe)
    try:
        authors = controller_author_set_for_client(fixture.client, "owner/repo")
        fixture.change = change
        if change == "history":
            with pytest.raises(GitHubRequestRefused):
                with controller_authority_scope(fixture.client, "owner/repo", authors):
                    fixture.armed = True
                    _mutate(fixture.client, "reply")
            assert fixture.mutated == []
        else:
            with controller_authority_scope(fixture.client, "owner/repo", authors):
                fixture.armed = True
                _mutate(fixture.client, "reply")
            assert len(fixture.mutated) == 1
            assert fixture.mutated[0].headers["Authorization"] == "Bearer replacement-token"
        assert governor._connection.execute("SELECT COUNT(*) FROM reservations WHERE resolved=0 AND recovered=0").fetchone() == (0,)
        assert governor._connection.execute("SELECT COUNT(*) FROM reservations WHERE kind='mutation'").fetchone() == (1 if change == "refresh" else 0,)
    finally:
        configure_github_request_boundary()
        governor.close()
