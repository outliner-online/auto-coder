"""Exercise the advisory workflow's real planner, native shards and result gate."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest
import yaml
from scripts.plan_python314_tests import HARNESS

from auto_coder.ci_shard_plan import read_plan
from tests.test_ci_shard_plan import ROOT, _project, _run

pytestmark = pytest.mark.usefixtures("_use_real_commands")
WORKFLOW = ROOT / ".github/workflows/python-314-compatibility.yml"
SOURCE = """
import os
from pathlib import Path
import pytest

@pytest.fixture(autouse=True)
def record(request):
    with open(os.environ['CI_SHARD_TEST_RECORD'], 'a') as stream:
        stream.write(request.node.nodeid + '\\n')

@pytest.mark.parametrize('value', range(85))
def test_parameterized(value):
    assert value != FAIL_VALUE or (
        os.environ.get('ADVISORY_FAILURE_TRIGGER')
        and not Path(os.environ['ADVISORY_FAILURE_TRIGGER']).exists()
    ), 'actual native advisory failure'

@pytest.mark.browser
def test_browser_profile(): pass

@pytest.mark.muse_live
def test_muse_profile(): pass

@pytest.mark.supervisor_integration
def test_supervisor_profile(): pass

@pytest.mark.opencode_live
def test_only_excluded_profile():
    raise AssertionError('must be deselected')
"""


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _command(job: str, name: str) -> str:
    return next(step["run"] for step in _workflow()["jobs"][job]["steps"] if step.get("name") == name)


def _native_command(script: str) -> list[str]:
    command = shlex.split(script)
    assert command[:3] == ["uv", "run", "python"]
    # The outer supported runner already selected this locked interpreter.
    return command[3:]


def _planned_project(tmp_path: Path, fail_value: int, *, attempt: str = "2") -> tuple[Path, Path, Path, dict[str, str], str]:
    project, _context_path, _context = _project(tmp_path, source=SOURCE.replace("FAIL_VALUE", str(fail_value)))
    for relative in HARNESS:
        if relative == "tests/conftest.py":
            continue
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    config = project / "pyproject.toml"
    config.write_text(config.read_text().replace('markers = ["slow: marker excluded by the CI profile"]', 'markers = ["browser", "muse_live", "supervisor_integration", "opencode_live"]'), encoding="utf-8")
    for command in (("add", "-A"), ("commit", "-qm", "advisory native fixture")):
        subprocess.run(["git", *command], cwd=project, check=True, capture_output=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
    runner = tmp_path / "runner"
    output = tmp_path / "github-output"
    environment = {
        "GITHUB_REPOSITORY": "outliner-online/auto-coder",
        "GITHUB_WORKFLOW": _workflow()["name"],
        "GITHUB_RUN_ID": "17",
        "GITHUB_RUN_ATTEMPT": attempt,
        "GITHUB_SHA": "f" * 40,
        "GITHUB_REF": "refs/pull/2881/merge",
        "AUTO_CODER_REQUIRE_BROWSER": "1",
        "AUTO_CODER_IMMEDIATE_TEST_FAILURES": "1",
    }
    plan_command = _command("python314-plan", "Collect complete non-live inventory and immutable assignments")
    plan_command = plan_command.replace("${{ github.event.pull_request.head.sha || github.sha }}", sha).replace("$RUNNER_TEMP", str(runner)).replace("$GITHUB_OUTPUT", str(output))
    planned = _run(project, *_native_command(plan_command), env_patch=environment)
    assert planned.returncode == 0, planned.stdout + planned.stderr
    return project, runner, output, environment, sha


def _verify_command(runner: Path, sha: str, outputs: dict[str, str], *, result: str = "success") -> list[str]:
    script = _command("python314-shard", "Verify authoritative successful planner binding")
    replacements = {
        "${{ github.event.pull_request.head.sha || github.sha }}": sha,
        "${{ needs.python314-plan.result }}": result,
        "${{ needs.python314-plan.outputs.artifact }}": outputs.get("artifact", ""),
        "${{ needs.python314-plan.outputs.digest }}": outputs.get("digest", ""),
        "$RUNNER_TEMP": str(runner),
    }
    for old, new in replacements.items():
        script = script.replace(old, new)
    return _native_command(script)


@pytest.mark.parametrize("fail_value", [-1, 42])
def test_workflow_native_shards_preserve_exact_non_live_selection_and_failures(tmp_path: Path, fail_value: int) -> None:
    project, runner, output, environment, sha = _planned_project(tmp_path, fail_value)
    plan, _raw = read_plan(runner / "python314-plan/plan.json")
    assert plan.context.profile_id == "python314-advisory-v1"
    assert json.loads(_raw)["semantic"]["source_floor_ms"] == 5_000
    expected = [f"tests/test_sample.py::test_parameterized[{value}]" for value in range(85)] + [f"tests/test_sample.py::test_{profile}_profile" for profile in ("browser", "muse", "supervisor")]
    assert list(plan.inventory) == expected
    matrix = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(matrix["matrix"]) == {"shard": list(range(1, len(plan.shards) + 1))}
    assert int(matrix["count"]) == len(plan.shards) > 1
    assert plan.context.origin["run_attempt"] == "2"
    assert plan.context.checkout_sha == sha != environment["GITHUB_SHA"]
    verified = _run(project, *_verify_command(runner, sha, matrix), env_patch=environment)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    observed: list[str] = []
    statuses = []
    for shard in range(1, len(plan.shards) + 1):
        record = tmp_path / f"executed-{shard}"
        script = _command("python314-shard", "Run complete non-live test selection")
        script = script.replace("${{ matrix.shard }}", str(shard)).replace("$RUNNER_TEMP", str(runner))
        result = _run(project, *_native_command(script), record=record, env_patch=environment)
        expected_failure = fail_value >= 0 and f"tests/test_sample.py::test_parameterized[{fail_value}]" in plan.shards[shard - 1]
        assert result.returncode == (1 if expected_failure else 0), result.stdout + result.stderr
        nodes = record.read_text().splitlines()
        assert nodes == list(plan.shards[shard - 1])
        observed.extend(nodes)
        junit = ET.parse(runner / "python314-shard/results.xml")
        assert len(junit.findall(".//testcase")) == len(nodes)
        assert len(junit.findall(".//failure")) == int(expected_failure)
        if expected_failure:
            assert "actual native advisory failure" in result.stdout
        statuses.append("failure" if result.returncode else "success")
    assert Counter(observed) == Counter(expected)
    aggregate = _command("python314-compatibility", "Require successful quality, complete planning and every native test shard")
    gate = subprocess.run(["bash", "-e", "-c", aggregate], env={**os.environ, "PLAN_RESULT": "success", "SHARDS_RESULT": "failure" if "failure" in statuses else "success"}, capture_output=True)
    assert (gate.returncode == 0) == (fail_value == -1)


def test_failed_only_native_rerun_uses_retained_successful_planner_without_replanning(tmp_path: Path) -> None:
    project, runner, output, environment, sha = _planned_project(tmp_path, 42, attempt="1")
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    download = next(step for step in _workflow()["jobs"]["python314-shard"]["steps"] if step.get("uses") == "actions/download-artifact@v8")
    assert download["with"]["name"] == "${{ needs.python314-plan.outputs.artifact }}"
    assert outputs["artifact"] == "python314-plan-17-1"
    plan_path = runner / "python314-plan/plan.json"
    original = plan_path.read_bytes()
    # Inert same-run artifact service: only the successful producer's upload exists.
    artifact = tmp_path / "artifact-store" / outputs["artifact"]
    artifact.parent.mkdir()
    shutil.move(str(plan_path.parent), artifact)
    shutil.copytree(artifact, plan_path.parent)
    plan, _raw = read_plan(plan_path)
    trigger = tmp_path / "transient-service-failure"
    trigger.touch()
    environment["ADVISORY_FAILURE_TRIGGER"] = str(trigger)
    failed = []
    retained_records = {}
    for shard in range(1, len(plan.shards) + 1):
        verified = _run(project, *_verify_command(runner, sha, outputs), env_patch=environment)
        assert verified.returncode == 0, verified.stdout + verified.stderr
        record = tmp_path / f"first-{shard}"
        script = _command("python314-shard", "Run complete non-live test selection").replace("${{ matrix.shard }}", str(shard)).replace("$RUNNER_TEMP", str(runner))
        run = _run(project, *_native_command(script), record=record, env_patch=environment)
        assert record.read_text().splitlines() == list(plan.shards[shard - 1])
        if run.returncode:
            assert run.returncode == 1 and "actual native advisory failure" in run.stdout
            failed.append((shard, script))
        else:
            retained_records[record] = record.read_bytes()
    assert len(failed) == 1 and retained_records
    environment["GITHUB_RUN_ATTEMPT"] = "2"
    trigger.unlink()  # Same committed tests/profile; the external transient clears.
    shutil.rmtree(plan_path.parent)
    shutil.copytree(artifact, plan_path.parent)  # Retained needs outputs select attempt 1.
    verified = _run(project, *_verify_command(runner, sha, outputs), env_patch=environment)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    shard, script = failed[0]
    record = tmp_path / "retry"
    retried = _run(project, *_native_command(script), record=record, env_patch=environment)
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert record.read_text().splitlines() == list(plan.shards[shard - 1])
    assert all(path.read_bytes() == contents for path, contents in retained_records.items())
    assert plan_path.read_bytes() == original and output.read_text().count("artifact=") == 1
    gate = _command("python314-compatibility", "Require successful quality, complete planning and every native test shard")
    assert subprocess.run(["bash", "-e", "-c", gate], env={**os.environ, "PLAN_RESULT": "success", "SHARDS_RESULT": "success"}, capture_output=True).returncode == 0


def test_retained_planner_refuses_missing_stale_or_contradictory_evidence(tmp_path: Path) -> None:
    project, runner, output, environment, sha = _planned_project(tmp_path, -1, attempt="1")
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    environment["GITHUB_RUN_ATTEMPT"] = "2"
    failures = [
        ({}, {**outputs, "artifact": ""}, sha, "success"),
        ({}, {**outputs, "digest": ""}, sha, "success"),
        ({}, {**outputs, "digest": "sha256:" + "0" * 64}, sha, "success"),
        ({}, {**outputs, "artifact": "python314-plan-17-2"}, sha, "success"),
        ({}, outputs, sha, "failure"),
        ({}, outputs, sha, "skipped"),
        ({"GITHUB_RUN_ID": "18"}, outputs, sha, "success"),
        ({"GITHUB_REPOSITORY": "other/repository"}, outputs, sha, "success"),
        ({"GITHUB_REF": "refs/heads/main"}, outputs, sha, "success"),
        ({"GITHUB_SHA": "e" * 40}, outputs, sha, "success"),
        ({"GITHUB_WORKFLOW": "Other workflow"}, outputs, sha, "success"),
        ({"GITHUB_RUN_ATTEMPT": "0"}, outputs, sha, "success"),
        ({}, outputs, "a" * 40, "success"),
    ]
    for patch, pointer, source, result in failures:
        rejected = _run(project, *_verify_command(runner, source, pointer, result=result), env_patch={**environment, **patch})
        assert rejected.returncode != 0, (patch, pointer, source, result)
    plan_path = runner / "python314-plan/plan.json"
    original = plan_path.read_bytes()
    changed = json.loads(original)
    changed["origin"]["planning_attempt"] = "python314-plan-17-2"
    plan_path.write_text(json.dumps(changed))
    assert _run(project, *_verify_command(runner, sha, outputs), env_patch=environment).returncode != 0
    plan_path.write_bytes(original)
    plan_path.unlink()
    assert _run(project, *_verify_command(runner, sha, outputs), env_patch=environment).returncode != 0


@pytest.mark.parametrize("dependency", ["PLAN_RESULT", "SHARDS_RESULT"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", ""])
def test_advisory_aggregate_rejects_every_incomplete_or_unsuccessful_dependency(dependency: str, result: str) -> None:
    job = _workflow()["jobs"]["python314-compatibility"]
    assert job["name"] == "Python 3.14 source quality and non-live tests (advisory)"
    assert job["if"] == "always()"
    assert job["needs"] == ["python314-plan", "python314-shard"]
    assert not job.get("continue-on-error", False)
    script = _command("python314-compatibility", "Require successful quality, complete planning and every native test shard")
    env = {**os.environ, "PLAN_RESULT": "success", "SHARDS_RESULT": "success", dependency: result}
    assert subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True).returncode != 0


def test_workflow_runs_every_shard_and_retains_plan_junit_and_timing_on_failure() -> None:
    jobs = _workflow()["jobs"]
    shard = jobs["python314-shard"]
    assert shard["strategy"] == {"fail-fast": False, "matrix": "${{ fromJSON(needs.python314-plan.outputs.matrix) }}"}
    assert shard["timeout-minutes"] == 120
    tests = next(step for step in shard["steps"] if step.get("name") == "Run complete non-live test selection")
    assert tests["env"]["AUTO_CODER_CI_TIMING_DIR"] == "${{ runner.temp }}/python314-shard/timing"
    assert jobs["python314-plan"]["outputs"]["artifact"] == "${{ steps.plan.outputs.artifact }}"
    assert jobs["python314-plan"]["outputs"]["digest"] == "${{ steps.plan.outputs.digest }}"
    names = [step.get("name") for step in shard["steps"]]
    assert names.index("Download authoritative successful planner assignments") < names.index("Verify authoritative successful planner binding") < names.index("Prepare required Python-only management fixture") < names.index("Run complete non-live test selection")
    for key, prefix in (("python314-plan", "python314-plan"), ("python314-shard", "python314-shard")):
        upload = next(step for step in jobs[key]["steps"] if step.get("uses") == "actions/upload-artifact@v7")
        assert upload["if"] == "always()"
        assert upload["with"]["name"].startswith(prefix + "-${{ github.run_id }}-${{ github.run_attempt }}")
    assert not any(step.get("continue-on-error", False) for step in jobs["python314-plan"]["steps"])
