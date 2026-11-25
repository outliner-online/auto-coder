"""Tests for PR-scoped persistent adversarial reviewer sessions."""

from dataclasses import replace

import pytest

from auto_coder.reviewer_session_registry import RecoveredFileEvidence, ReviewerSession, ReviewerSessionRegistry, TestOracleGap


def test_sessions_are_isolated_by_pr_and_backend_and_survive_reload(tmp_path):
    path = tmp_path / "sessions.json"
    registry = ReviewerSessionRegistry(path)
    claude_123 = ReviewerSession("owner/repo", 123, "claude-review", "claude", "opus", "claude-s1", "sha-a")
    claude_124 = ReviewerSession("owner/repo", 124, "claude-review", "claude", "opus", "claude-s2", "sha-b")
    codex_123 = ReviewerSession("owner/repo", 123, "codex-review", "codex", "gpt", "codex-s1", "sha-c")

    registry.save(claude_123)
    registry.save(claude_124)
    registry.save(codex_123)
    reloaded = ReviewerSessionRegistry(path)

    assert reloaded.get("owner/repo", 123, "claude-review", "claude", "opus") == claude_123
    assert reloaded.get("owner/repo", 124, "claude-review", "claude", "opus") == claude_124
    assert reloaded.get("owner/repo", 123, "codex-review", "codex", "gpt") == codex_123
    assert reloaded.get("owner/repo", 123, "codex-review", "codex", "opus") is None


def test_remove_pr_removes_all_of_only_that_pr(tmp_path):
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    registry.save(ReviewerSession("owner/repo", 123, "claude", "claude", "opus", "s1", "a"))
    registry.save(ReviewerSession("owner/repo", 123, "codex", "codex", "gpt", "s2", "b"))
    registry.save(ReviewerSession("owner/repo", 124, "claude", "claude", "opus", "s3", "c"))

    registry.remove_pr("owner/repo", 123)

    assert registry.get("owner/repo", 123, "claude", "claude", "opus") is None
    assert registry.get("owner/repo", 123, "codex", "codex", "gpt") is None
    assert registry.get("owner/repo", 124, "claude", "claude", "opus") is not None


def test_material_test_oracle_gap_identity_and_scope_survive_reload(tmp_path):
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    gap = TestOracleGap(
        gap_id="TOG-123",
        requirement_id="REQ-001",
        authoritative_boundary="GridMutation.apply_candidate",
        invariant="Rejection preserves stored state and revision",
        status="RESOLVED",
        resolution_evidence="The focused regression protects the boundary.",
        resolution_head_sha="sha-b",
        historical_resolution_head_sha="sha-a",
        historical_resolution_evidence="The earlier focused regression protected the boundary.",
    )
    session = ReviewerSession("owner/repo", 123, "codex", "codex", "gpt", "s1", "sha-a", [gap])

    registry.save(session)

    assert ReviewerSessionRegistry(registry.path).get("owner/repo", 123, "codex", "codex", "gpt") == session


def test_same_head_recovered_file_evidence_survives_reload(tmp_path):
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    evidence = RecoveredFileEvidence(
        path=".github/workflows/release.yml",
        source="repository inspection",
        status="RECOVERED",
        evidence="Inspected the current-head workflow",
        requirement_ids=["REQ-001"],
        identity_version=1,
        change_identity="change-a",
        requirement_manifest_identity="manifest-a",
        origin_head_sha="sha-a",
        origin_validation_snapshot="snapshot-a",
        last_consumed_head_sha="sha-b",
        disposition="REUSED_EQUIVALENT",
    )
    session = ReviewerSession(
        repository="owner/repo",
        pr_number=123,
        backend_name="codex",
        backend_type="codex",
        model_name="gpt",
        session_id="s1",
        last_head_sha="sha-a",
        evidence_head_sha="sha-a",
        recovered_file_evidence=[evidence],
    )

    registry.save(session)

    assert ReviewerSessionRegistry(registry.path).get("owner/repo", 123, "codex", "codex", "gpt") == session


def test_invalidate_transport_preserves_lifecycle_evidence_and_other_prs(tmp_path):
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    gap = TestOracleGap(gap_id="TOG-123", requirement_id="#99/REQ-001", authoritative_boundary="apply_candidate", invariant="Rejected changes preserve state", status="OPEN")
    evidence = RecoveredFileEvidence(path="sample.py", evidence="Inspected retained source", origin_head_sha="sha-a", origin_validation_snapshot="snapshot-a", requirement_ids=["#99/REQ-001"])
    session = ReviewerSession(
        repository="owner/repo",
        pr_number=123,
        backend_name="codex",
        backend_type="codex",
        model_name="gpt",
        session_id="fixing-session",
        last_head_sha="sha-a",
        test_oracle_gaps=[gap],
        recovered_file_evidence=[evidence],
        evidence_head_sha="sha-a",
        evidence_validation_snapshot="snapshot-a",
    )
    other_backend = replace(session, backend_name="claude", backend_type="claude", model_name="opus", session_id="prior-claude-session")
    peer_pr = replace(session, pr_number=124, session_id="peer-session")
    peer_repository = replace(session, repository="other/repo", session_id="other-repository-session")
    for retained in (session, other_backend, peer_pr, peer_repository):
        registry.save(retained)

    registry.invalidate_transport_for_pr("owner/repo", 123)
    registry.invalidate_transport_for_pr("owner/repo", 123)

    reloaded = ReviewerSessionRegistry(registry.path)
    assert reloaded.get("owner/repo", 123, "codex", "codex", "gpt") == replace(session, session_id="")
    assert reloaded.get("owner/repo", 123, "claude", "claude", "opus") == replace(other_backend, session_id="")
    assert reloaded.get("owner/repo", 124, "codex", "codex", "gpt") == peer_pr
    assert reloaded.get("other/repo", 123, "codex", "codex", "gpt") == peer_repository

    independent = replace(session, session_id="independent-session", last_head_sha="sha-b")
    reloaded.save(independent)
    assert ReviewerSessionRegistry(registry.path).get("owner/repo", 123, "codex", "codex", "gpt") == independent


def test_invalidate_transport_without_retained_sessions_does_not_create_state(tmp_path):
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    registry.invalidate_transport_for_pr("owner/repo", 123)
    assert not registry.path.exists()


def test_transport_invalidation_write_failure_preserves_durable_evidence(tmp_path, monkeypatch):
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    session = ReviewerSession("owner/repo", 123, "codex", "codex", "gpt", "fixing-session", "sha-a", [TestOracleGap(gap_id="TOG-123")])
    registry.save(session)
    original = registry.path.read_bytes()

    def fail_replace(*_args):
        raise OSError("retained state unavailable")

    monkeypatch.setattr("auto_coder.reviewer_session_registry.os.replace", fail_replace)
    with pytest.raises(OSError, match="retained state unavailable"):
        registry.invalidate_transport_for_pr("owner/repo", 123)

    assert registry.path.read_bytes() == original
    assert registry.get("owner/repo", 123, "codex", "codex", "gpt") == session
