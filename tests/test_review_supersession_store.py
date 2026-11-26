"""Durable single-disposition fencing for retained reviewer corrections."""

import multiprocessing
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from auto_coder.local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore


def _request():
    return LocalReviewRepairRequest(
        repository="owner/repo",
        pr_number=42,
        head_repository="owner/repo",
        head_ref="review-head",
        head_sha="a" * 40,
        feedback_identities=("review-output:ordinary:attempt-7",),
        prompt="",
        expected_push_url="https://github.com/owner/repo.git",
    )


def _retained(tmp_path):
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    request = _request()
    claim = store.admit(request)
    assert claim.admitted
    assert store.transition(request, claim, "executing", workspace_path=str(tmp_path / "retained"))
    assert store.mark_invocation_entered(request, claim)
    assert store.checkpoint_pre_push(request, claim, '{"output":"retained"}')
    assert store.transition(request, claim, "indeterminate", reason="Author identity unknown", workspace_path=str(tmp_path / "retained"))
    return store, request, claim, store.get(request)


def test_pre_push_checkpoint_is_immutable_and_fenced(tmp_path):
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    request = _request()
    claim = store.admit(request)
    assert not store.checkpoint_pre_push(request, claim, "first")
    assert store.mark_invocation_entered(request, claim)
    assert store.checkpoint_pre_push(request, claim, "first")
    saved = store.get(request)
    assert store.checkpoint_pre_push(request, claim, "first")
    assert store.get(request) == saved
    assert not store.checkpoint_pre_push(request, claim, "second")
    assert not store.checkpoint_pre_push(request, replace(claim, incarnation=claim.incarnation + 1), "first")
    assert not store.checkpoint_pre_push(replace(request, pr_number=43), claim, "first")
    assert not store.checkpoint_pre_push(replace(request, head_ref="other"), claim, "first")
    assert not store.checkpoint_pre_push(replace(request, expected_push_url="other"), claim, "first")
    assert store.get(request) == saved


def test_publication_entry_remains_a_blocker_after_later_transition(tmp_path):
    store, request, claim, _record = _retained(tmp_path)
    assert store.transition(request, claim, "publication_pending", result_sha="b" * 40)
    assert store.transition(request, claim, "indeterminate", reason="lost response")
    record = store.get(request)
    assert record.result_sha == ""
    assert record.publication_started
    assert not store.settle_superseded(request, record, "c" * 40, "evidence", lambda: pytest.fail("publication entry must refuse before confirmation"))
    assert store.get(request) == record
    assert store.get_supersession(request) is None


def test_one_atomic_disposition_preserves_attempt_and_releases_only_old_blocker(tmp_path):
    store, request, claim, before = _retained(tmp_path)
    confirmations = []
    assert store.settle_superseded(request, before, "b" * 40, "evidence", lambda: confirmations.append(True) or True)
    disposition = store.get_supersession(request)
    assert disposition is not None
    assert (disposition.attempt_id, disposition.incarnation, disposition.replacement_head, disposition.evidence_json) == (request.attempt_id, claim.incarnation, "b" * 40, "evidence")
    assert store.get(request) == replace(before, phase="superseded", updated_at=disposition.settled_at)
    assert not store.transition(request, claim, "executing")
    assert not store.transition(request, claim, "awaiting_validation", result_sha="b" * 40)
    assert store.get_unfinished(request, feedback_prefix="review-output:") is None
    assert not store.admit(request).admitted
    restarted = LocalReviewRepairStore(store.path)
    assert restarted.settle_superseded(request, before, "b" * 40, "evidence", lambda: pytest.fail("settled reentry must not repeat evidence work"))
    assert restarted.get_supersession(request) == disposition
    assert confirmations == [True]
    newer_request = replace(request, head_sha="b" * 40)
    assert restarted.admit(newer_request).admitted
    assert restarted.get(request).phase == "superseded"
    assert restarted.get(newer_request).phase == "executing"


def test_concurrent_evaluation_persists_one_disposition(tmp_path):
    store, request, _claim, record = _retained(tmp_path)
    barrier = Barrier(2)
    confirmations = []

    def settle():
        competing_store = LocalReviewRepairStore(store.path)
        barrier.wait(timeout=10)
        return competing_store.settle_superseded(request, record, "b" * 40, "evidence", lambda: confirmations.append(True) or True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: settle(), range(2)))
    assert results == [True, True]
    assert confirmations == [True]
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_review_repair_supersessions").fetchone() == (1,)
    assert store.get(request).phase == "superseded"


@pytest.mark.parametrize("field,value", [("incarnation", 7), ("owner_pid", 7), ("owner_boot_id", "changed"), ("owner_start_ticks", 7), ("workspace_path", "/different"), ("pre_push_evidence_json", "changed"), ("updated_at", 0.0)])
def test_changed_attempt_evidence_preserves_blocker(tmp_path, field, value):
    store, request, _claim, record = _retained(tmp_path)
    with store._connect() as connection:
        connection.execute(f"UPDATE local_review_repair_attempts SET {field}=?", (value,))
    changed = store.get(request)
    assert not store.settle_superseded(request, record, "b" * 40, "evidence", lambda: pytest.fail("stale attempt must refuse before confirmation"))
    assert store.get(request) == changed
    assert store.get_supersession(request) is None


@pytest.mark.parametrize("field,value", [("repository", "other/repo"), ("head_repository", "other/repo"), ("pr_number", 43), ("head_sha", "c" * 40), ("head_ref", "other-branch"), ("feedback_identities", ("review-output:other",)), ("expected_push_url", "https://github.com/other/repo.git")])
def test_changed_request_target_preserves_blocker(tmp_path, field, value):
    store, request, _claim, record = _retained(tmp_path)
    assert not store.settle_superseded(replace(request, **{field: value}), record, "b" * 40, "evidence", lambda: pytest.fail("mismatched target must refuse before confirmation"))
    assert store.get(request) == record
    assert store.get_supersession(request) is None


def test_failed_or_raising_confirmation_leaves_no_partial_disposition(tmp_path):
    store, request, claim, record = _retained(tmp_path)
    assert not store.transition(request, claim, "superseded")
    assert not store.settle_superseded(request, record, "b" * 40, "evidence", lambda: False)
    assert store.get(request) == record
    assert store.get_supersession(request) is None

    def interrupted():
        raise RuntimeError("fresh evidence unavailable")

    with pytest.raises(RuntimeError, match="fresh evidence unavailable"):
        store.settle_superseded(request, record, "b" * 40, "evidence", interrupted)
    assert store.get(request) == record
    assert store.get_supersession(request) is None
    assert store.settle_superseded(request, record, "b" * 40, "evidence", lambda: True)


def test_conflicting_settled_evidence_cannot_replace_disposition(tmp_path):
    store, request, _claim, record = _retained(tmp_path)
    assert store.settle_superseded(request, record, "b" * 40, "evidence", lambda: True)
    settled = store.get(request)
    disposition = store.get_supersession(request)
    assert not store.settle_superseded(request, record, "c" * 40, "evidence", lambda: pytest.fail("conflict must not re-evaluate"))
    assert not store.settle_superseded(request, record, "b" * 40, "different", lambda: pytest.fail("conflict must not re-evaluate"))
    assert store.get(request) == settled
    assert store.get_supersession(request) == disposition


def test_dead_owner_preserves_checkpointed_uncommitted_output(tmp_path, _use_custom_subprocess_mock):
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(root, *args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()

    git(repository, "init")
    git(repository, "config", "user.name", "Recovery Test")
    git(repository, "config", "user.email", "recovery@example.com")
    (repository / "source.py").write_text("value = 1\n")
    git(repository, "add", "source.py")
    git(repository, "commit", "-m", "Initial source")
    head = git(repository, "rev-parse", "HEAD")
    workspace = tmp_path / "retained"
    git(repository, "worktree", "add", "--detach", str(workspace), head)
    (workspace / "source.py").write_text("value = 2\n")
    git(workspace, "add", "source.py")
    request = replace(_request(), head_sha=head)
    database = tmp_path / "repairs.sqlite3"

    def checkpoint_and_exit():
        store = LocalReviewRepairStore(database)
        claim = store.admit(request)
        assert store.transition(request, claim, "executing", workspace_path=str(workspace))
        assert store.mark_invocation_entered(request, claim)
        assert store.checkpoint_pre_push(request, claim, '{"output":"retained"}')
        os._exit(17)

    process = multiprocessing.get_context("fork").Process(target=checkpoint_and_exit)
    process.start()
    process.join(10)
    try:
        assert process.exitcode == 17
        restarted = LocalReviewRepairStore(database)
        before = restarted.get(request)
        assert before.phase == "executing"
        retained = restarted.get_unfinished(request, feedback_prefix="review-output:")
        assert retained.phase == "indeterminate"
        assert retained.workspace_path == str(workspace)
        assert retained.pre_push_evidence_json == before.pre_push_evidence_json
        assert retained.invocation_entered
        assert retained.result_sha == "" and not retained.publication_started
        assert (workspace / "source.py").read_text() == "value = 2\n"
        assert git(workspace, "rev-parse", "HEAD") == head
        assert git(workspace, "diff", "--cached", "--name-only") == "source.py"
        assert not restarted.admit(request).admitted
        assert not restarted.admit(replace(request, head_sha="b" * 40)).admitted
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
