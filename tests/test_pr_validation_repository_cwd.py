"""PR review enters the real detached checkout from a service working directory."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from auto_coder import pr_processor
from auto_coder.editable_review import editable_review_active
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.invocation_admission import pr_review_designated
from auto_coder.utils import CommandExecutor, bind_command_execution_cwd, reset_command_execution_cwd
from tests.ci_observation_fakes import authoritative_ci_status
from tests.test_adversarial_validation_concurrency import _git, _repositories
from tests.test_pr_adversarial_review_audit import (  # noqa: F401 (audit_store is a fixture)
    PASS_PAYLOAD,
    PR_NUMBER,
    REPO_NAME,
    MockReviewerClient,
    _apply_standard_merge_gates,
    _build_backend_manager,
    _build_config,
    _build_github_client,
    _build_pr_data,
    _build_pr_repo,
    _wire_backend,
    audit_store,
)


@pytest.fixture
def scenario(tmp_path, monkeypatch, _use_real_commands, audit_store):
    repo, head = _build_pr_repo(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(repo), str(remote))
    _git(repo, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    _git(repo, "config", f"url.{remote}.insteadOf", "https://github.com/owner/repo.git")
    # Remote PR ref and shared checkout advance after the admitted CI snapshot.
    (repo / "sample.py").write_text("def greet():\n    return 'newer unreviewed head'\n")
    _git(repo, "commit", "-am", "advance")
    newer = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", f"HEAD:refs/pull/{PR_NUMBER}/head")
    (repo / "sample.py").write_text("staged changes\n")
    _git(repo, "add", "sample.py")
    (repo / "sample.py").write_text("unstaged changes\n")
    (repo / "untracked.txt").write_text("keep me\n")
    ambient = _repositories(tmp_path / "unrelated").ambient
    outside = tmp_path / "not-git"
    outside.mkdir()
    monkeypatch.setenv("AUTO_CODER_WORKSPACE_ROOT", str(repo))
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    _apply_standard_merge_gates(monkeypatch, merge_result=True)
    merge = Mock(return_value=True)
    monkeypatch.setattr(pr_processor, "_merge_pr", merge)
    client = _build_github_client(head, repo=repo)
    data = _build_pr_data(head, repo=repo)
    config = _build_config()
    seen = []

    class Reviewer(MockReviewerClient):
        def _run_llm_cli(self, prompt, is_noedit=False):
            # Production grants edit permission only inside the source-bound
            # review context, whose bootstrap policy skips an unscoped baseline.
            # This scripted reviewer deliberately leaves the source unchanged.
            assert is_noedit is False
            assert pr_review_designated() is True
            assert editable_review_active() is True
            root = CommandExecutor.run_command(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
            assert Path(root) != repo
            assert CommandExecutor.run_command(["git", "rev-parse", "HEAD"]).stdout.strip() == head
            assert not CommandExecutor.run_command(["git", "symbolic-ref", "-q", "HEAD"]).success
            assert (Path(root) / "sample.py").read_text() == 'def greet():\n    return "hello"\n'
            seen.append(Path(root))
            return super()._run_llm_cli(prompt, is_noedit=is_noedit)

    reviewer = Reviewer("reviewer", responses=[PASS_PAYLOAD])
    manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")
    _wire_backend(monkeypatch, manager)
    publication = Mock(return_value=ReviewPublicationResult(True, "APPROVE", ""))
    monkeypatch.setattr(pr_processor, "publish_adversarial_review", publication)
    return repo, head, newer, ambient, outside, client, data, config, seen, merge, publication


def _snapshot(repo):
    return (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "symbolic-ref", "HEAD"),
        _git(repo, "status", "--porcelain"),
        _git(repo, "diff", "--binary"),
        _git(repo, "diff", "--cached", "--binary"),
        _git(repo, "worktree", "list", "--porcelain"),
        {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()},
    )


@pytest.mark.parametrize("start", ["non-git", "unrelated-git", "bound-context", "linked-shared-origin", "linked-worktree-origin"])
def test_real_pr_validation_uses_selected_repository_and_ci_head(scenario, monkeypatch, start):
    repo, head, newer, ambient, outside, client, data, config, seen, merge, publication = scenario
    if start.startswith("linked-"):
        linked = repo.parent / "linked-source"
        _git(repo, "worktree", "add", "--detach", str(linked), newer)
        if start == "linked-worktree-origin":
            _git(repo, "config", "--local", "extensions.worktreeConfig", "true")
            _git(repo, "config", "--local", "--unset-all", "remote.origin.url")
            _git(linked, "config", "--worktree", "remote.origin.url", "https://github.com/owner/repo.git")
        monkeypatch.setenv("AUTO_CODER_WORKSPACE_ROOT", str(linked))
    monkeypatch.chdir(ambient if start == "unrelated-git" else outside)
    if start == "bound-context":
        monkeypatch.delenv("AUTO_CODER_WORKSPACE_ROOT")
    before = _snapshot(repo), _snapshot(ambient)
    token = bind_command_execution_cwd(str(repo if start == "bound-context" else ambient))
    try:
        actions = pr_processor._handle_pr_merge(client, REPO_NAME, data, config, {})
        assert CommandExecutor.run_command(["git", "rev-parse", "--show-toplevel"]).stdout.strip() == str(repo if start == "bound-context" else ambient)
    finally:
        reset_command_execution_cwd(token)
    assert head != newer
    assert len(seen) == 1, actions
    assert not seen[0].exists()
    assert before == (_snapshot(repo), _snapshot(ambient))
    assert Path.cwd() == (ambient if start == "unrelated-git" else outside)
    assert pr_review_designated() is False
    assert editable_review_active() is False
    publication.assert_called_once()
    merge.assert_called_once()


@pytest.mark.parametrize("configuration", ["global-only", "include-only", "effective-conflict"])
def test_pr_review_requires_checkout_owned_origin(scenario, monkeypatch, configuration):
    """Ordinary inherited Git configuration cannot identify the selected repository."""
    repo, head, newer, ambient, outside, client, data, config, seen, merge, publication = scenario
    inherited = repo.parent / "inherited.gitconfig"
    url = "https://github.com/other/repo.git" if configuration == "effective-conflict" else "https://github.com/owner/repo.git"
    inherited.write_text(f'[remote "origin"]\n    url = {url}\n')
    if configuration != "effective-conflict":
        _git(repo, "config", "--local", "--unset-all", "remote.origin.url")
    if configuration == "include-only":
        _git(repo, "config", "--local", "include.path", str(inherited))
    else:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(inherited))
    # The historical effective-only check sees a matching origin in both
    # inherited-only cases, without needing a network or credential boundary.
    effective = _git(repo, "config", "--includes", "--get-all", "remote.origin.url").splitlines()
    assert effective == (["https://github.com/other/repo.git", "https://github.com/owner/repo.git"] if configuration == "effective-conflict" else ["https://github.com/owner/repo.git"])
    monkeypatch.chdir(outside)
    before = _snapshot(repo), _snapshot(ambient)
    actions = pr_processor._handle_pr_merge(client, REPO_NAME, data, config, {})
    assert seen == [], actions
    merge.assert_not_called()
    publication.assert_called_once()
    diagnostic = publication.call_args.args[3]
    assert diagnostic.result == "ERROR"
    assert diagnostic.diagnostic_category == "validation_execution_error"
    assert before == (_snapshot(repo), _snapshot(ambient))


@pytest.mark.parametrize("invalid", ["wrong-repository", "non-git", "relative", "missing", "config-override"])
def test_invalid_selected_repository_never_falls_back_or_merges(scenario, monkeypatch, invalid):
    repo, head, newer, ambient, outside, client, data, config, seen, merge, publication = scenario
    _git(ambient, "remote", "add", "origin", "https://github.com/other/repo.git")
    # Even the wrong repository can fetch the admitted SHA: identity, rather
    # than an accidental missing object or unreachable network, must reject it.
    _git(ambient, "config", f"url.{repo.parent / 'remote.git'}.insteadOf", "https://github.com/other/repo.git")
    monkeypatch.chdir(repo)
    selected = {"wrong-repository": str(ambient), "non-git": str(outside), "relative": ".", "missing": str(outside / "missing"), "config-override": str(repo)}[invalid]
    monkeypatch.setenv("AUTO_CODER_WORKSPACE_ROOT", selected)
    if invalid == "config-override":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    before = _snapshot(repo), _snapshot(ambient)
    actions = pr_processor._handle_pr_merge(client, REPO_NAME, data, config, {})
    assert seen == [], actions
    merge.assert_not_called()
    publication.assert_called_once()
    assert publication.call_args.args[:3] == (REPO_NAME, PR_NUMBER, head)
    diagnostic = publication.call_args.args[3]
    assert diagnostic.result == "ERROR"
    assert diagnostic.diagnostic_category == "validation_execution_error"
    assert diagnostic.findings == []
    assert before == (_snapshot(repo), _snapshot(ambient))


@pytest.mark.parametrize("gate", ["review", "ci-admission", "ci-refresh"])
def test_checkout_fix_preserves_review_and_ci_gates(scenario, monkeypatch, gate):
    repo, head, newer, ambient, outside, client, data, config, seen, merge, publication = scenario
    monkeypatch.chdir(outside)
    if gate == "review":
        monkeypatch.setattr(pr_processor, "has_unresolved_review_threads", lambda *a, **k: True)
    else:
        green = authoritative_ci_status(success=True, ids=[1])
        pending = authoritative_ci_status(success=False, in_progress=True, ids=[1])
        monkeypatch.setattr(pr_processor, "_check_github_actions_status", lambda *a, **k: pending(*a, **k) if gate == "ci-admission" or seen else green(*a, **k))
    before = _snapshot(repo), _snapshot(ambient)
    actions = pr_processor._handle_pr_merge(client, REPO_NAME, data, config, {})
    assert len(seen) == (1 if gate == "ci-refresh" else 0), actions
    merge.assert_not_called()
    assert before == (_snapshot(repo), _snapshot(ambient))
