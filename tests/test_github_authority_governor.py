"""Release only the exact admission reservation for a definitely-unsent effect."""

from dataclasses import replace

import pytest

from auto_coder.github_request_governor import GitHubRequestDeferred, GitHubRequestGovernor
from auto_coder.util.github_request_outcome import DeliveryCertainty, GitHubApiOutcome, GitHubRequestContext, GitHubRequestOutcome, GitHubResponseMetadata, RequestProvenance


def _context(attempt: str) -> GitHubRequestContext:
    return GitHubRequestContext("effect", attempt, "ghapi", "https://api.github.com", "POST", "mutation", "/repos/owner/repo/issues/{id}/comments", "owner/repo")


def _outcome(context: GitHubRequestContext, *, sent: bool = False) -> GitHubRequestOutcome:
    return GitHubRequestOutcome(context, 200 if sent else None, GitHubApiOutcome.SUCCESS if sent else GitHubApiOutcome.REFUSED, RequestProvenance.NETWORK, DeliveryCertainty.HTTP_RESPONSE_RECEIVED if sent else DeliveryCertainty.DEFINITELY_NOT_SENT, GitHubResponseMetadata(), 0.0)


def test_unsent_release_is_exact_and_does_not_charge_mutation_spacing(tmp_path):
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3", monotonic=lambda: 100.0, wall_time=lambda: 1800000000.0)
    first, other, retry = _context("first"), _context("other"), _context("retry")
    try:
        # A guard can fail before the first admission without poisoning storage.
        governor.observe(_outcome(other))
        assert governor.admit(first) is True
        governor.observe(_outcome(other))
        with pytest.raises(GitHubRequestDeferred, match="request_in_flight"):
            governor.admit(retry)
        governor.observe(_outcome(first))
        assert governor.admit(retry) is True
        assert governor._connection.execute("SELECT attempt_id FROM reservations").fetchall() == [("retry",)]
        governor.observe(_outcome(retry, sent=True))
        governor.observe(_outcome(retry))
        assert governor._connection.execute("SELECT attempt_id, resolved FROM reservations").fetchall() == [("retry", 1)]
        with pytest.raises(GitHubRequestDeferred, match="mutation_spacing"):
            governor.admit(other)
    finally:
        governor.close()


def test_unsent_release_never_removes_a_peer_reservation(tmp_path):
    path = tmp_path / "governor.sqlite3"
    first = GitHubRequestGovernor(store_path=path)
    peer = GitHubRequestGovernor(store_path=path)
    request = _context("owned")
    try:
        assert first.admit(request) is True
        with pytest.raises(GitHubRequestDeferred, match="request_in_flight"):
            peer.admit(_context("peer"))
        peer.observe(_outcome(request))
        with pytest.raises(GitHubRequestDeferred, match="request_in_flight"):
            first.admit(_context("later"))
        assert first._connection.execute("SELECT attempt_id, resolved FROM reservations").fetchall() == [("owned", 0)]
        first.observe(_outcome(request))
        assert peer.admit(_context("peer-after-release")) is True
        peer.observe(_outcome(_context("peer-after-release"), sent=True))
    finally:
        first.close()
        peer.close()


def test_local_cache_observation_cannot_release_native_reservation(tmp_path):
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3")
    request = _context("owned")
    try:
        assert governor.admit(request) is True
        governor.observe(replace(_outcome(request), provenance=RequestProvenance.LOCAL_CACHE))
        assert governor._connection.execute("SELECT attempt_id, resolved FROM reservations").fetchall() == [("owned", 0)]
        governor.observe(_outcome(request))
    finally:
        governor.close()
