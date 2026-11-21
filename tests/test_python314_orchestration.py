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
import pytest

@pytest.fixture(autouse=True)
def record(request):
    with open(os.environ['CI_SHARD_TEST_RECORD'], 'a') as stream:
        stream.write(request.node.nodeid + '\\n')

@pytest.mark.parametrize('value', range(85))
def test_parameterized(value):
    assert value != FAIL_VALUE, 'actual native advisory failure'

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


@pytest.mark.parametrize("fail_value", [-1, 42])
def test_workflow_native_shards_preserve_exact_non_live_selection_and_failures(tmp_path: Path, fail_value: int) -> None:
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
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "f" * 40,
        "AUTO_CODER_REQUIRE_BROWSER": "1",
        "AUTO_CODER_IMMEDIATE_TEST_FAILURES": "1",
    }
    plan_command = _command("python314-plan", "Collect complete non-live inventory and immutable assignments")
    plan_command = plan_command.replace("${{ github.event.pull_request.head.sha || github.sha }}", sha).replace("$RUNNER_TEMP", str(runner)).replace("$GITHUB_OUTPUT", str(output))
    planned = _run(project, *_native_command(plan_command), env_patch=environment)
    assert planned.returncode == 0, planned.stdout + planned.stderr
    plan, _raw = read_plan(runner / "python314-plan/plan.json")
    assert plan.context.profile_id == "python314-advisory-v1"
    expected = [f"tests/test_sample.py::test_parameterized[{value}]" for value in range(85)] + [f"tests/test_sample.py::test_{profile}_profile" for profile in ("browser", "muse", "supervisor")]
    assert list(plan.inventory) == expected
    matrix = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(matrix["matrix"]) == {"shard": list(range(1, len(plan.shards) + 1))}
    assert int(matrix["count"]) == len(plan.shards) > 1
    assert plan.context.origin["run_attempt"] == "2"
    assert plan.context.checkout_sha == sha != environment["GITHUB_SHA"]
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
    for key, prefix in (("python314-plan", "python314-plan"), ("python314-shard", "python314-shard")):
        upload = next(step for step in jobs[key]["steps"] if step.get("uses") == "actions/upload-artifact@v7")
        assert upload["if"] == "always()"
        assert upload["with"]["name"].startswith(prefix + "-${{ github.run_id }}-${{ github.run_attempt }}")
    assert not any(step.get("continue-on-error", False) for step in jobs["python314-plan"]["steps"])
