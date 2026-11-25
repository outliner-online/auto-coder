"""Regression coverage for retiring Jules from ordinary Issue dispatch.

These tests exercise the real configuration loader (via ``AUTO_CODER_CONFIG_PATH``
and a ``HOME`` pointed at a temporary directory) and the real
``build_backend_manager_from_config`` / ``build_backend_manager`` / ordinary
dispatcher boundary rather than mocking them, so the fix is proven at the
production-reachable boundary described by the issue rather than only at a
newly changed helper.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from auto_coder.automation_config import AutomationConfig
from auto_coder.cli_commands_main import process_issues
from auto_coder.llm_backend_config import TASK_ONLY_BACKEND_TYPES, LLMBackendConfiguration, get_llm_config


def _write_config(tmp_path, toml_text: str, monkeypatch) -> None:
    config_path = tmp_path / "llm_config.toml"
    config_path.write_text(toml_text, encoding="utf-8")
    monkeypatch.setenv("AUTO_CODER_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("HOME", str(tmp_path))


def _synchronous_backends(config: LLMBackendConfiguration) -> list:
    """Mirror the synchronous-manager projection computed in ``process_issues``."""
    selected_backends = [name for group in config.get_ordinary_priority_groups() for name in group]
    return [name for name in selected_backends if config.resolve_backend_type(name) not in TASK_ONLY_BACKEND_TYPES]


# ---------------------------------------------------------------------------
# Configuration-layer coverage: the synchronous-manager projection of the
# ordinary pool, as computed by process_issues() in cli_commands_main.py.
# ---------------------------------------------------------------------------


def test_ordinary_synchronous_backends_excludes_task_only_types():
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["jules", "local-b", "remote-a"]},
            "backends": {
                "jules": {"backend_type": "jules"},
                "local-b": {"backend_type": "codex"},
                "remote-a": {"backend_type": "codex-cloud"},
            },
        }
    )
    assert config.get_ordinary_priority_groups() == [["local-b"], ["remote-a"]]
    assert _synchronous_backends(config) == ["local-b"]


def test_ordinary_synchronous_backends_empty_for_all_remote_pool():
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["jules"]},
            "backends": {"jules": {"backend_type": "jules", "enabled": True}},
        }
    )
    assert _synchronous_backends(config) == []


def test_ordinary_synchronous_backends_ignores_unlisted_enabled_backends():
    """An enabled local backend that is not part of the ordinary selector must
    never leak into the synchronous projection just because the ordinary pool
    is remote-only (this is the ``cli_backends=[]`` all-configured-backends trap
    named in the issue's implementation notes, one layer down)."""
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["jules"]},
            "backends": {
                "jules": {"backend_type": "jules", "enabled": True},
                "local-unlisted": {"backend_type": "codex", "enabled": True},
            },
        }
    )
    assert _synchronous_backends(config) == []


def test_ordinary_synchronous_backends_preserves_priority_group_order():
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"priority_groups": [["remote-a"], ["local-b", "local-c"]]},
            "backends": {
                "remote-a": {"backend_type": "claude-routine"},
                "local-b": {"backend_type": "codex"},
                "local-c": {"backend_type": "qwen"},
            },
        }
    )
    assert _synchronous_backends(config) == ["local-b", "local-c"]


# ---------------------------------------------------------------------------
# CLI bootstrap coverage: process-issues --only over a real configuration file
# ---------------------------------------------------------------------------


def _invoke_only(repo_name: str, issue_number: int, extra_args=()):
    return CliRunner().invoke(
        process_issues,
        [
            "--repo",
            repo_name,
            "--github-token",
            "token",
            "--only",
            f"https://github.com/{repo_name}/issues/{issue_number}",
            *extra_args,
        ],
        catch_exceptions=False,
    )


def test_process_issues_only_bootstraps_mixed_pool_using_only_local_synchronous_manager(tmp_path, monkeypatch):
    """A retired Jules entry cannot prevent startup of a supported local candidate."""
    _write_config(
        tmp_path,
        ('[backend]\norder = ["jules", "codex"]\n\n' '[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "test-jules-key"\n\n' '[backends.codex]\nbackend_type = "codex"\nenabled = true\n'),
        monkeypatch,
    )

    engine = MagicMock()
    engine.process_single.return_value = {
        "repository": "owner/repo",
        "issues_processed": [],
        "prs_processed": [],
        "errors": [],
    }

    captured_backend_manager_kwargs = {}
    from auto_coder import cli_helpers

    real_build_backend_manager_from_config = cli_helpers.build_backend_manager_from_config

    def spy_build_backend_manager_from_config(**kwargs):
        captured_backend_manager_kwargs.update(kwargs)
        return real_build_backend_manager_from_config(**kwargs)

    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=MagicMock()),
        patch("auto_coder.cli_commands_main.AutomationEngine", return_value=engine),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch("auto_coder.cli_commands_main.build_backend_manager_from_config", spy_build_backend_manager_from_config),
    ):
        result = _invoke_only("owner/repo", 1591)

    assert result.exit_code == 0, result.output
    # The synchronous manager is scoped to the supported local projection.
    assert captured_backend_manager_kwargs.get("cli_backends") == ["codex"]
    engine.process_single.assert_called_once()


def test_process_issues_only_explicit_empty_pool_still_fails_closed(tmp_path, monkeypatch):
    """An explicitly empty/disabled-only ordinary pool must keep producing its
    existing explicit diagnostic; this must not be resurrected into a
    default backend just because the synchronous-projection change touched
    the same startup block (REQ-002)."""
    _write_config(
        tmp_path,
        '[backend]\norder = ["codex"]\n\n[backends.codex]\nenabled = false\n',
        monkeypatch,
    )

    with patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"):
        result = _invoke_only("owner/repo", 1591)

    assert result.exit_code != 0
    assert "ordinary backend candidate pool is empty" in result.output


@pytest.mark.parametrize(
    ("backend_type", "provider_patch", "provider_result"),
    [
        (
            "codex-cloud",
            "auto_coder.codex_cloud_client.CodexCloudClient.submit_task",
            None,
        ),
        ("claude-routine", "auto_coder.claude_routine_client.ClaudeRoutineClient.fire_routine", ("routine-2891", None)),
    ],
)
def test_cli_startup_reaches_supported_cloud_dispatch_without_jules(
    tmp_path,
    monkeypatch,
    backend_type,
    provider_patch,
    provider_result,
):
    """REQ-006: real CLI policy and ordinary admission select each supported provider."""
    from auto_coder.codex_cloud_client import CodexSubmissionOutcome, CodexSubmissionResult

    if backend_type == "codex-cloud":
        provider_result = CodexSubmissionResult(CodexSubmissionOutcome.ACCEPTED, "task-2891")
    alias = f"supported-{backend_type}"
    _write_config(
        tmp_path,
        f'[backend]\norder = ["{alias}"]\n\n[backends.{alias}]\nbackend_type = "{backend_type}"\nenabled = true\n',
        monkeypatch,
    )
    issue = {
        "number": 2891,
        "title": "Supported cloud route",
        "body": "## Requirements\nREQ-001: Route this ordinary Issue.",
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    github = MagicMock(token="token")
    github.get_item_type_strict.return_value = "issue"
    github.get_issue_dispatch_snapshot_strict.return_value = issue
    github.get_issue.return_value = issue
    github.get_issue_details.return_value = issue
    github.get_all_sub_issues.return_value = []
    github.get_direct_sub_issues_strict.return_value = []
    github.get_parent_issue_details_strict.return_value = None
    github.get_parent_issue_details.return_value = None
    github.get_issue_comments_strict.return_value = []
    github.get_open_issue_declarations.return_value = []
    github.try_add_labels.return_value = True

    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.automation_engine.AutomationEngine._check_and_handle_closed_branch", return_value=True),
        patch("auto_coder.automation_engine.get_current_branch", return_value="main"),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch(provider_patch, return_value=provider_result) as provider_send,
        patch("auto_coder.issue_processor.JulesClient.start_session") as jules_send,
    ):
        result = _invoke_only("owner/repo", 2891)

    assert result.exit_code == 0, result.output
    provider_send.assert_called_once()
    jules_send.assert_not_called()


# ---------------------------------------------------------------------------
# Ordinary dispatcher coverage: _dispatch_issue_candidates over real config
# ---------------------------------------------------------------------------


def test_dispatch_candidates_local_first_pool_never_touches_jules(tmp_path, monkeypatch):
    from auto_coder.issue_dispatch import DispatchOutcome
    from auto_coder.issue_processor import _dispatch_issue_candidates
    from auto_coder.jules_client import JulesClient

    _write_config(
        tmp_path,
        ('[backend]\norder = ["codex", "jules"]\n\n' '[backends.codex]\nbackend_type = "codex"\nenabled = true\n\n' '[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "test-jules-key"\n'),
        monkeypatch,
    )
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_a: 0)
    monkeypatch.setattr("auto_coder.issue_processor._take_issue_actions", lambda *_args, **_kwargs: ["done locally"])

    jules_started = MagicMock()
    monkeypatch.setattr(JulesClient, "start_session", jules_started)

    github = MagicMock()
    issue_data = {"number": 77, "title": "T", "body": "B", "labels": [], "state": "open", "user": {"login": "x"}}

    execution = _dispatch_issue_candidates(
        "owner/repo",
        issue_data,
        AutomationConfig(repo_name="owner/repo"),
        github,
        ["codex", "jules"],
    )

    assert execution.result.outcome is DispatchOutcome.LOCAL_COMPLETED
    assert execution.result.backend_name == "codex"
    jules_started.assert_not_called()
