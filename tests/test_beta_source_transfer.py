"""Fresh repository identity evidence for retained beta selection histories."""

import json

import httpx
import pytest

from auto_coder.beta_source_selection import BETA_REF, CURRENT_SELECTION_REF, BetaSourceConflict, BetaSourceSelector, MutationCertainty
from auto_coder.util import gh_cache
from auto_coder.util.gh_cache import GitDataResponse, GitHubGitDataClient
from tests.test_beta_source_selection import PROOF_B, SHA_A, SHA_B, GitHubBoundary


def _identity_client(monkeypatch, historical_status=301, historical_data=None, location="https://api.github.com/repositories/17", current_data=None):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json=current_data if current_data is not None else {"id": 17, "full_name": "owner/repo"})
        assert request.url.path == "/repos/old/repo"
        return httpx.Response(historical_status, json=historical_data, headers={"Location": location})

    monkeypatch.setattr(gh_cache, "github_http_client", lambda **_kwargs: httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False))
    return GitHubGitDataClient("fixture-token", "owner/repo"), calls


@pytest.mark.parametrize("historical_status,historical_data", [(301, None), (200, {"id": 17, "full_name": "old/repo"})])
def test_identity_accepts_only_fresh_matching_ids_without_following_numeric_route(monkeypatch, historical_status, historical_data):
    client, calls = _identity_client(monkeypatch, historical_status, historical_data)
    assert client.same_repository("old/repo") is True
    assert calls == ["https://api.github.com/repos/owner/repo", "https://api.github.com/repos/old/repo"]


@pytest.mark.parametrize(
    "location",
    [
        "https://api.github.com/repositories/18",
        "https://evil.example/repositories/17",
        "http://api.github.com/repositories/17",
        "https://user@api.github.com/repositories/17",
        "https://api.github.com:8443/repositories/17",
        "https://api.github.com/repositories/17?other=1",
        "https://api.github.com/repositories/17#other",
        "https://api.github.com/repositories/17/other",
        "https://api.github.com/repos/owner/repo",
        "https://api.github.com/repositories/017",
        "",
    ],
)
def test_identity_rejects_foreign_or_malformed_numeric_locations(monkeypatch, location):
    client, calls = _identity_client(monkeypatch, location=location)
    with pytest.raises(RuntimeError):
        client.same_repository("old/repo")
    assert len(calls) == 2


@pytest.mark.parametrize("status,data", [(200, {"id": 18, "full_name": "old/repo"}), (200, {"id": True, "full_name": "old/repo"}), (200, {"id": 17, "full_name": "fake/alias"}), (302, {"url": "https://api.github.com/repositories/17"}), (403, None), (404, None), (503, None)])
def test_identity_rejects_reused_names_unavailable_reads_and_body_only_aliases(monkeypatch, status, data):
    client, _calls = _identity_client(monkeypatch, status, data)
    with pytest.raises(RuntimeError):
        client.same_repository("old/repo")


@pytest.mark.parametrize("data", [{}, {"id": True, "full_name": "owner/repo"}, {"id": 0, "full_name": "owner/repo"}, {"id": 17, "full_name": "foreign/repo"}])
def test_identity_requires_valid_current_canonical_anchor(monkeypatch, data):
    client, calls = _identity_client(monkeypatch, current_data=data)
    with pytest.raises(RuntimeError):
        client.same_repository("old/repo")
    assert calls == ["https://api.github.com/repos/owner/repo"]


class TransferBoundary(GitHubBoundary):
    def __init__(self):
        super().__init__()
        self.identity = True
        self.identity_calls = []
        self.current_writes = 0
        self.lose_current_reply = False
        self.drop_current_write = False

    def same_repository(self, historical):
        self.identity_calls.append(historical)
        if self.identity is None:
            raise RuntimeError("identity unavailable")
        return self.identity

    def request(self, method, path, payload=None):
        if method == "PATCH" and path == "git/refs/tags/auto-coder-beta-selection-current":
            self.calls.append((method, path))
            self.current_writes += 1
            if not self.drop_current_write:
                self.refs[CURRENT_SELECTION_REF] = ("tag", payload["sha"])
            if self.lose_current_reply:
                raise RuntimeError("reply lost")
            return GitDataResponse(200)
        return super().request(method, path, payload)


def _transferred():
    remote = TransferBoundary()
    assert BetaSourceSelector(remote, "owner/repo").advance("r900", None).certainty is MutationCertainty.CONFIRMED
    old_oid = remote.refs[CURRENT_SELECTION_REF][1]
    data = json.loads(remote.tags[old_oid]["message"])
    data["repository"] = "old/repo"
    remote.tags[old_oid]["message"] = json.dumps(data, sort_keys=True, separators=(",", ":"))
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T02:00:00Z")
    remote.add_proof(SHA_B, PROOF_B, 102, 1)
    return remote, old_oid, remote.tags[old_oid]["message"]


def test_transfer_keeps_original_history_and_confirms_both_current_refs():
    remote, old_oid, old_message = _transferred()
    result = BetaSourceSelector(remote, "owner/repo").advance("r901", SHA_A)
    assert result.certainty is MutationCertainty.CONFIRMED
    assert remote.refs[BETA_REF] == ("commit", SHA_B)
    assert remote.refs[CURRENT_SELECTION_REF] == remote.refs["refs/tags/auto-coder-beta-selection-r901"]
    assert remote.refs["refs/tags/auto-coder-beta-selection-r900"] == ("tag", old_oid)
    assert remote.tags[old_oid]["message"] == old_message
    assert json.loads(remote.tags[remote.refs[CURRENT_SELECTION_REF][1]]["message"])["repository"] == "owner/repo"
    assert remote.identity_calls == ["old/repo", "old/repo"]


@pytest.mark.parametrize("identity", [False, None])
def test_unverified_transfer_fails_before_source_pointer_effect(identity):
    remote, old_oid, old_message = _transferred()
    remote.identity = identity
    before = remote.pointer_mutations
    with pytest.raises(BetaSourceConflict):
        BetaSourceSelector(remote, "owner/repo").advance("r901", SHA_A)
    assert remote.pointer_mutations == before
    assert remote.refs[BETA_REF] == ("commit", SHA_A)
    assert remote.refs[CURRENT_SELECTION_REF] == ("tag", old_oid)
    assert remote.tags[old_oid]["message"] == old_message
    assert "refs/tags/auto-coder-beta-selection-r901-pointer-attempt" not in remote.refs


def test_tampered_predecessor_history_fails_before_source_pointer_effect():
    remote, _old_oid, _message = _transferred()
    remote.refs["refs/tags/auto-coder-beta-selection-r900"] = ("tag", "f" * 40)
    before = remote.pointer_mutations
    with pytest.raises(BetaSourceConflict):
        BetaSourceSelector(remote, "owner/repo").advance("r901", SHA_A)
    assert remote.pointer_mutations == before
    assert remote.refs[BETA_REF] == ("commit", SHA_A)


@pytest.mark.parametrize("applied", [False, True])
def test_transfer_current_selection_lost_reply_does_not_repeat_a_write(applied):
    remote, old_oid, old_message = _transferred()
    remote.lose_current_reply = True
    remote.drop_current_write = not applied
    controller = BetaSourceSelector(remote, "owner/repo")
    first = controller.advance("r901", SHA_A)
    writes = (remote.pointer_mutations, remote.current_writes)
    second = controller.advance("r901", SHA_A)
    expected = MutationCertainty.CONFIRMED if applied else MutationCertainty.SENT_UNKNOWN
    assert first.certainty is second.certainty is expected
    assert (remote.pointer_mutations, remote.current_writes) == writes
    assert remote.tags[old_oid]["message"] == old_message


def test_existing_partial_source_update_reconciles_without_resending_source_effect():
    remote, old_oid, old_message = _transferred()
    controller = BetaSourceSelector(remote, "owner/repo")
    # A retained operation from an older writer has already sent its source
    # pointer before encountering the predecessor identity failure.
    remote.identity = True
    original_confirm = controller._confirm_current_selection
    controller._confirm_current_selection = lambda _record: (_ for _ in ()).throw(RuntimeError("old writer stopped after source update"))
    with pytest.raises(RuntimeError, match="old writer stopped"):
        controller.advance("r901", SHA_A)
    writes = remote.pointer_mutations
    controller._confirm_current_selection = original_confirm
    assert controller.advance("r901", SHA_A).certainty is MutationCertainty.CONFIRMED
    assert remote.pointer_mutations == writes
    assert remote.tags[old_oid]["message"] == old_message


def test_predecessor_identity_is_revalidated_before_selection_pointer_effect():
    remote, old_oid, old_message = _transferred()

    def changes_after_preflight(historical):
        remote.identity_calls.append(historical)
        return len(remote.identity_calls) == 1

    remote.same_repository = changes_after_preflight
    before_current = remote.current_writes
    with pytest.raises(BetaSourceConflict):
        BetaSourceSelector(remote, "owner/repo").advance("r901", SHA_A)
    # The source effect has happened, but no current-selection marker/write
    # can be attributed when fresh identity evidence conflicts.
    assert remote.refs[BETA_REF] == ("commit", SHA_B)
    assert remote.refs[CURRENT_SELECTION_REF] == ("tag", old_oid)
    assert remote.current_writes == before_current
    assert remote.tags[old_oid]["message"] == old_message
    assert "refs/tags/auto-coder-beta-selection-r901-current-attempt" not in remote.refs


def test_transferred_newer_operation_prevents_first_source_effect():
    remote, old_oid, old_message = _transferred()
    before = remote.pointer_mutations
    result = BetaSourceSelector(remote, "owner/repo").advance("r899", SHA_A)
    assert result.certainty is MutationCertainty.SUPERSEDED
    assert remote.pointer_mutations == before
    assert remote.refs[CURRENT_SELECTION_REF] == ("tag", old_oid)
    assert remote.tags[old_oid]["message"] == old_message


def test_transfer_proof_does_not_authorize_reusing_another_repository_operation():
    remote, old_oid, old_message = _transferred()
    with pytest.raises(BetaSourceConflict, match="different operation"):
        BetaSourceSelector(remote, "owner/repo").advance("r900", None)
    assert remote.refs[CURRENT_SELECTION_REF] == ("tag", old_oid)
    assert remote.tags[old_oid]["message"] == old_message
    assert remote.identity_calls == []
