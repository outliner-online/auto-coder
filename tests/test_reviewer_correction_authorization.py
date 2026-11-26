"""Durable pre-edit authorization at reviewer commit and publication boundaries."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from auto_coder import local_review_repair
from auto_coder.editable_review import resume_pending_review_publication
from auto_coder.local_review_repair import LocalReviewRepairStore, execute_local_review_repair, recover_confirmed_local_review_repair
from auto_coder.review_correction_policy import ReviewCorrectionBasis, ReviewCorrectionBlocker
from tests.test_editable_review import repository  # noqa: F401
from tests.test_editable_review import _git, _request
from tests.test_review_publication_recovery import resume
from tests.test_review_supersession import retained  # noqa: F401

pytestmark = pytest.mark.usefixtures("_use_custom_subprocess_mock")


def _basis(head: str, *, verdict: str = "NEEDS_FIX") -> str:
    return ReviewCorrectionBasis(
        head_sha=head,
        contract_identity="requirements:source-value:v1",
        verdict=verdict,
        blockers=(
            ReviewCorrectionBlocker(
                requirement_ids=("REQ-001",),
                requirement_texts=("The exported source value must equal two.",),
                boundary="source.py:value",
                evidence="The reviewed input exports value = 1.",
                counterexample="Read the exported value from the input head.",
                required_behavior="The value is two.",
                actual_behavior="The value is one.",
                regression="Assert the exported value equals two.",
            ),
        ),
    ).to_json()


def _invalid_basis(head: str, kind: str) -> str:
    if kind == "missing":
        return ""
    if kind == "malformed":
        return "not JSON"
    if kind == "wrong-head":
        return _basis("0" * 40)
    return _basis(head, verdict=kind)


def _forbid_effects(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("unqualified reviewer output must not enter a new commit or push")

    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", forbidden)
    monkeypatch.setattr(local_review_repair, "git_push", forbidden)


def _pending(repository, tmp_path, basis_json):
    root, remote, head = repository
    request = replace(_request(head), expected_push_url=str(remote), review_correction_basis_json=basis_json)
    store = LocalReviewRepairStore(tmp_path / "publication.sqlite3")
    claim = store.admit(request)
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    _git(root, "add", "source.py")
    _git(root, "commit", "-m", "Retained correction candidate")
    candidate = _git(root, "rev-parse", "HEAD")
    assert store.transition(request, claim, "publication_pending", result_sha=candidate)
    return request, store, candidate


def _resume(root, store):
    return resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store, authority_check=lambda *_: True)


def _remove_basis(retained):
    with sqlite3.connect(retained.store.path) as connection:
        connection.execute("UPDATE local_review_repair_attempts SET review_correction_basis_json='' WHERE attempt_id=?", (retained.request.attempt_id,))


def test_claim_retains_basis_across_restart_without_retroactive_replacement(tmp_path):
    request = replace(_request("a" * 40), review_correction_basis_json=_basis("a" * 40))
    store = LocalReviewRepairStore(tmp_path / "claims.sqlite3")
    claim = store.admit(request)
    assert store.mark_invocation_entered(request, claim)
    before = store.get(request)
    restarted = LocalReviewRepairStore(store.path)
    changed_request = replace(request, review_correction_basis_json=_basis(request.head_sha, verdict="PASS"))
    assert not restarted.admit(changed_request).admitted
    assert restarted.get(request) == before
    assert restarted.get(request).review_correction_basis_json == request.review_correction_basis_json


def test_schema_upgrade_keeps_legacy_output_unqualified(tmp_path):
    request = replace(_request("a" * 40), review_correction_basis_json=_basis("a" * 40))
    store = LocalReviewRepairStore(tmp_path / "legacy.sqlite3")
    claim = store.admit(request)
    assert store.transition(request, claim, "indeterminate", reason="retained legacy output", workspace_path="/retained/reviewer-output")
    before = store.get(request)
    with sqlite3.connect(store.path) as connection:
        connection.execute("ALTER TABLE local_review_repair_attempts DROP COLUMN review_correction_basis_json")
    upgraded = LocalReviewRepairStore(store.path)
    assert upgraded.get(request) == replace(before, review_correction_basis_json="")
    assert not upgraded.admit(request).admitted
    assert upgraded.get(request).review_correction_basis_json == ""


@pytest.mark.parametrize("kind", ["missing", "PASS", "INCONCLUSIVE", "wrong-head", "malformed"])
def test_new_reviewer_correction_refuses_before_executor_or_claim(repository, tmp_path, monkeypatch, kind):
    root, remote, head = repository
    request = replace(_request(head), review_correction_basis_json=_invalid_basis(head, kind))
    store = LocalReviewRepairStore(tmp_path / "refused.sqlite3")
    _forbid_effects(monkeypatch)

    def executor(*_args):
        pytest.fail("a review without a pre-edit blocker must not copy or generate corrections")

    outcome = execute_local_review_repair(request, store=store, executor=executor, root=root)
    assert outcome.phase == "not_admitted" and not outcome.executed and not outcome.published
    assert store.get(request) is None
    assert _git(root, "rev-parse", "HEAD") == head
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head


@pytest.mark.parametrize("kind", ["missing", "PASS", "wrong-head"])
def test_restart_refuses_unsent_candidate_using_stored_basis(repository, tmp_path, monkeypatch, kind):
    root, remote, head = repository
    request, store, candidate = _pending(repository, tmp_path, _invalid_basis(head, kind))
    before = store.get(request)
    _forbid_effects(monkeypatch)
    # A caller cannot retrofit a new decision onto previously retained output.
    amended_request = replace(request, review_correction_basis_json=_basis(head))
    restarted = LocalReviewRepairStore(store.path)
    outcome = local_review_repair._resume_publication(amended_request, restarted, restarted.get(request), root)
    assert outcome.phase == "publication_pending" and not outcome.published
    assert "correction" in outcome.reason
    assert restarted.get(request) == before
    assert restarted.get(request).publication_send_state == "not_entered"
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head
    assert _git(root, "rev-parse", "HEAD") == candidate


def test_qualified_candidate_publishes_once_after_restart(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    request, store, candidate = _pending(repository, tmp_path, _basis(head))
    sends = []
    real_push = local_review_repair.git_push

    def push(**kwargs):
        sends.append(kwargs)
        return real_push(**kwargs)

    monkeypatch.setattr(local_review_repair, "git_push", push)
    restarted = LocalReviewRepairStore(store.path)
    outcome = _resume(root, restarted)
    assert outcome.published and outcome.phase == "awaiting_validation"
    assert _git(remote, "rev-parse", "refs/heads/review-head") == candidate
    assert restarted.get(request).review_correction_basis_json == request.review_correction_basis_json
    assert _resume(root, LocalReviewRepairStore(store.path)) is None
    assert len(sends) == 1 and sends[0]["expected_remote_sha"] == head


@pytest.mark.parametrize("send_state", ["entered", "unknown"])
def test_legacy_send_can_reconcile_observed_candidate_without_new_push(repository, tmp_path, monkeypatch, send_state):
    root, remote, _head = repository
    request, store, candidate = _pending(repository, tmp_path, "")
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE local_review_repair_attempts SET publication_send_state=? WHERE attempt_id=?", (send_state, request.attempt_id))
    _git(root, "push", "origin", f"{candidate}:refs/heads/review-head")
    _forbid_effects(monkeypatch)
    outcome = _resume(root, LocalReviewRepairStore(store.path))
    assert outcome.published and outcome.phase == "awaiting_validation"
    assert store.get(request).review_correction_basis_json == ""
    assert store.get(request).publication_send_state == send_state
    assert _git(remote, "rev-parse", "refs/heads/review-head") == candidate


def test_legacy_identity_failure_cannot_enter_recovery_commit(retained, monkeypatch):
    _remove_basis(retained)
    original = retained.store.get(retained.request)
    _forbid_effects(monkeypatch)
    outcome = resume(retained, store=LocalReviewRepairStore(retained.store.path))
    assert outcome.phase == "indeterminate" and "correction_basis_unavailable" in outcome.reason
    assert retained.store.get(retained.request) == original
    assert retained.store.get_commit_recovery(retained.request) is None
    assert _git(retained.workspace, "rev-parse", "HEAD") == retained.original_head
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == retained.original_head


def test_legacy_completed_backend_cannot_enter_recovery_commit(retained, monkeypatch):
    _remove_basis(retained)
    with sqlite3.connect(retained.store.path) as connection:
        connection.execute("UPDATE local_review_repair_attempts SET phase='executing' WHERE attempt_id=?", (retained.request.attempt_id,))
    original = retained.store.get(retained.request)
    _forbid_effects(monkeypatch)
    outcome = recover_confirmed_local_review_repair(retained.request, "correction complete", store=retained.store, allowance_authority=MagicMock())
    assert outcome.phase == "indeterminate" and "commit refused" in outcome.reason
    assert retained.store.get(retained.request) == original
    assert _git(retained.workspace, "rev-parse", "HEAD") == retained.original_head


def test_legacy_entered_recovery_observes_candidate_but_does_not_send(retained, monkeypatch):
    real_finish = retained.store.finish_commit_recovery

    def interrupted(*args, **kwargs):
        if kwargs.get("result_sha"):
            raise RuntimeError("process exit before candidate checkpoint")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(retained.store, "finish_commit_recovery", interrupted)
    assert resume(retained).phase == "indeterminate"
    candidate = _git(retained.workspace, "rev-parse", "HEAD")
    assert candidate != retained.original_head
    with sqlite3.connect(retained.store.path) as connection:
        original_json = connection.execute("SELECT original_record_json FROM local_review_commit_recoveries WHERE attempt_id=?", (retained.request.attempt_id,)).fetchone()[0]
        legacy_record = json.loads(original_json)
        legacy_record.pop("review_correction_basis_json")
        connection.execute("UPDATE local_review_commit_recoveries SET original_record_json=? WHERE attempt_id=?", (json.dumps(legacy_record), retained.request.attempt_id))
        connection.execute("ALTER TABLE local_review_repair_attempts DROP COLUMN review_correction_basis_json")
    _forbid_effects(monkeypatch)
    restarted = LocalReviewRepairStore(retained.store.path)
    outcome = resume(retained, store=restarted)
    assert outcome.phase == "publication_pending" and not outcome.published
    assert restarted.get(retained.request).result_sha == candidate
    assert restarted.get(retained.request).review_correction_basis_json == ""
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == retained.original_head


def test_legacy_output_can_still_be_superseded_by_independent_work(retained, monkeypatch):
    _remove_basis(retained)
    replacement = retained.advance()
    _forbid_effects(monkeypatch)
    outcome = retained.resume(store=LocalReviewRepairStore(retained.store.path))
    assert outcome.phase == "superseded" and not outcome.published
    assert retained.store.get(retained.request).review_correction_basis_json == ""
    assert retained.store.get_supersession(retained.request).replacement_head == replacement
    assert retained.workspace.is_dir()


def test_unrelated_local_correction_remains_eligible_without_reviewer_basis(repository, tmp_path):
    root, remote, head = repository
    request = replace(_request(head), feedback_identities=("review-thread:123",), review_correction_basis_json="")
    store = LocalReviewRepairStore(tmp_path / "thread.sqlite3")

    def executor(_request, workspace):
        Path(workspace, "source.py").write_text("value = 2\n", encoding="utf-8")
        return "Corrected the explicit review thread."

    outcome = execute_local_review_repair(request, store=store, executor=executor, root=root, allow_commit_repair=False)
    assert outcome.published and outcome.phase == "awaiting_validation"
    assert _git(remote, "show", "refs/heads/review-head:source.py") == "value = 2"
    assert store.get(request).review_correction_basis_json == ""
