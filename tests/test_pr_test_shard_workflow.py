import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/pr-tests.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _prepare_step(tmp_path, group, test_script, timeout="0.25", grace="0.1", *, retain_workflow_uv=False):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/run_pr_test_shard.py", scripts)
    data = tmp_path / "tests/data"
    data.mkdir(parents=True)
    (data / "pr-test-durations.json").write_text('{"fixture": 1.0}', encoding="utf-8")
    (scripts / "test.sh").write_text(test_script, encoding="utf-8")
    run = next(step["run"] for step in _workflow()["jobs"]["tests-shard"]["steps"] if step["name"].startswith("Run tests with coverage"))
    run = re.sub(r"--attempt-timeout\s+\S+", f"--attempt-timeout {timeout}", run)
    run = re.sub(r"--termination-grace\s+\S+", f"--termination-grace {grace}", run)
    run = run.replace("${{ matrix.group }}", str(group))
    return run if retain_workflow_uv else run.replace("uv run python", f"{shutil.which('python')} ")


def _run_step(tmp_path, group, test_script, timeout="0.25", grace="0.1"):
    run = _prepare_step(tmp_path, group, test_script, timeout, grace)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=5,
    )


def _run_real_pytest_step(tmp_path: Path, assertion: str) -> subprocess.CompletedProcess[str]:
    """Run workflow -> supervisor -> real test.sh -> native pytest."""
    tmp_path.mkdir()
    run = _prepare_step(tmp_path, 1, "unused", timeout="10", grace="1", retain_workflow_uv=True)
    scripts = tmp_path / "scripts"
    shutil.copy(ROOT / "scripts/test.sh", scripts)
    shutil.copy(ROOT / "pyproject.toml", tmp_path)
    shutil.copy(ROOT / "uv.lock", tmp_path)
    shutil.copytree(ROOT / "build_backend", tmp_path / "build_backend")
    shutil.copytree(ROOT / "src", tmp_path / "src")
    package = tmp_path / "src/auto_coder"
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "git_info.py").write_text(
        "def get_current_repo_name():\n    return 'outliner-online/auto-coder'\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    node_id = "tests/test_sample.py::test_outcome"
    (tests / "test_sample.py").write_text(f"def test_outcome():\n    assert {assertion}\n", encoding="utf-8")
    data = tests / "data"
    (data / "pr-test-durations.json").write_text(f'{{"{node_id}": 1.0}}\n', encoding="utf-8")

    environment = {
        **os.environ,
        "CI": "true",
        "REPO_NAME": "outliner-online/auto-coder",
        "HOME": str(tmp_path / "home"),
        # Never allow an inherited project environment (including the checkout's
        # .venv) to be synchronized from this deliberately modified fixture.
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / ".venv"),
    }
    environment.pop("VIRTUAL_ENV", None)
    uv_executable = next(str(Path(directory) / "uv") for directory in environment["PATH"].split(os.pathsep) if (Path(directory) / "uv").is_file())
    sync = subprocess.Popen(
        [uv_executable, "sync", "--locked", "--dev", "--extra", "test"],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    sync_stdout, sync_stderr = sync.communicate(timeout=120)
    assert sync.returncode == 0, sync_stdout + sync_stderr
    # The workflow has just completed the locked sync represented above;
    # prevent nested uv invocations from narrowing that prepared profile.
    environment["UV_NO_SYNC"] = "1"
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _environment_linkage_snapshot(environment: Path) -> dict[str, bytes]:
    """Capture files that identify which source tree an environment imports."""
    patterns = ("pyvenv.cfg", "*.pth", "direct_url.json")
    return {str(path.relative_to(environment)): path.read_bytes() for pattern in patterns for path in environment.rglob(pattern)}


@pytest.mark.parametrize("group", [1, 2, 3, 4])
def test_production_step_passes_correct_shard_once(tmp_path, group):
    result = _run_step(
        tmp_path,
        group,
        '#!/bin/bash\nprintf "%s\\n" "$*" >> invocations\necho shard-output\n',
    )

    assert result.returncode == 0
    assert (tmp_path / "invocations").read_text().splitlines() == [
        f"--ci-tests-only --splits 4 --group {group} --splitting-algorithm duration_based_chunks --durations-path pr-test-logs/shard-{group}/normalized-durations.json -m not browser and not opencode_live and not muse_live and not supervisor_integration -vv -o faulthandler_timeout=30"
    ]
    assert "shard-output" in result.stdout
    assert (tmp_path / f"pr-test-logs/shard-{group}/attempt-1.stdout.log").exists()
    assert not (tmp_path / f"pr-test-logs/shard-{group}/attempt-2.stdout.log").exists()


@pytest.mark.parametrize("status", [1, 2, 5, 124, 137])
def test_completed_nonzero_status_is_not_retried(tmp_path, status):
    result = _run_step(
        tmp_path,
        2,
        f"#!/bin/bash\necho invoked >> invocations\nexit {status}\n",
    )

    assert result.returncode != 0
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    assert "ordinary failure" in result.stderr
    assert "starting retry" not in result.stderr


def test_actual_pytest_assertion_propagates_without_retry_and_passing_control_passes(tmp_path):
    outer_environment = ROOT / ".venv"
    outer_before_passing = _environment_linkage_snapshot(outer_environment)
    passing = _run_real_pytest_step(tmp_path / "passing", "True")
    assert "uv run python scripts/run_pr_test_shard.py" in passing.args[-1]
    assert passing.returncode == 0, passing.stdout + passing.stderr
    assert (tmp_path / "passing/.venv/pyvenv.cfg").is_file()
    assert _environment_linkage_snapshot(outer_environment) == outer_before_passing

    outer_before_failing = _environment_linkage_snapshot(outer_environment)
    failing = _run_real_pytest_step(tmp_path / "failing", "False")
    assert failing.returncode != 0
    assert "1 failed" in failing.stdout
    assert "ordinary failure" in failing.stderr
    assert "starting retry" not in failing.stderr
    assert not (tmp_path / "failing/pr-test-logs/shard-1/attempt-2.stdout.log").exists()
    assert (tmp_path / "failing/.venv/pyvenv.cfg").is_file()
    assert _environment_linkage_snapshot(outer_environment) == outer_before_failing

    aggregate = _workflow()["jobs"]["tests"]["steps"][0]["run"]

    def run_aggregate(shard: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
        shard_conclusion = "success" if shard.returncode == 0 else "failure"
        command = aggregate.replace("${{ needs.lint.result }}", "success").replace("${{ needs.tests-shard.result }}", shard_conclusion)
        return subprocess.run(["bash", "-c", command], capture_output=True, text=True)

    assert run_aggregate(passing).returncode == 0
    assert run_aggregate(failing).returncode != 0


def test_completed_failure_observed_after_deadline_is_not_a_timeout(tmp_path, monkeypatch):
    """A completed group wins over the deadline at the production supervisor."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/run_pr_test_shard.py", scripts)
    (scripts / "test.sh").write_text("#!/bin/bash\nexit 9\n", encoding="utf-8")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    durations = tmp_path / "durations.json"
    durations.write_text('{"fixture":1.0}\n', encoding="utf-8")

    spec = importlib.util.spec_from_file_location("post_deadline_runner", scripts / "run_pr_test_shard.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    real_monotonic = time.monotonic
    calls = 0
    launched_process = None

    def observe_after_completion():
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0.0
        if calls == 2:
            deadline = real_monotonic() + 2
            assert launched_process is not None
            while runner._group_members(launched_process.pid) and real_monotonic() < deadline:
                time.sleep(0.001)
            return 2.0
        return 2.0

    monkeypatch.chdir(tmp_path)
    real_popen = runner.subprocess.Popen
    launched = []

    def completed_popen(*args, **kwargs):
        nonlocal launched_process
        launched.append(args[0])
        launched_process = real_popen(*args, **kwargs)
        return launched_process

    monkeypatch.setattr(runner.subprocess, "Popen", completed_popen)
    monkeypatch.setattr(runner.time, "monotonic", observe_after_completion)
    outcome = runner._run_attempt(1, 1, 1.0, 0.1, log_dir, durations)

    assert outcome == "failure"
    assert len(launched) == 1
    assert "ordinary failure with status 9" in (log_dir / "attempt-1.supervisor.log").read_text()
    runner.logger.remove()


def test_changed_duration_input_cannot_be_accepted_or_retried(tmp_path):
    result = _run_step(
        tmp_path,
        1,
        '#!/bin/bash\nwhile [ "$1" != "--durations-path" ]; do shift; done\nprintf "{}" > "$2"\nexit 0\n',
    )
    assert result.returncode != 0
    assert "Duration input changed during attempt 1" in result.stderr
    assert "starting retry" not in result.stderr


@pytest.mark.parametrize("first_body", ["sleep 2", "while :; do echo active; done"])
def test_deadline_retries_one_complete_invocation(tmp_path, first_body):
    result = _run_step(
        tmp_path,
        3,
        f"""#!/bin/bash
count=$(cat count 2>/dev/null || echo 0)
count=$((count + 1)); echo "$count" > count
echo "attempt-$count-out"
echo "attempt-$count-err" >&2
if [ "$count" = 1 ]; then {first_body}; fi
exit 0
""",
    )

    assert result.returncode == 0
    assert (tmp_path / "count").read_text().strip() == "2"
    logs = tmp_path / "pr-test-logs/shard-3"
    assert "attempt-1-out" in (logs / "attempt-1.stdout.log").read_text()
    assert "attempt-1-err" in (logs / "attempt-1.stderr.log").read_text()
    assert "attempt-2-out" in (logs / "attempt-2.stdout.log").read_text()
    assert "starting retry" in result.stderr


def test_timeout_kills_term_resistant_child_before_retry(tmp_path):
    result = _run_step(
        tmp_path,
        1,
        """#!/bin/bash
count=$(cat count 2>/dev/null || echo 0)
count=$((count + 1)); echo "$count" > count
if [ "$count" = 1 ]; then
  bash -c 'trap "" TERM; while :; do echo child-alive >> child-events; sleep .03; done' &
  child=$!; echo "$child" > child-pid
  trap 'exit 0' TERM
  wait
fi
state=$(awk '{print $3}' "/proc/$(cat child-pid)/stat" 2>/dev/null || true)
[ -n "$state" ] && [ "$state" != Z ] && exit 22
echo retry-started >> child-events
""",
    )

    assert result.returncode == 0
    assert "retry-started" in (tmp_path / "child-events").read_text()
    child = int((tmp_path / "child-pid").read_text())
    stat = Path(f"/proc/{child}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_cancellation_kills_owned_term_resistant_group_without_retry(tmp_path):
    run = _prepare_step(
        tmp_path,
        1,
        """#!/bin/bash
        echo invoked >> invocations
        echo $$ > child-pid
        echo child-started > child-started
        trap '' TERM
        while :; do echo retained-diagnostic; sleep .03; done
        """,
        timeout="10",
        grace="0.1",
    )
    process = subprocess.Popen(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
        cwd=tmp_path,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 3
    while not (tmp_path / "child-started").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "child-started").exists()

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode != 0
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    logs = tmp_path / "pr-test-logs/shard-1"
    assert "retained-diagnostic" in (logs / "attempt-1.stdout.log").read_text()
    supervisor = (logs / "attempt-1.supervisor.log").read_text()
    assert "cancelled by signal" in supervisor
    assert "Escalating managed process group" in supervisor
    assert "starting retry" not in stderr
    child = int((tmp_path / "child-pid").read_text())
    stat = Path(f"/proc/{child}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
    assert "retained-diagnostic" in stdout


def test_unconfirmed_cancellation_cleanup_is_non_success_without_retry(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "test.sh").write_text(
        '#!/bin/bash\necho invoked >> invocations\nkill -TERM "$PPID"\nsleep .1\n',
        encoding="utf-8",
    )
    data = tmp_path / "tests/data"
    data.mkdir(parents=True)
    (data / "pr-test-durations.json").write_text('{"fixture": 1.0}', encoding="utf-8")
    spec = importlib.util.spec_from_file_location("cancel_cleanup_runner", ROOT / "scripts/run_pr_test_shard.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "_stop_group", lambda *_args: False)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pr_test_shard.py",
            "--group",
            "1",
            "--attempt-timeout",
            "10",
            "--termination-grace",
            ".1",
            "--max-attempts",
            "2",
            "--log-dir",
            "pr-test-logs/shard-1",
        ],
    )

    assert runner.main() != 0
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    supervisor = (tmp_path / "pr-test-logs/shard-1/attempt-1.supervisor.log").read_text()
    assert "cancelled by signal" in supervisor
    assert not (tmp_path / "pr-test-logs/shard-1/attempt-2.stdout.log").exists()


@pytest.mark.parametrize("completion_signal", [None, signal.SIGINT, signal.SIGTERM])
def test_cancellation_after_successful_child_exit_is_non_success(tmp_path, monkeypatch, completion_signal):
    """Exercise the real successful attempt before delivering the boundary signal."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "test.sh").write_text("#!/bin/bash\necho invoked >> invocations\necho retained-success-output\n", encoding="utf-8")
    data = tmp_path / "tests/data"
    data.mkdir(parents=True)
    (data / "pr-test-durations.json").write_text('{"fixture": 1.0}', encoding="utf-8")
    spec = importlib.util.spec_from_file_location("completion_boundary_runner", ROOT / "scripts/run_pr_test_shard.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    original_attempt = runner._run_attempt
    outcomes = []

    def deliver_after_success(*args):
        outcome = original_attempt(*args)
        assert outcome == "success"
        outcomes.append(outcome)
        if completion_signal is not None:
            signal.raise_signal(completion_signal)
            assert runner.cancel_signal == completion_signal
        return outcome

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "_run_attempt", deliver_after_success)
    monkeypatch.setattr(sys, "argv", ["run_pr_test_shard.py", "--group", "1", "--attempt-timeout", "10", "--max-attempts", "2", "--log-dir", "pr-test-logs/shard-1"])
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = runner.main()
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        runner.logger.remove()

    assert outcomes == ["success"]
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    logs = tmp_path / "pr-test-logs/shard-1"
    assert "retained-success-output" in (logs / "attempt-1.stdout.log").read_text()
    assert not (logs / "attempt-2.stdout.log").exists()
    diagnostic = (logs / "supervisor.log").read_text()
    assert "starting retry" not in diagnostic
    if completion_signal is None:
        assert result == 0
    else:
        assert result != 0
        assert "Cancellation observed after attempt 1" in diagnostic


def test_two_timeouts_fail_and_keep_both_logs(tmp_path):
    result = _run_step(tmp_path, 4, "#!/bin/bash\necho invoked >> invocations\nsleep 2\n")

    assert result.returncode != 0
    assert len((tmp_path / "invocations").read_text().splitlines()) == 2
    logs = tmp_path / "pr-test-logs/shard-4"
    assert (logs / "attempt-1.supervisor.log").exists()
    assert (logs / "attempt-2.supervisor.log").exists()


def test_completed_pytest_progress_permits_one_bounded_wall_extension(tmp_path):
    result = _run_step(
        tmp_path,
        3,
        "#!/bin/bash\necho invoked >> invocations\nsleep .10\necho 'tests/example.py::test_one PASSED [ 10%]'\nsleep .20\n",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    assert "extending bounded deadline" in result.stderr


def test_unrelated_output_cannot_extend_stalled_test_progress(tmp_path):
    result = _run_step(
        tmp_path,
        3,
        """#!/bin/bash
count=$(cat count 2>/dev/null || echo 0)
count=$((count + 1)); echo "$count" > count
if [ "$count" = 1 ]; then
  echo 'tests/example.py::test_one PASSED [ 10%]'
  while :; do echo 'unrelated process output'; sleep .02; done
fi
echo retry-succeeded
""",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "count").read_text().strip() == "2"
    assert "without a completed pytest test" in result.stderr
    assert "retry-succeeded" in result.stdout


def test_retry_removes_partial_coverage(tmp_path):
    result = _run_step(
        tmp_path,
        1,
        """#!/bin/bash
count=$(cat count 2>/dev/null || echo 0); count=$((count + 1)); echo "$count" > count
mkdir -p htmlcov
if [ "$count" = 1 ]; then echo stale > htmlcov/index.html; sleep 2; fi
echo fresh > htmlcov/index.html
""",
    )

    assert result.returncode == 0
    assert (tmp_path / "htmlcov/index.html").read_text().strip() == "fresh"
    partial = tmp_path / "pr-test-logs/shard-1/attempt-1-partial-reports/htmlcov/index.html"
    assert partial.read_text().strip() == "stale"


def test_workflow_contract_and_aggregate_shell():
    workflow = _workflow()
    assert workflow["name"] == "PR Tests"
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) >= {"lint", "tests-shard", "tests"}
    shard = workflow["jobs"]["tests-shard"]
    assert shard["strategy"] == {"fail-fast": False, "matrix": {"group": [1, 2, 3, 4]}}
    test_step = next(step for step in shard["steps"] if step["name"].startswith("Run tests"))
    assert test_step["timeout-minutes"] == 24
    assert "--attempt-timeout 480" in test_step["run"]
    assert "--termination-grace 10" in test_step["run"]
    assert "--max-attempts 2" in test_step["run"]
    assert "scripts/run_pr_test_shard.py" in test_step["run"]
    aggregate = workflow["jobs"]["tests"]
    assert aggregate["needs"] == ["lint", "tests-shard"]
    assert aggregate["if"] == "always()"
    aggregate_shell = aggregate["steps"][0]["run"]
    assert "needs.lint.result" in aggregate_shell
    assert "needs.tests-shard.result" in aggregate_shell


@pytest.mark.parametrize(
    "lint,shards,expected",
    [
        ("success", "success", 0),
        ("failure", "success", 1),
        ("cancelled", "success", 1),
        ("skipped", "success", 1),
        ("", "success", 1),
        ("success", "failure", 1),
        ("success", "cancelled", 1),
        ("success", "skipped", 1),
        ("success", "", 1),
    ],
)
def test_aggregate_requires_current_dependency_success(lint, shards, expected):
    shell = _workflow()["jobs"]["tests"]["steps"][0]["run"]
    shell = shell.replace("${{ needs.lint.result }}", lint).replace("${{ needs.tests-shard.result }}", shards)
    result = subprocess.run(["bash", "-c", shell], capture_output=True, text=True)
    assert (result.returncode == 0) == (expected == 0)
