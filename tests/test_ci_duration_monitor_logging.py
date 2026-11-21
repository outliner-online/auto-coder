"""Verify real monitor sinks survive Auto-Coder's logger initialization."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("_use_real_commands")

PROBE = """
import sys
from types import SimpleNamespace
from scripts.ci_duration_monitor import main

class EmptyGitHub:
    def request(self, method, path, payload=None):
        assert method == 'GET'
        if path.startswith('actions/workflows/'):
            return SimpleNamespace(status=200, data={'total_count': 0, 'workflow_runs': []})
        if path.startswith('issues?'):
            return SimpleNamespace(status=200, data=[])
        raise AssertionError(path)

def create_client(token, repository):
    from auto_coder.logger_config import setup_logger
    setup_logger()
    if sys.argv[1] == 'failure':
        raise RuntimeError('intentional transport bootstrap failure')
    return EmptyGitHub()

raise SystemExit(main(sys.argv[2:], client_factory=create_client))
"""


@pytest.mark.parametrize("bootstrap", ["success", "failure"])
def test_real_project_logger_bootstrap_preserves_stdout_and_monitor_file(tmp_path: Path, bootstrap: str) -> None:
    repository = Path(__file__).resolve().parents[1]
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps({"workflows": [{"path": ".github/workflows/pr-tests.yml", "families": []}]}), encoding="utf-8")
    output = tmp_path / "output"
    environment = {**os.environ, "PYTHONPATH": str(repository / "src"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"), "AUTO_CODER_LOG_FILE": str(tmp_path / "project.log")}
    result = subprocess.run([sys.executable, "-c", PROBE, bootstrap, "--repository", "example/project", "--config", str(config), "--output-dir", str(output)], cwd=repository, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == (0 if bootstrap == "success" else 1), result.stdout + result.stderr
    expected = "Monitor completed; 0 family actions" if bootstrap == "success" else "CI duration monitor failed: intentional transport bootstrap failure"
    assert expected in result.stdout
    assert "ci_duration_monitor.py:main:" in result.stdout
    file_log = (output / "monitor.log").read_text(encoding="utf-8")
    assert expected in file_log
    assert "ci_duration_monitor.py:main:" in file_log
