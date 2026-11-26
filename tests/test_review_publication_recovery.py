"""Real Git recovery of settled reviewer output after a local identity failure."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from auto_coder import editable_review, local_review_repair
from auto_coder.controller_git_identity import ControllerGitIdentity
from auto_coder.editable_review import capture_review_workspace, resume_pending_review_publication
from auto_coder.local_review_repair import LocalReviewRepairStore
from auto_coder.review_supersession import ReviewSupersessionTarget
from tests.test_editable_review import repository  # noqa: F401
from tests.test_editable_review import _git, _publish, _request
from tests.test_review_supersession import retained  # noqa: F401

pytestmark = pytest.mark.usefixtures("_use_custom_subprocess_mock")


IDENTITY = ControllerGitIdentity("owner/repo", "controller-app[bot]", 98765, 123)


def resume(retained, *, store=None, current_target=None, authority_check=None, identity_provider=None):
    return resume_pending_review_publication(
        repository="owner/repo",
        pr_number=42,
        root=str(retained.root),
        store=store or retained.store,
        current_target=current_target or retained.target,
        authority_check=authority_check or (lambda *_: True),
        commit_identity_provider=identity_provider or (lambda: IDENTITY),
    )


def forbid_reviewer(monkeypatch):
    monkeypatch.setattr(editable_review, "execute_local_review_repair", lambda *_args, **_kwargs: pytest.fail("retained reviewer output must not be copied or modeled again"))
    import auto_coder.git_branch as git_branch

    monkeypatch.setattr(git_branch, "try_llm_dprint_fallback", lambda *_args, **_kwargs: pytest.fail("controller publication must not invoke a hidden editor"))


@pytest.mark.parametrize("retained", ["legacy", "checkpointed"], indirect=True)
def test_restart_recovers_exact_retained_output_once_with_controller_bot_identity(retained, monkeypatch):
    original = retained.store.get(retained.request)
    caller_config = (retained.root / ".git" / "config").read_bytes()
    caller_head = _git(retained.root, "rev-parse", "HEAD")
    forbid_reviewer(monkeypatch)
    calls = []
    real_commit = local_review_repair.git_commit_with_retry
    real_push = local_review_repair.git_push

    def commit(*args, **kwargs):
        calls.append("commit")
        assert kwargs["allow_repair"] is False
        return real_commit(*args, **kwargs)

    def push(**kwargs):
        calls.append("push")
        record = retained.store.get(retained.request)
        assert record.phase == "publication_pending" and record.result_sha
        assert kwargs["expected_remote_sha"] == retained.original_head
        return real_push(**kwargs)

    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", commit)
    monkeypatch.setattr(local_review_repair, "git_push", push)
    outcome = resume(retained, store=LocalReviewRepairStore(retained.store.path))
    assert (outcome.phase, outcome.executed, outcome.published) == ("awaiting_validation", True, True)
    candidate = _git(retained.remote, "rev-parse", "refs/heads/review-head")
    assert candidate != retained.original_head
    assert _git(retained.remote, "show", "-s", "--format=%an|%ae|%cn|%ce", candidate) == "controller-app[bot]|98765+controller-app[bot]@users.noreply.github.com|controller-app[bot]|98765+controller-app[bot]@users.noreply.github.com"
    assert _git(retained.remote, "rev-parse", f"{candidate}^") == retained.original_head
    assert _git(retained.remote, "show", f"{candidate}:source.py") == "value = 2"
    assert _git(retained.remote, "show", f"{candidate}:added.txt") == "entire retained correction"
    assert "removed.txt" not in _git(retained.remote, "ls-tree", "-r", candidate)
    assert "100755 blob" in _git(retained.remote, "ls-tree", candidate, "source.py")
    assert "120000 blob" in _git(retained.remote, "ls-tree", candidate, "latest.py")
    message = _git(retained.remote, "show", "-s", "--format=%B", candidate)
    assert "Review-Origin: https://github.com/owner/repo/pull/42" in message
    assert "Review-Output: ordinary:attempt-7" in message
    journal = retained.store.get_commit_recovery(retained.request)
    assert journal is not None and journal.result_sha == candidate
    proof = json.loads(journal.original_record_json)
    assert proof["reason"] == original.reason and proof["workspace_path"] == original.workspace_path
    assert proof["incarnation"] == original.incarnation and proof["attempt_id"] == original.attempt_id
    assert calls == ["commit", "push"]
    assert resume(retained, store=LocalReviewRepairStore(retained.store.path)) is None
    assert calls == ["commit", "push"]
    assert (retained.root / ".git" / "config").read_bytes() == caller_config
    assert _git(retained.root, "rev-parse", "HEAD") == caller_head


def test_concurrent_resumption_enters_one_commit_and_one_push(retained, monkeypatch):
    forbid_reviewer(monkeypatch)
    entered, release = Event(), Event()
    real_commit = local_review_repair.git_commit_with_retry
    real_push = local_review_repair.git_push
    effects = []

    def commit(*args, **kwargs):
        effects.append("commit")
        entered.set()
        assert release.wait(timeout=10)
        return real_commit(*args, **kwargs)

    def push(**kwargs):
        effects.append("push")
        return real_push(**kwargs)

    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", commit)
    monkeypatch.setattr(local_review_repair, "git_push", push)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(resume, retained)
        assert entered.wait(timeout=10)
        second = workers.submit(resume, retained).result(timeout=10)
        assert second.phase == "indeterminate" and "another evaluator" in second.reason
        release.set()
        assert first.result(timeout=10).published
    assert effects == ["commit", "push"]
    assert resume(retained) is None


def test_lost_candidate_checkpoint_reconciles_without_second_commit(retained, monkeypatch):
    forbid_reviewer(monkeypatch)
    real_finish = retained.store.finish_commit_recovery

    def interrupted(*args, **kwargs):
        if kwargs.get("result_sha"):
            raise RuntimeError("process exit before candidate checkpoint")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(retained.store, "finish_commit_recovery", interrupted)
    outcome = resume(retained)
    assert outcome.phase == "indeterminate" and not outcome.published
    candidate = _git(retained.workspace, "rev-parse", "HEAD")
    assert candidate != retained.original_head
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == retained.original_head
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", lambda *_args, **_kwargs: pytest.fail("a recovered candidate must not be committed again"))
    restarted = LocalReviewRepairStore(retained.store.path)
    recovered = resume(retained, store=restarted)
    assert recovered.published and recovered.phase == "awaiting_validation"
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == candidate


def test_interrupted_commit_entry_without_candidate_is_never_replayed(retained, monkeypatch):
    def interrupted(*args, **kwargs):
        raise RuntimeError("commit entry interrupted")

    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", interrupted)
    assert resume(retained).phase == "indeterminate"
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", lambda *_args, **_kwargs: pytest.fail("an uncertain commit entry must not be repeated"))
    outcome = resume(retained, store=LocalReviewRepairStore(retained.store.path))
    assert outcome.phase == "indeterminate" and "commit_entry_unresolved" in outcome.reason
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == retained.original_head
    assert retained.store.get(retained.request).phase == "indeterminate"


@pytest.mark.parametrize("damage", ["unstaged", "restaged-equivalent", "missing-owner", "publication-entered", "unknown-failure"])
def test_uncertain_or_modified_output_preserves_original_record_without_commit(retained, monkeypatch, damage):
    if damage in {"unstaged", "restaged-equivalent"}:
        (retained.workspace / "source.py").write_text("value = 2\n" if damage == "restaged-equivalent" else "value = 999\n")
        if damage == "restaged-equivalent":
            _git(retained.workspace, "add", "source.py")
    else:
        column, value = {
            "missing-owner": ("owner_pid", None),
            "publication-entered": ("publication_started", 1),
            "unknown-failure": ("reason", "backend transport outcome is unknown"),
        }[damage]
        with sqlite3.connect(retained.store.path) as connection:
            connection.execute(f"UPDATE local_review_repair_attempts SET {column}=? WHERE attempt_id=?", (value, retained.request.attempt_id))
    before = retained.store.get(retained.request)
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", lambda *_args, **_kwargs: pytest.fail("unproven output must not enter commit"))
    outcome = resume(retained)
    assert outcome.phase == "indeterminate" and not outcome.published
    assert retained.store.get(retained.request) == before
    assert retained.store.get_commit_recovery(retained.request) is None
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == retained.original_head


@pytest.mark.parametrize("failure", ["foreign-identity", "missing-identity", "authority-refused", "remote-moved", "owner-moved"])
def test_current_identity_target_and_owner_authority_are_required(retained, monkeypatch, failure):
    old_target = retained.target()
    identity_provider = lambda: IDENTITY
    authority_check = lambda *_: True
    target = lambda: old_target
    if failure == "foreign-identity":
        identity_provider = lambda: replace(IDENTITY, repository="other/repo")
    elif failure == "missing-identity":

        def identity_provider():
            raise RuntimeError("Controller App installation unavailable")

    elif failure == "authority-refused":
        authority_check = lambda *_: False
    elif failure == "remote-moved":
        retained.advance("different")
    elif failure == "owner-moved":

        def target():
            with sqlite3.connect(retained.store.path) as connection:
                connection.execute("UPDATE local_review_repair_attempts SET incarnation=incarnation+1 WHERE attempt_id=?", (retained.request.attempt_id,))
            return old_target

    before = retained.store.get(retained.request)
    remote_before = _git(retained.remote, "rev-parse", "refs/heads/review-head")
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", lambda *_args, **_kwargs: pytest.fail("missing current authority must not enter commit"))
    outcome = resume(retained, current_target=target, authority_check=authority_check, identity_provider=identity_provider)
    assert outcome.phase == "indeterminate" and not outcome.published
    if failure != "owner-moved":
        assert retained.store.get(retained.request) == before
    assert retained.store.get_commit_recovery(retained.request) is None
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == remote_before


def test_unavailable_identity_retains_new_output_and_recovers_without_personal_fallback(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    (root / "source.py").write_text("value = 2\n")
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")

    def unavailable():
        raise RuntimeError("Controller App installation unavailable")

    result = _publish(snapshot, store, commit_identity_provider=unavailable)
    assert result.phase == "indeterminate" and not result.published
    retained_record = store.get(_request(head))
    assert retained_record.pre_push_evidence_json and "Controller Git identity unavailable" in retained_record.reason
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head
    forbid_reviewer(monkeypatch)
    outcome = resume_pending_review_publication(
        repository="owner/repo",
        pr_number=42,
        root=str(root),
        store=store,
        current_target=lambda: ReviewSupersessionTarget("owner/repo", 42, "owner/repo", "review-head", head),
        authority_check=lambda *_: True,
        commit_identity_provider=lambda: IDENTITY,
    )
    assert outcome.published and outcome.phase == "awaiting_validation"
    candidate = _git(remote, "rev-parse", "refs/heads/review-head")
    assert _git(remote, "show", "-s", "--format=%an|%cn", candidate) == "controller-app[bot]|controller-app[bot]"
    assert store.get_commit_recovery(_request(head)).original_record_json


def test_uncertain_push_response_reuses_candidate_without_another_commit_or_push(retained, monkeypatch):
    from auto_coder.utils import CommandResult

    real_push = local_review_repair.git_push
    pushes = []

    def lost_response(**kwargs):
        pushes.append(kwargs)
        assert real_push(**kwargs).success
        return CommandResult(False, "", "response lost", 1)

    monkeypatch.setattr(local_review_repair, "git_push", lost_response)
    first = resume(retained)
    assert first.phase == "publication_pending" and not first.published
    candidate = _git(retained.remote, "rev-parse", "refs/heads/review-head")
    assert candidate != retained.original_head
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", lambda *_args, **_kwargs: pytest.fail("uncertain push must not recommit"))
    second = resume(retained, store=LocalReviewRepairStore(retained.store.path))
    assert second.phase == "awaiting_validation" and second.published
    assert len(pushes) == 1
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == candidate


def test_successful_recovery_hook_cannot_publish_a_changed_source_tree(retained):
    hook = retained.root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'value = 999\\n' > source.py\ngit add source.py\nexit 0\n")
    hook.chmod(0o755)
    original = retained.store.get(retained.request)
    outcome = resume(retained)
    assert outcome.phase == "indeterminate" and "candidate_mismatch" in outcome.reason
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == retained.original_head
    assert retained.store.get(retained.request) == original
    journal = retained.store.get_commit_recovery(retained.request)
    assert journal.phase == "started" and not journal.result_sha
    hook.unlink()
    assert "candidate_mismatch" in resume(retained).reason


def test_failed_hook_checkpoint_is_not_reclassified_as_identity_failure(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    (root / "source.py").write_text("value = 2\n")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'value = 999\\n' > source.py\ngit add source.py\necho 'formatter changed source' >&2\nexit 1\n")
    hook.chmod(0o755)
    store = LocalReviewRepairStore(tmp_path / "hook.sqlite3")
    result = _publish(snapshot, store)
    assert result.phase == "indeterminate" and not result.published
    record = store.get(_request(head))
    assert record.pre_push_evidence_json and "formatter changed source" in record.reason
    hook.unlink()
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", lambda *_args, **_kwargs: pytest.fail("hook-mutated output must not be committed as a settled identity failure"))
    outcome = resume_pending_review_publication(
        repository="owner/repo",
        pr_number=42,
        root=str(root),
        store=store,
        current_target=lambda: ReviewSupersessionTarget("owner/repo", 42, "owner/repo", "review-head", head),
        authority_check=lambda *_: True,
        commit_identity_provider=lambda: IDENTITY,
    )
    assert outcome.phase == "indeterminate" and "pre_push_failure_unproven" in outcome.reason
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head
    assert store.get(_request(head)) == record


def test_entered_lost_push_with_stale_or_unavailable_readback_never_sends_again(retained, monkeypatch):
    from auto_coder.utils import CommandResult

    real_push = local_review_repair.git_push
    real_remote_head = local_review_repair._remote_head
    sends = []

    def lost_response(**kwargs):
        before_send = retained.store.get(retained.request)
        assert before_send.phase == "publication_pending"
        assert before_send.publication_send_state == "entered"
        sends.append(kwargs)
        assert real_push(**kwargs).success
        return CommandResult(False, "", "transport response lost after send entry", 1)

    monkeypatch.setattr(local_review_repair, "git_push", lost_response)
    first = resume(retained)
    assert first.phase == "publication_pending" and not first.published
    candidate = _git(retained.remote, "rev-parse", "refs/heads/review-head")
    assert candidate != retained.original_head
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", lambda *_args, **_kwargs: pytest.fail("unresolved publication must not recommit"))
    for observation in [(True, retained.original_head), (False, "remote temporarily unavailable")]:
        monkeypatch.setattr(local_review_repair, "_remote_head", lambda *_args, result=observation: result)
        restarted = LocalReviewRepairStore(retained.store.path)
        pending = resume(retained, store=restarted)
        assert pending.phase == "publication_pending" and not pending.published
        assert restarted.get(retained.request).publication_send_state == "entered"
        assert len(sends) == 1
    monkeypatch.setattr(local_review_repair, "_remote_head", real_remote_head)
    settled = resume(retained, store=LocalReviewRepairStore(retained.store.path))
    assert settled.phase == "awaiting_validation" and settled.published
    assert len(sends) == 1
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == candidate


def test_legacy_pending_candidate_does_not_guess_that_no_send_was_entered(retained, monkeypatch):
    from auto_coder.utils import CommandResult

    monkeypatch.setattr(local_review_repair, "git_push", lambda **_kwargs: CommandResult(False, "", "transport response lost", 1))
    assert resume(retained).phase == "publication_pending"
    pending = retained.store.get(retained.request)
    assert pending.result_sha and pending.publication_send_state == "entered"
    with sqlite3.connect(retained.store.path) as connection:
        connection.execute("ALTER TABLE local_review_repair_attempts DROP COLUMN publication_send_state")
    migrated = LocalReviewRepairStore(retained.store.path)
    assert migrated.get(retained.request).publication_send_state == "unknown"
    monkeypatch.setattr(local_review_repair, "git_push", lambda **_kwargs: pytest.fail("unknown legacy send disposition must not resend"))
    outcome = resume(retained, store=migrated)
    assert outcome.phase == "publication_pending" and "unknown" in outcome.reason
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == retained.original_head


def test_legacy_recovery_journal_survives_send_state_schema_addition(retained, monkeypatch):
    real_finish = retained.store.finish_commit_recovery

    def interrupted(*args, **kwargs):
        if kwargs.get("result_sha"):
            raise RuntimeError("exit before candidate checkpoint")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(retained.store, "finish_commit_recovery", interrupted)
    first = resume(retained)
    assert first.phase == "indeterminate" and not first.published
    candidate = _git(retained.workspace, "rev-parse", "HEAD")
    assert candidate != retained.original_head
    journal = retained.store.get_commit_recovery(retained.request)
    old_record = json.loads(journal.original_record_json)
    old_record.pop("publication_send_state")
    old_json = json.dumps(old_record, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(retained.store.path) as connection:
        connection.execute("UPDATE local_review_commit_recoveries SET original_record_json=? WHERE attempt_id=?", (old_json, retained.request.attempt_id))
        connection.execute("ALTER TABLE local_review_repair_attempts DROP COLUMN publication_send_state")
    restarted = LocalReviewRepairStore(retained.store.path)
    assert restarted.get(retained.request).publication_send_state == "unknown"
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", lambda *_args, **_kwargs: pytest.fail("legacy journal recovery must not recommit"))
    recovered = resume(retained, store=restarted)
    assert recovered.phase == "awaiting_validation" and recovered.published
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == candidate
    assert restarted.get_commit_recovery(retained.request).original_record_json == old_json


def test_affirmative_send_entry_blocks_contradictory_pre_push_recovery(retained, monkeypatch):
    with sqlite3.connect(retained.store.path) as connection:
        connection.execute("UPDATE local_review_repair_attempts SET publication_send_state='entered' WHERE attempt_id=?", (retained.request.attempt_id,))
    original = retained.store.get(retained.request)
    assert not original.result_sha and not original.publication_started
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", lambda *_args, **_kwargs: pytest.fail("affirmative send entry must not be discarded"))
    outcome = resume(retained)
    assert outcome.phase == "indeterminate" and not outcome.published
    assert retained.store.get(retained.request) == original
    assert retained.store.get_commit_recovery(retained.request) is None
