"""Configured provider resolution retained after removing the unused sweep."""

from unittest.mock import Mock, patch

import pytest

from auto_coder.cloud_task_engine import CloudTaskEngine


@pytest.mark.parametrize(
    ("provider", "module", "client_name", "kwargs"),
    [
        ("jules", "jules_client", "JulesClient", {}),
        ("codex-cloud", "codex_cloud_client", "CodexCloudClient", {"repo_name": "o/r"}),
        ("claude-routine", "claude_routine_client", "ClaudeRoutineClient", {"backend_name": "named-routine", "repo_name": "o/r"}),
    ],
)
def test_resolves_only_requested_provider(provider, module, client_name, kwargs):
    with patch(f"auto_coder.{module}.{client_name}") as factory:
        result = CloudTaskEngine().get_client_for_provider(provider, "o/r", "named-routine")
    factory.assert_called_once_with(**kwargs)
    assert result is factory.return_value


def test_injected_routine_resolution_preserves_backend_identity():
    client_type = type("ClaudeRoutineClient", (), {})
    first, second = client_type(), client_type()
    first.backend_name, second.backend_name = "first", "second"
    engine = CloudTaskEngine(clients=[first, second])

    assert engine.get_client_for_provider("claude-routine", "o/r", "second") is second
    assert engine.get_client_for_provider("claude-routine", "o/r", "missing") is None
    assert engine.get_client_for_provider("codex-cloud", "o/r") is None


def test_unknown_provider_does_not_discover_or_start_work():
    with patch("auto_coder.jules_client.JulesClient") as jules:
        assert CloudTaskEngine().get_client_for_provider("unsupported", "o/r") is None
    jules.assert_not_called()


def test_resolution_does_not_enumerate_or_resume_injected_tasks():
    client = type("JulesClient", (), {})()
    client.list_tasks = Mock(side_effect=AssertionError("unexpected task discovery"))
    client.continue_if_paused = Mock(side_effect=AssertionError("unexpected continuation"))

    assert CloudTaskEngine(clients=[client]).get_client_for_provider("jules", "o/r") is client
    client.list_tasks.assert_not_called()
    client.continue_if_paused.assert_not_called()
