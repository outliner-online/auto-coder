"""Evidence-only supersession through the real editable-review recovery entry."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Barrier

import pytest

from auto_coder import editable_review, local_review_repair
from auto_coder.editable_review import capture_review_workspace, resume_pending_review_publication
from auto_coder.local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore
from auto_coder.review_supersession import ReviewSupersessionTarget
from tests.test_editable_review import repository  # noqa: F401 (shared real Git fixture)
from tests.test_editable_review import _git, _publish, _request

pytestmark = pytest.mark.usefixtures("_use_custom_subprocess_mock")


@contextmanager
def missing_git_identity(root: Path, monkeypatch):
    """Exercise Git's actual commit refusal without changing user configuration."""
    name = _git(root, "config", "user.name")
    email = _git(root, "config", "user.email")
    _git(root, "config", "--unset", "user.name")
    _git(root, "config", "--unset", "user.email")
    _git(root, "config", "user.useConfigOnly", "true")
    try:
        with monkeypatch.context() as isolated:
            isolated.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
            isolated.setenv("GIT_CONFIG_NOSYSTEM", "1")
            for variable in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
                isolated.delenv(variable, raising=False)
            yield
    finally:
        _git(root, "config", "user.name", name)
        _git(root, "config", "user.email", email)
        _git(root, "config", "--unset", "user.useConfigOnly")


def retain_legacy_shape(store: LocalReviewRepairStore, request: LocalReviewRepairRequest) -> None:
    """Remove only the newly introduced checkpoint from this isolated fixture."""
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE local_review_repair_attempts SET pre_push_evidence_json='' WHERE repository=? AND pr_number=? AND attempt_id=?",
            (request.repository, request.pr_number, request.attempt_id),
        )


@dataclass(frozen=True)
class RetainedReview:
    root: Path
    remote: Path
    original_head: str
    request: LocalReviewRepairRequest
    store: LocalReviewRepairStore
    workspace: Path
    earlier_workspace: Path | None = None

    def target(self) -> ReviewSupersessionTarget:
        return ReviewSupersessionTarget("owner/repo", 42, "owner/repo", "review-head", _git(self.remote, "rev-parse", "refs/heads/review-head"))

    def resume(self, *, current_target=None, store=None):
        return resume_pending_review_publication(
            repository="owner/repo",
            pr_number=42,
            root=str(self.root),
            current_target=current_target or self.target,
            store=store or self.store,
            authority_check=lambda *_: pytest.fail("supersession must never seek publication authority"),
        )

    def advance(self, variant: str = "complete") -> str:
        peer = self.root.parent / "peer"
        _git(self.root.parent, "clone", "--branch", "review-head", str(self.remote), str(peer))
        _git(peer, "config", "user.name", "Independent Owner")
        _git(peer, "config", "user.email", "owner@example.com")
        if variant == "unrelated-lineage":
            _git(peer, "checkout", "--orphan", "other-lineage")
        patch = subprocess.run(["git", "diff", "--cached", "--binary"], cwd=self.workspace, capture_output=True, check=True).stdout
        subprocess.run(["git", "apply", "--binary"], cwd=peer, input=patch, capture_output=True, check=True)
        if variant == "partial":
            (peer / "added.txt").unlink()
        elif variant == "different":
            (peer / "source.py").write_text("value = 3\n", encoding="utf-8")
        elif variant == "lost-mode":
            (peer / "source.py").chmod(0o644)
        elif variant == "lost-deletion":
            (peer / "removed.txt").write_text("old\n", encoding="utf-8")
        elif variant == "wrong-symlink":
            (peer / "latest.py").unlink()
            (peer / "latest.py").symlink_to("added.txt")
        (peer / "peer.txt").write_text("independent additional work\n", encoding="utf-8")
        _git(peer, "add", "-A")
        _git(peer, "commit", "-m", "Independently include reviewer correction")
        head = _git(peer, "rev-parse", "HEAD")
        _git(peer, "push", "--force", "origin", "HEAD:review-head")
        return head


def _edit_retained_source(root: Path) -> None:
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    (root / "source.py").chmod(0o755)
    (root / "removed.txt").unlink()
    (root / "added.txt").write_text("entire retained correction\n", encoding="utf-8")
    (root / "latest.py").symlink_to("source.py")


@pytest.fixture
def retained(repository, tmp_path, monkeypatch, request):
    variant = getattr(request, "param", "legacy")
    legacy = variant != "checkpointed"
    root, remote, head = repository
    earlier_workspace = None
    if variant == "legacy-with-old-worktree":
        earlier_workspace = tmp_path / "auto_coder_review_42_prior"
        _git(root, "worktree", "add", "--detach", str(earlier_workspace), head)
        _edit_retained_source(earlier_workspace)
        _git(earlier_workspace, "add", "-A")
    snapshot = capture_review_workspace(str(root), head)
    _edit_retained_source(root)
    store = LocalReviewRepairStore(tmp_path / "retained.sqlite3")
    with missing_git_identity(root, monkeypatch):
        outcome = _publish(snapshot, store)
    request = _request(head)
    record = store.get(request)
    assert outcome.phase == "indeterminate" and not outcome.published
    assert record is not None and record.invocation_entered and not record.result_sha
    assert "identity" in record.reason.lower() or "email" in record.reason.lower()
    workspace = Path(record.workspace_path)
    assert workspace.is_dir() and _git(workspace, "rev-parse", "HEAD") == head
    if legacy:
        retain_legacy_shape(store, request)
    else:
        assert record.pre_push_evidence_json
    yield RetainedReview(root, remote, head, request, store, workspace, earlier_workspace)
    # The product retains the output; fixture teardown removes only its own worktrees.
    for fixture_workspace in (workspace, earlier_workspace):
        if fixture_workspace is not None:
            subprocess.run(["git", "worktree", "remove", "--force", str(fixture_workspace)], cwd=root, capture_output=True)


def forbid_recovery_writes(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("evidence-only recovery must not commit, push, copy, or replay a reviewer")

    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", unexpected)
    monkeypatch.setattr(local_review_repair, "git_push", unexpected)
    monkeypatch.setattr(editable_review, "execute_local_review_repair", unexpected)
    original = subprocess.run

    def checked_run(command, *args, **kwargs):
        if isinstance(command, (tuple, list)) and command and command[0] == "git":
            assert not any(part in {"commit", "push", "send-email", "apply", "add", "checkout", "reset", "update-ref"} for part in command[1:]), command
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", checked_run)


@pytest.mark.parametrize("retained", ["legacy", "checkpointed"], indirect=True)
def test_legacy_pre_push_correction_is_superseded_without_replay_and_survives_restart(retained, monkeypatch):
    head = retained.advance()
    original = retained.store.get(retained.request)
    observed = []

    def current_target():
        target = retained.target()
        observed.append(target)
        return target

    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = retained.resume(current_target=current_target)
        assert (outcome.phase, outcome.executed, outcome.published) == ("superseded", True, False)
        assert len(observed) >= 2
        restarted = LocalReviewRepairStore(retained.store.path)
        assert retained.resume(store=restarted) is None

    record = retained.store.get(retained.request)
    assert record.phase == "superseded"
    assert record.attempt_id == original.attempt_id and record.incarnation == original.incarnation
    assert (record.reason, record.workspace_path, record.result_sha) == (original.reason, original.workspace_path, "")
    assert retained.workspace.is_dir()
    assert _git(retained.workspace, "rev-parse", "HEAD") == retained.original_head
    assert (retained.workspace / "source.py").read_text() == "value = 2\n"
    assert (retained.workspace / "added.txt").read_text() == "entire retained correction\n"
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == head
    with sqlite3.connect(retained.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_review_repair_supersessions").fetchone()[0] == 1
    disposition = retained.store.get_supersession(retained.request)
    assert disposition is not None
    assert disposition.replacement_head == head
    provenance = json.loads(disposition.evidence_json)
    assert provenance["classification"] == ("checkpointed_pre_push" if original.pre_push_evidence_json else "corroborated_legacy_pre_push")
    assert provenance["target"]["head_sha"] == head
    assert provenance["retained"]["attempt_id"] == original.attempt_id
    assert provenance["retained"]["original_head"] == retained.original_head
    assert provenance["retained"]["incarnation"] == original.incarnation
    assert {entry["path"] for entry in provenance["retained"]["paths"]} == {"source.py", "added.txt", "removed.txt", "latest.py"}


@pytest.mark.parametrize("variant", ["partial", "different", "lost-mode", "lost-deletion", "wrong-symlink", "unrelated-lineage"])
def test_newer_head_must_preserve_every_affected_content_and_lineage(retained, monkeypatch, variant):
    head = retained.advance(variant)
    original = retained.store.get(retained.request)
    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = retained.resume()
    assert outcome.phase == "indeterminate" and not outcome.published
    category = "unrelated_lineage" if variant == "unrelated-lineage" else "correction_not_incorporated"
    assert f"[{category}:" in outcome.reason
    assert retained.store.get(retained.request) == original
    assert retained.store.get_supersession(retained.request) is None
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == head


def test_unchanged_head_preserves_current_head_blocker(retained, monkeypatch):
    original = retained.store.get(retained.request)
    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = retained.resume()
    assert outcome.phase == "indeterminate" and not outcome.published
    assert "[unchanged_head:" in outcome.reason
    assert retained.store.get(retained.request) == original
    assert retained.store.get_supersession(retained.request) is None


@pytest.mark.parametrize(
    "damage",
    [
        "unstaged",
        "restaged",
        "restaged-equivalent",
        "missing-reflog",
        "missing-head",
        "missing-index",
        "missing-registration",
        "absent-origin",
        "committed-then-reset",
        "missing-workspace",
        "missing-owner",
        "missing-invocation",
        "candidate",
        "publish-entry",
        "different-ref",
        "job-owner",
        "missing-origin",
    ],
)
def test_missing_changed_or_publication_evidence_never_releases_blocker(retained, monkeypatch, damage):
    head = retained.advance()
    if damage in {"unstaged", "restaged", "restaged-equivalent"}:
        contents = "value = 2\n" if damage == "restaged-equivalent" else "value = 999\n"
        (retained.workspace / "source.py").write_text(contents, encoding="utf-8")
        if damage.startswith("restaged"):
            _git(retained.workspace, "add", "source.py")
    elif damage in {"missing-reflog", "missing-head", "missing-index", "missing-registration"}:
        git_directory = Path(_git(retained.workspace, "rev-parse", "--absolute-git-dir"))
        relative = {"missing-reflog": "logs/HEAD", "missing-head": "HEAD", "missing-index": "index", "missing-registration": "gitdir"}[damage]
        (git_directory / relative).unlink()
    elif damage == "absent-origin":
        _git(retained.root, "remote", "remove", "origin")
    elif damage == "committed-then-reset":
        _git(retained.workspace, "commit", "-m", "Candidate may have entered publication")
        _git(retained.workspace, "reset", "--soft", retained.original_head)
    else:
        column, value = {
            "missing-workspace": ("workspace_path", str(retained.root.parent / "absent-worktree")),
            "missing-owner": ("owner_pid", None),
            "missing-invocation": ("invocation_entered", 0),
            "candidate": ("result_sha", head),
            "publish-entry": ("publication_started", 1),
            "different-ref": ("head_ref", "another-branch"),
            "job-owner": ("local_job_id", "different-owner"),
            "missing-origin": ("expected_push_url", ""),
        }[damage]
        with sqlite3.connect(retained.store.path) as connection:
            connection.execute(f"UPDATE local_review_repair_attempts SET {column}=? WHERE attempt_id=?", (value, retained.request.attempt_id))
    original = retained.store.get(retained.request)
    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = retained.resume()
    assert outcome.phase == "indeterminate" and not outcome.published
    category = {
        "unstaged": "unsettled_output",
        "restaged": "retained_output_changed",
        "restaged-equivalent": "retained_output_changed",
        "missing-reflog": "reflog_evidence_missing",
        "missing-head": "head_evidence_missing",
        "missing-index": "index_evidence_missing",
        "missing-registration": "workspace_registration_missing",
        "absent-origin": "origin_evidence_unavailable",
        "committed-then-reset": "candidate_history_conflict",
        "missing-workspace": "workspace_evidence_missing",
        "missing-owner": "owner_evidence_missing",
        "missing-invocation": "owner_evidence_missing",
        "candidate": "publication_may_have_started",
        "publish-entry": "publication_may_have_started",
        "different-ref": "target_mismatch",
        "job-owner": "owner_evidence_missing",
        "missing-origin": "workspace_evidence_missing",
    }[damage]
    assert f"[{category}:" in outcome.reason
    assert retained.store.get(retained.request) == original
    assert retained.store.get_supersession(retained.request) is None
    assert _git(retained.remote, "rev-parse", "refs/heads/review-head") == head


def test_matching_stderr_without_settled_retained_state_is_not_pre_push_proof(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    store = LocalReviewRepairStore(tmp_path / "stderr-only.sqlite3")
    request = replace(_request(head), expected_push_url=str(remote))
    claim = store.admit(request)
    store.mark_invocation_entered(request, claim)
    store.transition(request, claim, "indeterminate", reason="Author identity unknown", workspace_path=str(root))
    original = store.get(request)
    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store, current_target=lambda: ReviewSupersessionTarget("owner/repo", 42, "owner/repo", "review-head", head))
    assert outcome.phase == "indeterminate" and not outcome.published
    assert "[workspace_owner_mismatch:" in outcome.reason
    assert store.get(request) == original
    assert store.get_supersession(request) is None


@pytest.mark.parametrize("field,value", [("repository", "other/repo"), ("pr_number", 43), ("head_repository", "other/repo"), ("head_ref", "other-branch"), ("state", "closed")])
def test_fresh_target_must_still_be_the_same_open_pr_and_branch(retained, monkeypatch, field, value):
    retained.advance()
    target = replace(retained.target(), **{field: value})
    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = retained.resume(current_target=lambda: target)
    assert outcome.phase == "indeterminate" and not outcome.published
    assert "[target_mismatch:" in outcome.reason
    assert retained.store.get(retained.request).phase == "indeterminate"
    assert retained.store.get_supersession(retained.request) is None


def test_unavailable_fresh_target_is_an_explicit_blocker(retained, monkeypatch):
    retained.advance()

    def unavailable():
        raise RuntimeError("GitHub readback unavailable")

    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = retained.resume(current_target=unavailable)
    assert outcome.phase == "indeterminate" and not outcome.published
    assert "[target_readback_unavailable:" in outcome.reason
    assert retained.store.get_supersession(retained.request) is None


@pytest.mark.parametrize("change", ["target", "output", "output-on-final-read", "owner"])
def test_evidence_and_owner_are_rechecked_at_atomic_settlement(retained, monkeypatch, change):
    retained.advance()
    observed = 0

    def current_target():
        nonlocal observed
        observed += 1
        target = retained.target()
        if observed == 1:
            if change == "output":
                (retained.workspace / "source.py").write_text("value = 999\n", encoding="utf-8")
            elif change == "owner":
                with sqlite3.connect(retained.store.path) as connection:
                    connection.execute("UPDATE local_review_repair_attempts SET incarnation=incarnation+1 WHERE attempt_id=?", (retained.request.attempt_id,))
        if observed == 2 and change == "output-on-final-read":
            (retained.workspace / "source.py").write_text("value = 999\n", encoding="utf-8")
        if observed > 1 and change == "target":
            return replace(target, state="closed")
        return target

    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = retained.resume(current_target=current_target)
    assert outcome.phase == "indeterminate" and not outcome.published
    category = {"target": "target_mismatch", "output": "unsettled_output", "output-on-final-read": "unsettled_output", "owner": "attempt_owner_changed"}[change]
    assert f"[{category}:" in outcome.reason
    assert retained.store.get(retained.request).phase == "indeterminate"
    assert retained.store.get_supersession(retained.request) is None


def test_concurrent_reentry_records_exactly_one_disposition(retained, monkeypatch):
    retained.advance()
    barrier = Barrier(2)

    def recover():
        barrier.wait(timeout=10)
        return retained.resume(store=LocalReviewRepairStore(retained.store.path))

    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(lambda _: recover(), range(2)))
    assert any(outcome is not None and outcome.phase == "superseded" for outcome in outcomes)
    assert all(outcome is None or outcome.phase == "superseded" for outcome in outcomes)
    assert retained.store.get(retained.request).phase == "superseded"
    with sqlite3.connect(retained.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_review_repair_supersessions").fetchone()[0] == 1
    assert retained.resume(store=LocalReviewRepairStore(retained.store.path)) is None


def test_missing_authoritative_pr_observer_preserves_blocker(retained, monkeypatch):
    retained.advance()
    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(retained.root), store=retained.store)
    assert outcome.phase == "indeterminate" and not outcome.published
    assert "[target_readback_unavailable:" in outcome.reason
    assert retained.store.get_supersession(retained.request) is None


def test_pr_readback_and_authoritative_branch_must_agree(retained, monkeypatch):
    retained.advance()
    stale_target = retained.target()
    _git(retained.remote, "update-ref", "refs/heads/review-head", retained.original_head)
    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = retained.resume(current_target=lambda: stale_target)
    assert outcome.phase == "indeterminate" and not outcome.published
    assert "[remote_head_mismatch:" in outcome.reason
    assert retained.store.get_supersession(retained.request) is None


@pytest.mark.parametrize("retained", ["legacy-with-old-worktree"], indirect=True)
def test_legacy_attempt_cannot_borrow_an_older_staged_worktree(retained, monkeypatch):
    retained.advance()
    earlier = retained.earlier_workspace
    assert earlier is not None
    original = retained.store.get(retained.request)
    assert (earlier / ".git").stat().st_mtime_ns < original.incarnation
    with sqlite3.connect(retained.store.path) as connection:
        connection.execute("UPDATE local_review_repair_attempts SET workspace_path=? WHERE attempt_id=?", (str(earlier), original.attempt_id))
    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        outcome = retained.resume()
    assert outcome.phase == "indeterminate" and not outcome.published
    assert "[workspace_owner_mismatch:" in outcome.reason
    assert retained.store.get_supersession(retained.request) is None
    assert retained.store.get(retained.request).phase == "indeterminate"


def test_legacy_worktree_must_belong_to_the_same_pr(retained, tmp_path, monkeypatch):
    retained.advance()
    wrong_pr = tmp_path / "auto_coder_review_43_other"
    _git(retained.root, "worktree", "move", str(retained.workspace), str(wrong_pr))
    try:
        with sqlite3.connect(retained.store.path) as connection:
            connection.execute("UPDATE local_review_repair_attempts SET workspace_path=? WHERE attempt_id=?", (str(wrong_pr), retained.request.attempt_id))
        with monkeypatch.context() as recovery:
            forbid_recovery_writes(recovery)
            outcome = retained.resume()
        assert outcome.phase == "indeterminate" and not outcome.published
        assert "[workspace_owner_mismatch:" in outcome.reason
        assert retained.store.get_supersession(retained.request) is None
        assert retained.store.get(retained.request).phase == "indeterminate"
    finally:
        _git(retained.root, "worktree", "remove", "--force", str(wrong_pr))


def test_merge_base_operational_failure_is_not_reported_as_unrelated_lineage(retained, monkeypatch):
    retained.advance()
    with monkeypatch.context() as recovery:
        forbid_recovery_writes(recovery)
        run = subprocess.run

        def failed_merge_base(command, *args, **kwargs):
            if isinstance(command, (list, tuple)) and command[0] == "git" and "merge-base" in command:
                return subprocess.CompletedProcess(command, 128, b"", b"fatal: object database is unavailable")
            return run(command, *args, **kwargs)

        recovery.setattr(subprocess, "run", failed_merge_base)
        outcome = retained.resume()
    assert outcome.phase == "indeterminate" and not outcome.published
    assert "[git_evidence_unavailable:" in outcome.reason
    assert retained.store.get_supersession(retained.request) is None
    assert retained.store.get(retained.request).phase == "indeterminate"
