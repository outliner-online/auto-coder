"""Coverage aggregation must account for every planned source shard."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from coverage import Coverage, CoverageData
from coverage.exceptions import CoverageException
from scripts.combine_source_coverage import combine

from auto_coder.source_ci_evidence import _artifact_name
from tests.test_tested_source_refs import source_plan

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "def choose(value):\n    if value:\n        return 1\n    return 0\n\ndef never_called():\n    return -1\n"


def _measure(source: Path, database: Path, value: bool) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    coverage = Coverage(data_file=str(database), source=[str(source.parent)], config_file=False)
    coverage.start()
    try:
        namespace: dict[str, object] = {"__name__": "sample"}
        exec(compile(SOURCE, str(source), "exec"), namespace)
        choose = namespace["choose"]
        assert callable(choose)
        assert choose(value) == int(value)
    finally:
        coverage.stop()
        coverage.save()


def _inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src/auto_coder/sample.py"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    plan = source_plan(tmp_path / "plan.json")
    artifacts = tmp_path / "artifacts"
    for shard, value in ((1, True), (2, False)):
        _measure(source, artifacts / _artifact_name(17, 2, shard) / "coverage/coverage.db", value)
    return source, plan, artifacts


def test_combines_every_planned_shard_and_keeps_unexecuted_lines_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, plan, artifacts = _inputs(tmp_path, monkeypatch)
    output = tmp_path / "reports"
    foreign = artifacts / _artifact_name(17, 1, 1) / "coverage/coverage.db"
    foreign.parent.mkdir(parents=True)
    stale_data = CoverageData(basename=str(foreign))
    stale_data.add_lines({str(source): {7}})
    stale_data.write()

    percent = combine(plan, artifacts, output)

    combined = Coverage(data_file=str(output / "coverage.db"), config_file=False)
    combined.load()
    assert set(combined.get_data().lines(str(source)) or ()) == {1, 2, 3, 4, 6}
    assert combined.analysis2(str(source))[3] == [7]
    assert percent == pytest.approx(100 * 5 / 6)
    assert (output / "htmlcov/index.html").is_file()
    assert "<coverage version=" in (output / "coverage.xml").read_text()
    assert "TOTAL" in (output / "summary.txt").read_text()


@pytest.mark.parametrize("defect", ["missing", "empty_file", "invalid_data", "no_measured_files"])
def test_incomplete_coverage_refuses_a_partial_report(defect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _source, plan, artifacts = _inputs(tmp_path, monkeypatch)
    broken = artifacts / _artifact_name(17, 2, 2) / "coverage/coverage.db"
    broken.unlink()
    if defect == "empty_file":
        broken.touch()
    elif defect == "invalid_data":
        broken.write_bytes(b"not a coverage database")
    elif defect == "no_measured_files":
        empty = CoverageData(basename=str(broken))
        empty.add_lines({})
        empty.write()
    output = tmp_path / "reports"

    with pytest.raises((ValueError, CoverageException)):
        combine(plan, artifacts, output)

    assert not (output / "coverage.xml").exists()
    assert not (output / "htmlcov/index.html").exists()
    assert not (output / "summary.txt").exists()


def test_workflow_measures_and_combines_only_verified_complete_source_coverage() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/test-source-for-runtime.yml").read_text())
    for job_name in ("source-plan", "complete-source-shard"):
        job = workflow["jobs"][job_name]
        assert "--cov" not in job["env"]["PYTEST_ADDOPTS"]
        assert job["env"]["COVERAGE_FILE"].startswith("${{ runner.temp }}/")
        command = next(step["run"] for step in job["steps"] if "--cov=src/auto_coder" in step.get("run", ""))
        assert "--cov-report=" in command
    steps = workflow["jobs"]["complete-source-tests"]["steps"]
    verification = next(i for i, step in enumerate(steps) if step.get("name") == "Verify exact current-attempt shard evidence")
    combining = next(i for i, step in enumerate(steps) if "scripts/combine_source_coverage.py" in step.get("run", ""))
    upload = next(i for i, step in enumerate(steps) if step.get("name") == "Upload combined source coverage")
    assert verification < combining < upload
    for index in (combining, upload):
        assert "if" not in steps[index]
        assert "continue-on-error" not in steps[index]
    assert steps[upload]["with"]["if-no-files-found"] == "error"
