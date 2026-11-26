"""Real Git regressions for settled editable-review output and publication."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder import editable_review, local_review_repair
from auto_coder.editable_review import (
    ReviewChangesProduced,
    assert_review_workspace_unchanged,
    bind_editable_review,
    capture_review_workspace,
    editable_review_active,
    publish_review_changes,
    resume_pending_review_publication,
    review_workspace_changed,
)
from auto_coder.local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore
from auto_coder.utils import CommandResult
from auto_coder.worktree_utils import WorkspaceHandoffError

pytestmark = pytest.mark.usefixtures("_use_custom_subprocess_mock")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Review Test")
    _git(root, "config", "user.email", "review@example.com")
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    (root / "removed.txt").write_text("old\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
    (root / "coverage").mkdir()
    (root / "coverage" / "tracked.txt").write_text("tracked source\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Initial source")
    head = _git(root, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "origin", f"{head}:refs/heads/review-head")
    return root, remote, head


def _publish(snapshot, store, **kwargs):
    return publish_review_changes(snapshot, repository="owner/repo", pr_number=42, head_repository="owner/repo", head_ref="review-head", review_identity="ordinary:attempt-7", store=store, **kwargs)


def _request(head: str) -> LocalReviewRepairRequest:
    return LocalReviewRepairRequest("owner/repo", 42, "owner/repo", "review-head", head, ("review-output:ordinary:attempt-7",), "")


def test_guard_uses_final_source_contents_and_restores_binding(repository):
    root, _remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    assert editable_review_active() is False

    with bind_editable_review(snapshot):
        assert editable_review_active() is True
        assert_review_workspace_unchanged()
        (root / "source.py").write_text("value = 2\n", encoding="utf-8")
        assert _git(root, "rev-parse", "HEAD") == head
        with pytest.raises(ReviewChangesProduced) as changed:
            assert_review_workspace_unchanged()
        assert changed.value.snapshot is snapshot
        (root / "source.py").write_text("value = 1\n", encoding="utf-8")
        assert_review_workspace_unchanged()
    assert editable_review_active() is False


@pytest.mark.parametrize("multiple_destinations", [False, True])
def test_preconfigured_foreign_push_destination_is_refused(repository, tmp_path, multiple_destinations):
    root, remote, head = repository
    alternate = tmp_path / "other.git"
    _git(tmp_path, "clone", "--bare", str(remote), str(alternate))
    if multiple_destinations:
        _git(root, "config", "--add", "remote.origin.pushurl", str(remote))
    _git(root, "config", "--add", "remote.origin.pushurl", str(alternate))
    with pytest.raises(RuntimeError, match="destination"):
        capture_review_workspace(str(root), head)
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head
    assert _git(alternate, "rev-parse", "refs/heads/review-head") == head


def test_new_runtime_artifacts_do_not_become_source_but_tracked_reports_do(repository):
    root, _remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    for relative in ("coverage/new.json", "test-results/output.xml", ".agent-tmp/check.txt", "build/ignored.js", "runtime.log"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("generated", encoding="utf-8")
    assert review_workspace_changed(snapshot) is False
    (root / "coverage" / "tracked.txt").write_text("changed tracked source\n", encoding="utf-8")
    assert review_workspace_changed(snapshot) is True


@pytest.mark.parametrize("change", ["dirty", "untracked", "head"])
def test_snapshot_and_guard_refuse_unadmitted_source(repository, change):
    root, _remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    if change == "untracked":
        (root / "new.py").write_text("value = 1\n", encoding="utf-8")
    else:
        (root / "source.py").write_text("value = 2\n", encoding="utf-8")
        if change == "head":
            _git(root, "add", "source.py")
            _git(root, "commit", "-m", "Moved controller head")
    with pytest.raises(WorkspaceHandoffError):
        capture_review_workspace(str(root), head)
    if change == "head":
        with pytest.raises(WorkspaceHandoffError):
            review_workspace_changed(snapshot)


def test_source_delta_publishes_through_existing_correction_without_artifacts(repository, tmp_path):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    (root / "removed.txt").unlink()
    (root / "tests").mkdir()
    (root / "tests" / "test_source.py").write_text("def test_source():\n    assert True\n", encoding="utf-8")
    (root / "source.py").chmod(0o755)
    (root / "latest.py").symlink_to("source.py")
    (root / "coverage" / "new.json").write_text("generated", encoding="utf-8")
    (root / "runtime.log").write_text("generated", encoding="utf-8")
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    checks = []
    result = _publish(snapshot, store, authority_check=lambda sha, ref: checks.append((sha, ref)) or True)
    assert (result.phase, result.published, result.executed) == ("awaiting_validation", True, True)
    assert checks == [(head, "review-head")] * 3
    published = _git(remote, "rev-parse", "refs/heads/review-head")
    assert published != head
    assert _git(remote, "show", f"{published}:source.py") == "value = 2"
    assert _git(remote, "show", f"{published}:tests/test_source.py").startswith("def test_source")
    files = _git(remote, "ls-tree", "-r", published)
    assert "100755 blob" in files and "120000 blob" in files
    assert "removed.txt" not in files and "coverage/new.json" not in files and "runtime.log" not in files
    assert _git(root, "rev-parse", "HEAD") == head
    assert (root / "source.py").read_text() == "value = 2\n"
    assert store.get(_request(head)).result_sha == published
    assert store.get(_request(head)).feedback_identities == ("review-output:ordinary:attempt-7",)


def test_lost_push_response_reconciles_without_copy_model_or_second_push(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    real_push = local_review_repair.git_push
    pushed = []

    def lost_response(**kwargs):
        pushed.append(kwargs)
        result = real_push(**kwargs)
        assert result.success
        return CommandResult(False, "", "response lost", 1)

    monkeypatch.setattr(local_review_repair, "git_push", lost_response)
    first = _publish(snapshot, store)
    assert (first.phase, first.published) == ("publication_pending", False)
    published = _git(remote, "rev-parse", "refs/heads/review-head")
    assert published != head
    monkeypatch.setattr(editable_review, "execute_local_review_repair", lambda *_a, **_kw: pytest.fail("recovery must not repeat the copier or invoke a model"))
    restored = resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store, authority_check=lambda *_: pytest.fail("confirmed publication needs no new push authority"))
    assert (restored.phase, restored.published) == ("awaiting_validation", True)
    assert len(pushed) == 1
    assert resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store) is None


def test_pre_send_deferral_recovers_original_ref_and_commit_without_repeating_review(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    real_push = local_review_repair.git_push
    monkeypatch.setattr(local_review_repair, "git_push", lambda **_kwargs: pytest.fail("pre-send authority deferral must not invoke push"))

    def before_send_only(*_args):
        record = store.get(_request(head))
        return record is None or not record.result_sha

    assert _publish(snapshot, store, authority_check=before_send_only).phase == "publication_pending"
    assert store.get(_request(head)).publication_send_state == "not_entered"
    retained = store.get(_request(head)).result_sha
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head
    (root / "source.py").write_text("value = 999\n", encoding="utf-8")
    monkeypatch.setattr(local_review_repair, "git_push", real_push)
    checks = []
    result = resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store, authority_check=lambda sha, ref: checks.append((sha, ref)) or True)
    assert result.published is True
    assert checks == [(head, "review-head")]
    assert _git(remote, "rev-parse", "refs/heads/review-head") == retained
    assert _git(remote, "show", f"{retained}:source.py") == "value = 2"


def test_remote_advance_never_gets_overwritten_by_reviewer_publication(repository, tmp_path):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    peer = tmp_path / "peer"
    _git(tmp_path, "clone", str(remote), "--branch", "review-head", str(peer))
    _git(peer, "config", "user.name", "Peer")
    _git(peer, "config", "user.email", "peer@example.com")
    (peer / "peer.txt").write_text("concurrent work\n", encoding="utf-8")
    _git(peer, "add", "peer.txt")
    _git(peer, "commit", "-m", "Peer update")
    _git(peer, "push", "origin", "HEAD:review-head")
    peer_sha = _git(peer, "rev-parse", "HEAD")
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    first = _publish(snapshot, store)
    assert first.phase == "publication_pending" and not first.published
    assert _git(remote, "rev-parse", "refs/heads/review-head") == peer_sha
    resumed = resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store)
    assert resumed.phase == "publication_pending" and "conflict" in resumed.reason
    assert _git(remote, "rev-parse", "refs/heads/review-head") == peer_sha


def test_changed_authority_or_failed_hook_never_starts_hidden_editor(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    denied_store = LocalReviewRepairStore(tmp_path / "denied.sqlite3")
    denied = _publish(snapshot, denied_store, authority_check=lambda *_: False)
    assert (denied.phase, denied.executed, denied.published) == ("not_started", False, False)
    assert resume_pending_review_publication(repository="owner/repo", pr_number=42, root=lambda: pytest.fail("no candidate needs repository access"), store=denied_store) is None
    assert denied_store.get(_request(head)).phase == "not_started"
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'dprint fmt required' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    import auto_coder.git_branch as git_branch

    monkeypatch.setattr(git_branch, "try_llm_dprint_fallback", lambda *_args: pytest.fail("publication must not invoke another model"))
    store = LocalReviewRepairStore(tmp_path / "hook.sqlite3")
    failed = _publish(snapshot, store)
    assert failed.phase == "indeterminate" and not failed.published
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head
    retained = store.get(_request(head))
    assert Path(retained.workspace_path, "source.py").read_text() == "value = 2\n"


def test_recovery_does_not_consume_unrelated_correction_or_uncommitted_output(repository, tmp_path):
    root, _remote, head = repository
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ordinary = replace(_request(head), feedback_identities=("native-thread-123",))
    claim = store.admit(ordinary)
    store.transition(ordinary, claim, "publication_pending", result_sha=head)
    assert resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store) is None
    store.transition(ordinary, claim, "awaiting_validation", result_sha=head)
    review = _request(head)
    claim = store.admit(review)
    store.transition(review, claim, "indeterminate", reason="copy interrupted", workspace_path=str(root))
    resumed = resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store)
    assert resumed.phase == "indeterminate" and not resumed.published


def test_new_reviewer_output_never_resumes_another_correction(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ordinary = replace(_request(head), feedback_identities=("native-thread-123",))
    claim = store.admit(ordinary)
    store.transition(ordinary, claim, "publication_pending", result_sha=head)
    monkeypatch.setattr(local_review_repair, "_resume_publication", lambda *_a, **_kw: pytest.fail("a new reviewer must not consume another correction"))
    result = _publish(snapshot, store)
    assert result.phase == "publication_pending" and not result.published
    assert store.get(ordinary).phase == "publication_pending"
    assert store.get(_request(head)) is None
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head


def test_changed_push_destination_refuses_fresh_and_recovered_review_output(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    real_push = local_review_repair.git_push
    monkeypatch.setattr(local_review_repair, "git_push", lambda **_kwargs: pytest.fail("pre-send authority deferral must not invoke push"))

    def before_send_only(*_args):
        record = store.get(_request(head))
        return record is None or not record.result_sha

    assert _publish(snapshot, store, authority_check=before_send_only).phase == "publication_pending"
    assert store.get(_request(head)).publication_send_state == "not_entered"
    alternate = tmp_path / "another-remote.git"
    _git(tmp_path, "clone", "--bare", str(remote), str(alternate))
    assert _git(alternate, "rev-parse", "refs/heads/review-head") == head
    _git(root, "remote", "set-url", "origin", str(alternate))
    with pytest.raises(WorkspaceHandoffError, match="destination changed"):
        review_workspace_changed(snapshot)
    with pytest.raises(WorkspaceHandoffError, match="destination changed"):
        _publish(snapshot, store)
    monkeypatch.setattr(local_review_repair, "git_push", real_push)
    recovered = resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store, authority_check=lambda *_: True)
    assert recovered.phase == "publication_pending" and not recovered.published
    assert "destination changed" in recovered.reason
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head
    assert _git(alternate, "rev-parse", "refs/heads/review-head") == head
    _git(root, "remote", "set-url", "origin", str(remote))
    restored = resume_pending_review_publication(repository="owner/repo", pr_number=42, root=str(root), store=store, authority_check=lambda *_: True)
    assert restored.published is True
    assert _git(alternate, "rev-parse", "refs/heads/review-head") == head


def test_lost_publication_checkpoint_ownership_never_pushes(repository, tmp_path, monkeypatch):
    root, remote, head = repository
    snapshot = capture_review_workspace(str(root), head)
    (root / "source.py").write_text("value = 2\n", encoding="utf-8")
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    real_transition = store.transition

    def changed_owner(request, claim, phase, **kwargs):
        if phase == "publication_pending":
            return False
        return real_transition(request, claim, phase, **kwargs)

    monkeypatch.setattr(store, "transition", changed_owner)
    monkeypatch.setattr(local_review_repair, "git_push", lambda **_kwargs: pytest.fail("a rejected durable candidate checkpoint cannot authorize push"))
    result = _publish(snapshot, store)
    assert result.phase == "indeterminate" and not result.published
    assert _git(remote, "rev-parse", "refs/heads/review-head") == head
    record = store.get(_request(head))
    assert Path(record.workspace_path).is_dir()
    assert _git(Path(record.workspace_path), "rev-parse", "HEAD") != head
