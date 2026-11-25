"""Regression tests for retirement of the Jules-only local-test bypass."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from auto_coder.automation_config import AutomationConfig
from auto_coder.cli_commands_main import fix_to_pass_tests_command
from auto_coder.cli_helpers import ensure_test_script_or_fail
from auto_coder.fix_to_pass_tests_runner import WorkspaceFixResult, fix_to_pass_tests, run_local_tests


def test_required_script_failure_does_not_enable_legacy_mode(monkeypatch):
    monkeypatch.setenv("AUTOCODER_JULES_ONLY_MODE", "true")

    runner = CliRunner()
    with runner.isolated_filesystem(), patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"), patch("auto_coder.cli_commands_main.get_llm_config") as llm_config:
        llm_config.return_value.get_active_backends.return_value = ["codex"]
        llm_config.return_value.backend_order = ["codex"]
        result = runner.invoke(fix_to_pass_tests_command)

    assert result.exit_code != 0
    assert "Required test script not found: scripts/test.sh" in result.output
    assert "AUTOCODER_JULES_ONLY_MODE" not in result.output
    assert not hasattr(AutomationConfig(), "JULES_ONLY_MODE")


def test_shared_runner_executes_real_script_despite_legacy_environment(monkeypatch, tmp_path):
    script = tmp_path / "test.sh"
    script.write_text("#!/usr/bin/env bash\nprintf 'real test observation\\n'\nexit 7\n", encoding="utf-8")
    config = AutomationConfig()
    config.TEST_SCRIPT_PATH = str(script)
    monkeypatch.setenv("AUTOCODER_JULES_ONLY_MODE", "true")

    result = run_local_tests(config, test_file="tests/example.py")

    assert result["success"] is False
    assert result["return_code"] == 7
    assert result["output"] == "real test observation\n"
    assert result["command"] == f"bash {script} tests/example.py"


def test_legacy_environment_cannot_exceed_configured_fix_attempt_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOCODER_JULES_ONLY_MODE", "true")
    invocation_log = tmp_path / "test-script-invocations.log"
    invocation_log.touch()
    script = tmp_path / "test.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'count=$(wc -l < "$AUTOCODER_TEST_INVOCATION_LOG")\n'
        'printf "run\\n" >> "$AUTOCODER_TEST_INVOCATION_LOG"\n'
        'printf "FAILED tests/example.py::test_failure\\nAssertionError: real observed failure invocation %s with stable output padding\\n" "$((count + 1))"\n'
        "exit 7\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOCODER_TEST_INVOCATION_LOG", str(invocation_log))
    config = AutomationConfig(automatic_test_fix=True)
    config.TEST_SCRIPT_PATH = str(script)
    config.MAX_FIX_ATTEMPTS = 2
    fix_result = WorkspaceFixResult("attempted fix", "changed code", "codex", "default", "codex")

    with (
        patch("auto_coder.fix_to_pass_tests_runner.check_for_updates_and_restart"),
        patch(
            "auto_coder.fix_to_pass_tests_runner.get_isolate_single_test_on_failure_from_config",
            return_value=False,
        ),
        patch("auto_coder.fix_to_pass_tests_runner.apply_workspace_test_fix", return_value=fix_result) as apply_fix,
    ):
        summary = fix_to_pass_tests(config, MagicMock())

    assert summary["success"] is False
    assert summary["attempts"] == 2
    assert invocation_log.read_text(encoding="utf-8").splitlines() == ["run", "run"]
    observed_result = apply_fix.call_args.args[1]
    assert observed_result["return_code"] == 7
    assert "real observed failure invocation 1" in observed_result["output"]
    assert observed_result["command"] == f"bash {script}"
    assert any("Change below 10% threshold" in message for message in summary["messages"])


def test_missing_script_helper_raises_without_mutating_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTOCODER_JULES_ONLY_MODE", raising=False)

    try:
        ensure_test_script_or_fail()
    except Exception as exc:
        assert str(exc) == "Required test script not found: scripts/test.sh"
    else:
        raise AssertionError("missing required script was accepted")

    assert "AUTOCODER_JULES_ONLY_MODE" not in __import__("os").environ
