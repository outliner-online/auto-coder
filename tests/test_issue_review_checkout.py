"""Ordinary Issue intake through the scheduler, real Git, and local Muse MSP."""

from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.backend_manager import BackendManager
from auto_coder.execution_trace import get_trace_collector
from auto_coder.invocation_admission import bind_invocation_target, current_invocation_target
from auto_coder.issue_review_execution import issue_specification_execution_cwd
from auto_coder.issue_stage_routing import IMPLEMENTATION_STAGE, REVIEW_STAGE
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.muse_client import MuseClient
from auto_coder.specification_analyzer import analyze_issue_specification
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.utils import _COMMAND_EXECUTION_CWD, bind_command_execution_cwd, reset_command_execution_cwd
from auto_coder.validation_scheduler import ValidationScheduler
from auto_coder.worktree_utils import get_current_local_workspace
from tests.test_issue_review_service import FakeGitHub
from tests.test_muse_msp import _host, _repository

READY = json.dumps(
    {
        "verdict": "READY",
        "remediation": "NONE",
        "findings": [],
        "finding_dispositions": [],
        "coverage": [{"boundary": boundary, "status": "FRESH", "no_impact_reason": ""} for boundary in ("REQ-001", "contract-wide")],
    }
)


def git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=checkout, check=True, capture_output=True, text=True).stdout.strip()


def checkout_for(root: Path, repository: str) -> Path:
    root.mkdir()
    checkout = _repository(root)
    git(checkout, "remote", "add", "origin", f"https://github.com/{repository}.git")
    return checkout


def configure(repository: str, value: object) -> None:
    directory = Path.home() / ".auto-coder" / repository
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(f"[issue_review]\ncheckout = {json.dumps(value)}\n")


@dataclass(frozen=True)
class Launch:
    repository: str
    issue: str
    worktree: Path
    source: Path
    head: str
    contents: str
    thread: int


@pytest.fixture
def review_runtime(tmp_path, monkeypatch, _use_real_commands, _use_real_sleep):
    """Keep real production analysis; replace only GitHub and the provider host."""
    controller = tmp_path / "controller"
    controller.mkdir()
    monkeypatch.chdir(controller)
    monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(tmp_path / "routing.sqlite3"))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    monkeypatch.setenv("AUTO_CODER_REVIEW_AUDIT_ROOT", str(tmp_path / "audit"))
    host = _host(tmp_path)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_ANSWER", READY)
    alias = "review-muse"
    config = LLMBackendConfiguration(backends={alias: BackendConfig(name=alias, backend_type="muse", model="muse-spark-1.3", timeout=10)})
    monkeypatch.setattr("auto_coder.muse_client.get_llm_config", lambda: config)
    monkeypatch.setattr("auto_coder.backend_manager.get_llm_config", lambda: config)
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.analyze_issue_specification", analyze_issue_specification)
    managers = {}
    engines = []
    launches = []
    barrier = None
    original_popen = subprocess.Popen

    def observed_popen(command, *args, **kwargs):
        if command[0] == str(host) and "serve" in command:
            target = current_invocation_target()
            workspace = get_current_local_workspace()
            assert target is not None and workspace is not None
            cwd = Path(kwargs["cwd"])
            # Independent launch-boundary oracle: query actual Git, rather than
            # trusting the resolver, snapshot helper, or a successful validator.
            launches.append(Launch(target.repository, target.target, Path(git(cwd, "rev-parse", "--show-toplevel")), workspace.caller_root, git(cwd, "rev-parse", "HEAD"), (cwd / "tracked.txt").read_text(), threading.get_ident()))
            assert "--disable-write" in command
            assert Path.cwd() == controller
            if barrier is not None:
                barrier.wait(timeout=10)
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", observed_popen)

    def manager_for(**_kwargs):
        target = current_invocation_target()
        assert target is not None
        return managers[target.repository]

    monkeypatch.setattr("auto_coder.cli_helpers.create_adversarial_validation_backend_manager", manager_for)

    def engine_for(repository, number=1):
        directory = tmp_path / repository.replace("/", "-")
        directory.mkdir(exist_ok=True)
        client = MuseClient(alias)
        manager = BackendManager(default_backend=alias, default_client=client, factories={alias: lambda: client}, order=[alias], automatic_session_resume=False)
        managers[repository] = manager
        github = FakeGitHub([])
        github.issues[number] = github.snapshot(number)
        engine = AutomationEngine(github, config=AutomationConfig(repo_name=repository))
        engine.review_scheduler.shutdown()
        engine.review_scheduler = ValidationScheduler(1)
        engine._specification_validators[repository] = SpecificationValidationLifecycle(repository, "fixture-policy", directory / "decisions.json")
        engines.append(engine)
        return engine

    def overlap():
        nonlocal barrier
        barrier = threading.Barrier(2)

    yield engine_for, launches, controller, overlap, managers
    for engine in engines:
        engine.review_scheduler.shutdown()
        engine.validation_scheduler.shutdown()
    for manager in managers.values():
        manager.close()


def pump(engine, repository, number=1):
    return engine._get_review_service(repository).pump_target(number, "ordinary-checkout-regression")


def assert_unchanged(checkout, head):
    assert git(checkout, "rev-parse", "HEAD") == head
    assert git(checkout, "status", "--porcelain") == ""
    assert (checkout / "tracked.txt").read_text() == "unchanged\n"


@pytest.mark.parametrize("configuration", ["per-repository", "inline"])
def test_ordinary_intake_reaches_real_muse_from_non_git_controller(tmp_path, review_runtime, configuration):
    engine_for, launches, controller, _, managers = review_runtime
    repository = "owner/selected"
    checkout = checkout_for(tmp_path / "selected", repository)
    head = git(checkout, "rev-parse", "HEAD")
    if configuration == "per-repository":
        configure(repository, str(checkout))
    else:
        directory = Path.home() / ".auto-coder"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.toml").write_text(f'[repository."{repository}".issue_review]\ncheckout = {json.dumps(str(checkout))}\n')
    engine = engine_for(repository, 17)
    outcome = pump(engine, repository, 17)
    assert outcome.status == "completed" and outcome.handed_off
    assert len(launches) == 1
    launch = launches[0]
    assert (launch.repository, launch.issue, launch.source, launch.head) == (repository, "issue#17", checkout, head)
    assert launch.worktree != checkout and launch.contents == "unchanged\n"
    managers[repository].close()
    assert not launch.worktree.exists()
    assert Path.cwd() == controller and not (controller / ".git").exists()
    assert _COMMAND_EXECUTION_CWD.get() is None and current_invocation_target() is None
    assert_unchanged(checkout, head)


@pytest.mark.parametrize("problem", ["unset", "missing", "non-git", "unborn", "wrong-head", "mismatch", "ambiguous", "subdirectory", "relative", "wrong-type"])
def test_invalid_checkout_never_launches_or_authorizes_and_is_retryable(tmp_path, monkeypatch, review_runtime, problem):
    engine_for, launches, _, _, managers = review_runtime
    repository = "owner/selected"
    checkout = checkout_for(tmp_path / "selected", repository)
    good = checkout_for(tmp_path / "good", repository)
    head = git(good, "rev-parse", "HEAD")
    value = str(checkout)
    if problem == "missing":
        value = str(tmp_path / "missing")
    elif problem == "non-git":
        value = str(tmp_path)
    elif problem == "unborn":
        git(checkout, "update-ref", "-d", "HEAD")
    elif problem == "wrong-head":
        blob = git(checkout, "rev-parse", "HEAD:tracked.txt")
        (checkout / ".git" / "HEAD").write_text(blob + "\n")
    elif problem == "mismatch":
        git(checkout, "remote", "set-url", "origin", "https://github.com/other/repo.git")
    elif problem == "ambiguous":
        git(checkout, "config", "--add", "remote.origin.url", "https://github.com/other/repo.git")
    elif problem == "subdirectory":
        child = checkout / "nested"
        child.mkdir()
        value = str(child)
    elif problem == "relative":
        value = "../selected/repo"
    elif problem == "wrong-type":
        value = 42
    if problem != "unset":
        configure(repository, value)
    engine = engine_for(repository)
    # A valid ambient checkout must never rescue bad selected inputs.
    token = bind_command_execution_cwd(str(good))
    try:
        collector = get_trace_collector()
        before = max((event.sequence for event in collector.get_snapshot(repository=repository, item_type="issue", item_number=1).events), default=0)
        outcome = pump(engine, repository)
        assert outcome.status == "error" and not outcome.handed_off
        assert launches == []
        decision = next(iter(outcome.decisions.values()))
        assert decision.verdict == "ERROR" and decision.remediation_reason.startswith("Local Issue review setup failure:")
        assert engine._specification_validators[repository].store.get(decision.identity) is None
        assert engine.issue_stage_routing.pending(repository, IMPLEMENTATION_STAGE) == ()
        assert len(engine.issue_stage_routing.pending(repository, REVIEW_STAGE)) == 1
        assert engine.github.comments == [] and engine.github.removals == []
        assert not any(event.stage_id == "llm.local-execution" and event.sequence > before for event in collector.get_snapshot(repository=repository, item_type="issue", item_number=1).events)
        assert _COMMAND_EXECUTION_CWD.get() == str(good)
        configure(repository, str(good))
        retried = pump(engine, repository)
        assert retried.status == "completed" and retried.handed_off
        assert len(launches) == 1 and launches[0].source == good
        managers[repository].close()
        assert not launches[0].worktree.exists()
        assert _COMMAND_EXECUTION_CWD.get() == str(good)
    finally:
        reset_command_execution_cwd(token)
    assert_unchanged(good, head)


def test_overlapping_repositories_keep_checkout_head_and_issue_identity(tmp_path, review_runtime):
    engine_for, launches, controller, overlap, managers = review_runtime
    repositories = ("first/repo", "second/repo")
    sources = {repository: checkout_for(tmp_path / repository.split("/")[0], repository) for repository in repositories}
    # Independent commit identities and contents distinguish cross-repo borrowing.
    git(sources[repositories[1]], "commit", "--allow-empty", "-m", "second repository")
    heads = {repository: git(source, "rev-parse", "HEAD") for repository, source in sources.items()}
    engines = {}
    for number, repository in enumerate(repositories, 11):
        configure(repository, str(sources[repository]))
        engines[repository] = engine_for(repository, number)
    overlap()
    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(pump, engines[repository], repository, number) for number, repository in enumerate(repositories, 11)]
        outcomes = [future.result(timeout=30) for future in futures]
    assert all(outcome.status == "completed" and outcome.handed_off for outcome in outcomes)
    assert len(launches) == 2 and len({launch.thread for launch in launches}) == 2
    for manager in managers.values():
        manager.close()
    for launch in launches:
        number = 11 + repositories.index(launch.repository)
        assert (launch.source, launch.head, launch.issue) == (sources[launch.repository], heads[launch.repository], f"issue#{number}")
        assert not launch.worktree.exists()
        assert_unchanged(launch.source, launch.head)
    assert Path.cwd() == controller and _COMMAND_EXECUTION_CWD.get() is None


def test_failed_host_cleans_binding_before_same_thread_retry(tmp_path, monkeypatch, review_runtime):
    engine_for, launches, _, _, managers = review_runtime
    repository = "owner/selected"
    checkout = checkout_for(tmp_path / "selected", repository)
    head = git(checkout, "rev-parse", "HEAD")
    configure(repository, str(checkout))
    engine = engine_for(repository)
    monkeypatch.setenv("MSP_EXIT_NONZERO", "1")
    failed = pump(engine, repository)
    assert failed.status == "error" and not failed.handed_off
    assert len(launches) == 1 and not launches[0].worktree.exists()
    monkeypatch.delenv("MSP_EXIT_NONZERO")
    retried = pump(engine, repository)
    assert retried.status == "completed" and retried.handed_off
    managers[repository].close()
    assert len(launches) == 2 and launches[0].thread == launches[1].thread
    assert all(not launch.worktree.exists() for launch in launches)
    # Check the raw worker thread as well as the submitting thread after reuse.
    state = engine.review_scheduler._executor.submit(lambda: (_COMMAND_EXECUTION_CWD.get(), current_invocation_target(), get_current_local_workspace(), threading.get_ident())).result()
    assert state == (None, None, None, launches[0].thread)
    assert _COMMAND_EXECUTION_CWD.get() is None
    assert_unchanged(checkout, head)


def test_old_ambient_directory_path_fails_the_ordinary_positive_case(tmp_path, monkeypatch, review_runtime):
    engine_for, launches, controller, _, _managers = review_runtime
    repository = "owner/selected"
    checkout = checkout_for(tmp_path / "selected", repository)
    configure(repository, str(checkout))
    engine = engine_for(repository)
    # Remove only the new boundary, recreating the old scheduled production path.
    monkeypatch.setattr("auto_coder.backend_manager.issue_specification_execution_cwd", lambda *_args: nullcontext())
    original_snapshot = MuseClient._snapshot_at
    failures = []

    def observed_snapshot(client, cwd):
        try:
            return original_snapshot(client, cwd)
        except RuntimeError as error:
            failures.append((cwd, str(error)))
            raise

    monkeypatch.setattr(MuseClient, "_snapshot_at", observed_snapshot)
    outcome = pump(engine, repository)
    assert outcome.status == "error" and not outcome.handed_off
    assert launches == []
    assert Path.cwd() == controller and _COMMAND_EXECUTION_CWD.get() is None
    assert failures == [(controller, "Muse backend requires a Git repository with an existing HEAD")]


@pytest.mark.parametrize(
    "backend,stage,noedit",
    [("claude", "specification_validation", True), ("muse", "pr_review", True), ("muse", "decomposition_validation", True), ("muse", "specification_validation", False)],
)
def test_other_providers_and_review_kinds_keep_their_existing_directory(backend, stage, noedit, tmp_path):
    token = bind_command_execution_cwd(str(tmp_path))
    try:
        with bind_invocation_target("owner/repo", "issue#1", stage), issue_specification_execution_cwd(backend, noedit):
            assert _COMMAND_EXECUTION_CWD.get() == str(tmp_path)
        assert _COMMAND_EXECUTION_CWD.get() == str(tmp_path)
    finally:
        reset_command_execution_cwd(token)
