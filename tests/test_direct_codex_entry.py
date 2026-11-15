"""Unit coverage of the direct entry; fake supervisor is not live audit proof."""

import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.direct_codex import DirectCodexError, DirectCodexRequest, DirectCodexResult, claim_prepared_home, codex_arguments, private_environment, result_document, run_direct_codex, selected_credentials
from src.auto_coder.invocation_process_supervisor import InvocationOutcome, SupervisedInvocationResult, WriterState
from src.auto_coder.local_execution_boundary import BackendOutcome, EvidenceStatus, LocalBoundaryEvidence

pytestmark = pytest.mark.usefixtures("_use_real_commands")


def request(tmp_path: Path, **kwargs):
    return DirectCodexRequest(tmp_path, "Inspect the repository", "test-model", **kwargs)


@pytest.mark.parametrize("bad", [{"prompt": ""}, {"model": ""}, {"timeout_seconds": 0}, {"timeout_seconds": -1}, {"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")}])
def test_invalid_request_refused(tmp_path, bad):
    fields = dict(repository=tmp_path, prompt="prompt", model="model")
    fields.update(bad)
    with pytest.raises(DirectCodexError):
        codex_arguments(DirectCodexRequest(**fields))


def test_fixed_arguments_have_no_caller_arbitrary_flags(tmp_path):
    args = codex_arguments(request(tmp_path))
    assert args == ("--sandbox", "danger-full-access", "--ask-for-approval", "never", "-c", 'approvals_reviewer="user"', "exec", "--model", "test-model", "-")


def test_operator_receipt_separates_direct_child_from_descendants(tmp_path):
    evidence = LocalBoundaryEvidence(
        boundary_id="boundary",
        turn_id="turn",
        invocation_id="invocation",
        backend_type="codex",
        workspace=tmp_path / "workspace",
        editable=False,
        provider_session_id=None,
        backend_outcome=BackendOutcome.SUCCEEDED,
        filesystem_enforcement=EvidenceStatus.ESTABLISHED,
        publication_enforcement=EvidenceStatus.UNKNOWN,
        writer_completion=EvidenceStatus.UNKNOWN,
        violation_observation=EvidenceStatus.ESTABLISHED,
        policy_violation=False,
        failure=None,
        direct_process_completion=EvidenceStatus.ESTABLISHED,
    )
    receipt = result_document(DirectCodexResult("invocation", "head", "checkpoint", "stdout", "stderr", evidence, "/workspace", "/runtime"))

    assert receipt["direct_child_completion"] == "established"
    assert receipt["descendant_termination"] == "unknown"
    assert receipt["publication"] == "not_performed"


def test_private_environment_does_not_inherit_ambient_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/ambient-secret")
    monkeypatch.setenv("GIT_DIR", "/other-repo")
    env = private_environment(tmp_path / "home", tmp_path / "tmp", {"OPENAI_API_KEY": "synthetic"})
    assert env["CODEX_HOME"] == str(tmp_path / "home/.codex")
    assert env["OPENAI_API_KEY"] == "synthetic"
    assert "GIT_DIR" not in env
    with pytest.raises(DirectCodexError):
        private_environment(tmp_path, tmp_path, {"HOME": "/ambient-secret"})


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("editable", [False, True])
@pytest.mark.parametrize("outcome", [InvocationOutcome.SUCCEEDED, InvocationOutcome.PRESTART_UNAVAILABLE, InvocationOutcome.CANCELLED, InvocationOutcome.FAILED, InvocationOutcome.TIMED_OUT])
@pytest.mark.parametrize("missing", [None, "filesystem", "observation", "direct"])
def test_entry_binds_private_root_without_caller_publication(tmp_path, editable, outcome, missing, prepared):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "source").write_text("original")
    git("add", ".")
    git("commit", "-qm", "initial")
    observed = []
    auth_root = Path(tempfile.mkdtemp(prefix="direct-home-test-")) if prepared else None
    auth_home = auth_root / "home" if auth_root else None
    if auth_home:
        (auth_home / ".codex").mkdir(parents=True, mode=0o700)
        auth_home.chmod(0o700)
        (auth_home / ".codex/auth.json").write_text("synthetic login material")

    class Supervisor:
        def run(self, launch, *, boundary):
            observed.append(launch)
            assert launch.cwd != tmp_path
            assert launch.effective_mode == ("editable" if editable else "no-edit")
            assert launch.protected_paths == (tmp_path,)
            assert launch.runtime_paths[0] != Path.home()
            # Synthetic supervisor produces evidence only for this unit test.
            if editable:
                (launch.cwd / "source").write_text("private edit")
            invocation = launch.invocation_id
            boundary.record_backend_success(invocation)
            if missing != "filesystem":
                boundary.record_filesystem_enforcement(invocation, EvidenceStatus.ESTABLISHED)
            if missing != "observation":
                boundary.record_violation_observation(invocation)
            if missing != "direct":
                boundary.record_direct_process_completion(invocation)
            return SupervisedInvocationResult(invocation, outcome, WriterState.DIRECT_PROCESS_STOPPED, 0, "done", "", direct_process_complete=True)

    try:
        with patch("src.auto_coder.direct_codex.check_runtime"), patch("src.auto_coder.direct_codex.InvocationProcessSupervisor", Supervisor), patch("src.auto_coder.direct_codex.verify_worker_repository") as readiness:
            if outcome is InvocationOutcome.SUCCEEDED and missing is None:
                result = run_direct_codex(request(tmp_path, editable=editable), executable=Path("/bin/true"), provider_environment={}, prepared_home=auth_home)
                assert result.evidence.writer_completion is EvidenceStatus.UNKNOWN
                assert result.evidence.direct_process_completion is EvidenceStatus.ESTABLISHED
                assert result.source_head == subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            else:
                with pytest.raises(DirectCodexError, match="supervised invocation failed"):
                    run_direct_codex(request(tmp_path, editable=editable), executable=Path("/bin/true"), provider_environment={}, prepared_home=auth_home)
        if auth_home:
            assert observed[0].environment["HOME"] == str(auth_home)
            assert (auth_home / ".codex/auth.json").read_text() == "synthetic login material"
            assert (auth_root / ".direct-codex-invocation-claimed").exists()
        assert len(observed) == 1  # No alternate provider or unconfined retry.
        readiness.assert_called_once()
        assert (tmp_path / "source").read_text() == "original"
        assert (observed[0].cwd / "source").read_text() == ("private edit" if editable else "original")
    finally:
        for launch in observed:
            shutil.rmtree(launch.cwd.parent)
            shutil.rmtree(launch.runtime_paths[1].parent)
        if auth_root:
            shutil.rmtree(auth_root)


def test_root_refused_before_provider(tmp_path):
    with patch("src.auto_coder.direct_codex.os.geteuid", return_value=0), patch("src.auto_coder.direct_codex.InvocationProcessSupervisor") as supervisor:
        with pytest.raises(DirectCodexError, match="non-root"):
            run_direct_codex(request(tmp_path), executable=Path("/bin/true"), provider_environment={})
        supervisor.assert_not_called()


def test_credentials_require_explicit_selection():
    class NoRead(dict):
        def __getitem__(self, key):
            raise AssertionError("unexpected credential read")

    assert selected_credentials([], NoRead()) == {}
    assert selected_credentials(["OPENAI_API_KEY"], {"OPENAI_API_KEY": "synthetic", "CODEX_API_KEY": "other"}) == {"OPENAI_API_KEY": "synthetic"}
    with pytest.raises(DirectCodexError):
        selected_credentials(["HOME"], NoRead())


def test_explicit_prepared_home_single_use(tmp_path):
    root = tmp_path / "private"
    home = root / "home"
    (home / ".codex").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    home.chmod(0o700)
    caller = tmp_path / "caller"
    caller.mkdir()
    assert claim_prepared_home(home, caller) == home
    with pytest.raises(DirectCodexError, match="already claimed"):
        claim_prepared_home(home, caller)


@pytest.mark.parametrize("part", ["parent", "home", "codex"])
def test_prepared_home_nonprivate_refused(tmp_path, part):
    root = tmp_path / "private"
    home = root / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    home.chmod(0o700)
    {"parent": root, "home": home, "codex": codex}[part].chmod(0o755)
    with pytest.raises(DirectCodexError, match="private owned"):
        claim_prepared_home(home, tmp_path / "caller")
    assert not (root / ".direct-codex-invocation-claimed").exists()


def test_prepared_home_symlink_refused(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(DirectCodexError, match="symlinks"):
        claim_prepared_home(link, tmp_path / "caller")


def test_prepared_home_within_caller_refused(tmp_path):
    root = tmp_path / "private"
    home = root / "home"
    (home / ".codex").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    home.chmod(0o700)
    with pytest.raises(DirectCodexError, match="separate"):
        claim_prepared_home(home, root)


def test_prepared_home_codex_symlink_refused(tmp_path):
    root = tmp_path / "private"
    home = root / "home"
    home.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    actual = root / "elsewhere"
    actual.mkdir(mode=0o700)
    (home / ".codex").symlink_to(actual, target_is_directory=True)
    with pytest.raises(DirectCodexError, match="private owned"):
        claim_prepared_home(home, tmp_path / "caller")


def test_prepared_home_wrong_owner_refused(tmp_path):
    root = tmp_path / "private"
    home = root / "home"
    (home / ".codex").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    home.chmod(0o700)
    with patch("src.auto_coder.direct_codex.os.geteuid", return_value=424242):
        with pytest.raises(DirectCodexError, match="private owned"):
            claim_prepared_home(home, tmp_path / "caller")


@pytest.mark.parametrize("mixed", [False, True])
def test_prepared_home_prelaunch_failure_preserves_login(tmp_path, mixed):
    root = tmp_path / "private"
    home = root / "home"
    (home / ".codex").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    home.chmod(0o700)
    sentinel = home / ".codex/auth.json"
    sentinel.write_text("synthetic")
    caller = tmp_path / "caller"
    caller.mkdir()
    with (
        patch("src.auto_coder.direct_codex.check_runtime"),
        patch("src.auto_coder.direct_codex.is_git_repository", return_value=True),
        patch("src.auto_coder.direct_codex.isolated_local_llm_worktree", side_effect=RuntimeError("prelaunch fixture")),
        patch("src.auto_coder.direct_codex.InvocationProcessSupervisor") as supervisor,
    ):
        with pytest.raises(DirectCodexError if mixed else RuntimeError, match="not both" if mixed else "prelaunch fixture"):
            run_direct_codex(request(caller), executable=Path("/bin/true"), provider_environment={"OPENAI_API_KEY": "synthetic"} if mixed else {}, prepared_home=home)
        supervisor.assert_not_called()
    assert sentinel.read_text() == "synthetic"
    assert (root / ".direct-codex-invocation-claimed").exists() is (not mixed)
