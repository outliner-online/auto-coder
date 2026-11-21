"""Real scheduled decomposition analysis and Muse preflight, stopped before MSP."""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.backend_manager import BackendManager
from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from auto_coder.execution_trace import get_trace_collector
from auto_coder.invocation_admission import current_invocation_target
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.muse_client import MuseClient
from auto_coder.utils import _COMMAND_EXECUTION_CWD
from auto_coder.validation_scheduler import ValidationScheduler
from auto_coder.worktree_utils import get_current_local_workspace
from tests.test_issue_decomposition_validation_kill_switch import DecompositionGitHubFlow, make_child, make_parent
from tests.test_issue_review_checkout import Launch, assert_unchanged, checkout_for, configure, git
from tests.test_muse_msp import _host


class StoppedBeforeProvider(RuntimeError):
    """The real Git preparation succeeded; no provider process may start."""


@pytest.fixture
def decomposition_runtime(tmp_path, monkeypatch, _use_real_commands, _use_real_sleep):
    controller = tmp_path / "controller"
    controller.mkdir()
    monkeypatch.chdir(controller)
    monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(tmp_path / "routing.sqlite3"))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    monkeypatch.setenv("AUTO_CODER_REVIEW_AUDIT_ROOT", str(tmp_path / "audit"))
    # Only the fixture's local --version probe executes. MSP is never launched.
    host = str(_host(tmp_path))
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", host)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=10)})
    monkeypatch.setattr("auto_coder.muse_client.get_llm_config", lambda: config)
    monkeypatch.setattr("auto_coder.backend_manager.get_llm_config", lambda: config)
    managers = {}
    engines = []
    launches = []
    stopped_workspaces = []
    failures = []
    barrier = None
    original_popen = subprocess.Popen
    original_snapshot = MuseClient._snapshot_at

    def observed_snapshot(client, cwd):
        try:
            return original_snapshot(client, cwd)
        except RuntimeError as error:
            failures.append((cwd, str(error)))
            raise

    monkeypatch.setattr(MuseClient, "_snapshot_at", observed_snapshot)

    def stopped_popen(command, *args, **kwargs):
        if command[0] == host and "serve" in command:
            target = current_invocation_target()
            workspace = get_current_local_workspace()
            assert target is not None and workspace is not None
            assert target.stage == "decomposition_validation"
            cwd = Path(kwargs["cwd"])
            launches.append(Launch(target.repository, target.target, Path(git(cwd, "rev-parse", "--show-toplevel")), workspace.caller_root, git(cwd, "rev-parse", "HEAD"), (cwd / "tracked.txt").read_text(), threading.get_ident()))
            stopped_workspaces.append(workspace)
            assert "--disable-write" in command
            if barrier is not None:
                barrier.wait(timeout=10)
            raise StoppedBeforeProvider("Regression stopped before provider launch")
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", stopped_popen)

    def manager_for(**_kwargs):
        target = current_invocation_target()
        assert target is not None
        return managers[target.repository]

    monkeypatch.setattr("auto_coder.cli_helpers.create_adversarial_validation_backend_manager", manager_for)

    def engine_for(repository, number=10):
        client = MuseClient("muse")
        managers[repository] = BackendManager(default_backend="muse", default_client=client, factories={"muse": lambda: client}, order=["muse"], automatic_session_resume=False)
        parent = make_parent(number, body="## Objective\nCoordinate the child behavior.")
        child = make_child(number + 1, parent_number=number, body=f"Parent-Issue: #{number}\nBlocked-By:\n\n## Objective\nDeliver the behavior.\n\n## Requirements\nREQ-001: Deliver the behavior.")
        github = DecompositionGitHubFlow(parent, [child])
        config = AutomationConfig(repo_name=repository, env_override=False)
        config.issue_specification_validation = False
        config.issue_decomposition_validation = True
        engine = AutomationEngine(github, config=config)
        engine.review_scheduler.shutdown()
        engine.review_scheduler = ValidationScheduler(1)
        engine._decomposition_validators[repository] = DecompositionValidationLifecycle(repository, "fixture-policy", tmp_path / repository.replace("/", "-") / "sets.json")
        engines.append(engine)
        return engine

    def overlap():
        nonlocal barrier
        barrier = threading.Barrier(2)

    yield engine_for, launches, failures, controller, overlap
    for engine in engines:
        engine.review_scheduler.shutdown()
        engine.validation_scheduler.shutdown()
    for manager in managers.values():
        manager.close()
    # Production conservatively retains incomplete provider lifecycle evidence.
    # This fixture alone knows Popen never ran, so settle its intercepted owners.
    for workspace in stopped_workspaces:
        workspace.ownership.release_execution()
    assert all(not launch.worktree.exists() for launch in launches)


def schedule(engine, repository, number=10):
    authoritative = engine._fetch_authoritative_decomposition_set(repository, number)
    assert authoritative is not None
    job, children = engine._schedule_parent_validations(repository, authoritative, scheduler=engine.review_scheduler)
    assert job is not None and children == {}
    decision = job.result()
    # Both local failures and the deliberate pre-provider stop must fail closed.
    assert decision.verdict == "ERROR"
    assert engine._decomposition_validators[repository].store.get(decision.identity) is None
    assert engine.github.comments == [] and engine.github.removed_labels == []
    assert engine.implementation_slots is None
    state = engine.review_scheduler._executor.submit(lambda: (_COMMAND_EXECUTION_CWD.get(), current_invocation_target(), get_current_local_workspace())).result()
    assert state == (None, None, None)
    return decision


@pytest.mark.parametrize("ambient", ["non-git", "unrelated", "selected"])
def test_scheduled_decomposition_uses_selected_checkout_before_provider(tmp_path, monkeypatch, decomposition_runtime, ambient):
    engine_for, launches, failures, controller, _ = decomposition_runtime
    repository = "owner/selected"
    checkout = checkout_for(tmp_path / "selected", repository)
    head = git(checkout, "rev-parse", "HEAD")
    configure(repository, str(checkout))
    if ambient == "unrelated":
        controller = checkout_for(tmp_path / "unrelated", "other/repo")
    elif ambient == "selected":
        controller = checkout
    monkeypatch.chdir(controller)
    decision = schedule(engine_for(repository), repository)
    assert decision.remediation_reason == "Decomposition analysis execution failed: StoppedBeforeProvider"
    assert failures == [] and len(launches) == 1
    launch = launches[0]
    assert (launch.repository, launch.issue, launch.source, launch.head) == (repository, "issue#10", checkout, head)
    assert launch.worktree != checkout
    assert launch.contents == "unchanged\n"
    assert Path.cwd() == controller and _COMMAND_EXECUTION_CWD.get() is None
    assert_unchanged(checkout, head)


@pytest.mark.parametrize("problem", ["unset", "missing", "non-git", "mismatch", "unborn"])
def test_invalid_decomposition_checkout_fails_before_provider_and_can_retry(tmp_path, monkeypatch, decomposition_runtime, problem):
    engine_for, launches, _, _, _ = decomposition_runtime
    repository = "owner/selected"
    checkout = checkout_for(tmp_path / "selected", repository)
    good = checkout_for(tmp_path / "good", repository)
    # A valid ambient Git HEAD must never rescue an invalid selected checkout.
    monkeypatch.chdir(good)
    value = str(checkout)
    if problem == "missing":
        value = str(tmp_path / "missing")
    elif problem == "non-git":
        value = str(tmp_path)
    elif problem == "mismatch":
        git(checkout, "remote", "set-url", "origin", "https://github.com/other/repo.git")
    elif problem == "unborn":
        git(checkout, "update-ref", "-d", "HEAD")
    if problem != "unset":
        configure(repository, value)
    engine = engine_for(repository)
    collector = get_trace_collector()
    before = max((event.sequence for event in collector.get_snapshot(repository=repository, item_type="issue", item_number=10).events), default=0)
    decision = schedule(engine, repository)
    assert decision.remediation_reason.startswith("Local Issue review setup failure:")
    assert launches == []
    assert not any(event.stage_id == "llm.local-execution" and event.sequence > before for event in collector.get_snapshot(repository=repository, item_type="issue", item_number=10).events)
    configure(repository, str(good))
    retried = schedule(engine, repository)
    assert retried.remediation_reason == "Decomposition analysis execution failed: StoppedBeforeProvider"
    assert len(launches) == 1 and launches[0].source == good


def test_parallel_decomposition_repositories_do_not_borrow_checkout(tmp_path, decomposition_runtime):
    engine_for, launches, failures, controller, overlap = decomposition_runtime
    repositories = ("first/repo", "second/repo")
    sources = {repository: checkout_for(tmp_path / repository.split("/")[0], repository) for repository in repositories}
    git(sources[repositories[1]], "commit", "--allow-empty", "-m", "distinct second HEAD")
    heads = {repository: git(source, "rev-parse", "HEAD") for repository, source in sources.items()}
    engines = {}
    for repository in repositories:
        configure(repository, str(sources[repository]))
        engines[repository] = engine_for(repository, 10 + 10 * repositories.index(repository))
    overlap()
    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(schedule, engines[repository], repository, 10 + 10 * repositories.index(repository)) for repository in repositories]
        for future in futures:
            assert future.result(timeout=30).remediation_reason == "Decomposition analysis execution failed: StoppedBeforeProvider"
    assert failures == [] and len(launches) == 2
    assert len({launch.thread for launch in launches}) == 2
    for launch in launches:
        assert (launch.source, launch.head, launch.issue) == (sources[launch.repository], heads[launch.repository], f"issue#{10 + 10 * repositories.index(launch.repository)}")
        assert_unchanged(launch.source, launch.head)
    assert Path.cwd() == controller and _COMMAND_EXECUTION_CWD.get() is None


def test_old_decomposition_binding_gap_reproduces_muse_head_failure(tmp_path, monkeypatch, decomposition_runtime):
    engine_for, launches, failures, controller, _ = decomposition_runtime
    repository = "owner/selected"
    checkout = checkout_for(tmp_path / "selected", repository)
    configure(repository, str(checkout))
    monkeypatch.setattr("auto_coder.backend_manager.issue_specification_execution_cwd", lambda *_args: nullcontext())
    decision = schedule(engine_for(repository), repository)
    assert decision.remediation_reason == "Decomposition analysis execution failed: RuntimeError"
    assert failures == [(controller, "Muse backend requires a Git repository with an existing HEAD")]
    assert launches == []
