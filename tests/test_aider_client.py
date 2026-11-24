"""Exercise optional API availability, framing and the caller's backend rotation."""

import builtins
import importlib.machinery
import json
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from auto_coder.aider_client import AiderClient
from auto_coder.exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from auto_coder.llm_backend_config import LLMBackendConfiguration
from auto_coder.utils import CommandExecutor


@pytest.fixture
def aider_config(monkeypatch):
    config = LLMBackendConfiguration.load_from_dict({"backends": {"aider": {"model": "test/model", "options": ["--map-tokens", "0"], "options_for_noedit": ["--map-tokens", "1"]}}})
    monkeypatch.setattr("auto_coder.aider_client.get_llm_config", lambda: config)
    return config


@pytest.fixture
def fake_aider_api(monkeypatch):
    boundary = SimpleNamespace(response='{"decision":"pass"}', diagnostic="Aider banner and token summary", error=None, calls=[], prompts=[], env=[])

    class Coder:
        @staticmethod
        def create(**kwargs):
            boundary.calls.append(kwargs)
            print("Creating coder banner")
            return Coder()

        def run(self, prompt):
            boundary.prompts.append(prompt)
            boundary.env.append(dict(os.environ))
            print(prompt)  # Real InputOutput also displays user input.
            print(boundary.diagnostic, file=sys.stderr)
            if boundary.error:
                raise boundary.error
            return boundary.response

    def model(name):
        print("Model diagnostic")
        return name

    for name, attrs in [("aider", {}), ("aider.coders", {"Coder": Coder}), ("aider.models", {"Model": model}), ("aider.io", {"InputOutput": lambda **kwargs: kwargs})]:
        module = ModuleType(name)
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
    return boundary


def deny_aider_imports(monkeypatch):
    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "aider" or name.startswith("aider."):
            raise ModuleNotFoundError("No module named 'aider'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)


@pytest.mark.parametrize("noedit", [False, True, "constructor"])
def test_api_content_is_separate_from_diagnostics_and_argv(monkeypatch, aider_config, fake_aider_api, capsys, noedit):
    command = Mock(side_effect=AssertionError("Aider must not invoke CommandExecutor"))
    monkeypatch.setattr(CommandExecutor, "run_command", command)
    monkeypatch.delenv("AIDER_ANALYTICS", raising=False)
    client = AiderClient(backend_name="aider", use_noedit_options=noedit == "constructor")
    client.output_logger = Mock()
    client.set_extra_args(["--verbose"])
    prompt = 'PRIVATE-PROMPT @file "quotes"\n$(touch should-not-exist); 日本語'
    output = client._run_llm_cli(prompt, is_noedit=noedit is True)
    assert json.loads(output) == {"decision": "pass"}
    assert output == fake_aider_api.response
    assert fake_aider_api.prompts == [prompt]
    assert fake_aider_api.calls[0]["dry_run"] is bool(noedit)
    assert fake_aider_api.calls[0]["edit_format"] == ("ask" if noedit else None)
    assert fake_aider_api.calls[0]["auto_commits"] is False
    assert fake_aider_api.calls[0]["dirty_commits"] is False
    assert fake_aider_api.env[0]["AIDER_ANALYTICS"] == "false"
    assert fake_aider_api.env[0]["AIDER_MAP_TOKENS"] == ("1" if noedit else "0")
    assert fake_aider_api.env[0]["AIDER_VERBOSE"] == "true"
    assert "AIDER_ANALYTICS" not in os.environ
    assert client.consume_extra_args() == []
    captured = capsys.readouterr()
    assert prompt not in captured.out + captured.err
    assert fake_aider_api.diagnostic not in captured.out + captured.err
    assert prompt not in " ".join(sys.argv)
    command.assert_not_called()
    assert client.output_logger.log_interaction.call_args.kwargs["status"] == "success"


def test_runtime_noedit_switch_preserves_ordinary_mode(aider_config, fake_aider_api):
    client = AiderClient(backend_name="aider")
    client.output_logger = Mock()
    for noedit in [False, True, False]:
        assert client._run_llm_cli("hello", is_noedit=noedit) == fake_aider_api.response
    assert [call["dry_run"] for call in fake_aider_api.calls] == [False, True, False]
    assert [call["edit_format"] for call in fake_aider_api.calls] == [None, "ask", None]


@pytest.mark.parametrize("response", [None, "", "   "])
def test_empty_api_response_does_not_return_diagnostics(aider_config, fake_aider_api, response):
    fake_aider_api.response = response
    client = AiderClient(backend_name="aider")
    client.output_logger = Mock()
    with pytest.raises(RuntimeError, match="Aider returned no model response"):
        client._run_llm_cli("hello", is_noedit=True)
    interaction = client.output_logger.log_interaction.call_args.kwargs
    assert interaction["status"] == "error"
    assert interaction["response"] == ""
    assert fake_aider_api.diagnostic not in interaction["error"]


@pytest.mark.parametrize("error", [RuntimeError("provider failure"), AutoCoderTimeoutError("timed out"), AutoCoderUsageLimitError("rate limit")])
def test_api_errors_restore_environment_and_log_failure(monkeypatch, aider_config, fake_aider_api, error):
    monkeypatch.delenv("AIDER_ANALYTICS", raising=False)
    fake_aider_api.error = error
    client = AiderClient(backend_name="aider")
    client.output_logger = Mock()
    with pytest.raises(type(error), match=str(error)):
        client._run_llm_cli("hello")
    assert "AIDER_ANALYTICS" not in os.environ
    interaction = client.output_logger.log_interaction.call_args.kwargs
    assert interaction["status"] == "error"
    assert interaction["error"] == str(error)


def test_diagnostic_usage_limit_is_an_error_without_becoming_response(aider_config, fake_aider_api):
    fake_aider_api.response = None
    fake_aider_api.diagnostic = "rate limit"
    client = AiderClient(backend_name="aider")
    client.output_logger = Mock()
    with pytest.raises(AutoCoderUsageLimitError, match="Aider reported a usage limit"):
        client._run_llm_cli("hello")
    assert client.output_logger.log_interaction.call_args.kwargs["response"] == ""


def test_missing_aider_has_actionable_error(monkeypatch, aider_config):
    deny_aider_imports(monkeypatch)
    with pytest.raises(RuntimeError, match="Install aider-chat manually in the Python environment"):
        AiderClient(backend_name="aider")


def test_default_orders_do_not_select_optional_aider():
    config = LLMBackendConfiguration()
    assert config.get_ordinary_priority_groups() == [["codex"]]
    assert config.get_active_noedit_backends() == ["codex"]
    assert config.get_local_pr_repair_backends() == ["codex"]


def test_selected_aider_alias_fails_at_manager_construction(monkeypatch, aider_config):
    from auto_coder.cli_helpers import build_backend_manager
    from auto_coder.llm_backend_config import BackendConfig

    aider_config.backends["optional-aider"] = BackendConfig(name="optional-aider", backend_type="aider")
    monkeypatch.setattr("auto_coder.cli_helpers.get_llm_config", lambda: aider_config)
    deny_aider_imports(monkeypatch)
    with pytest.raises(RuntimeError, match="Install aider-chat manually"):
        build_backend_manager(["optional-aider"], "optional-aider", {})


def test_manager_rotates_past_missing_optional_aider(tmp_path, monkeypatch, aider_config):
    from auto_coder.backend_manager import BackendManager
    from auto_coder.backend_state_manager import BackendStateManager
    from auto_coder.llm_backend_config import BackendConfig

    deny_aider_imports(monkeypatch)
    monkeypatch.setattr("auto_coder.backend_manager.get_llm_config", lambda: aider_config)
    first = Mock(config_backend=BackendConfig(name="codex", backend_type="codex-cloud"))
    first._run_llm_cli.side_effect = AutoCoderUsageLimitError("rate limit")
    last = Mock(config_backend=BackendConfig(name="claude-routine", backend_type="claude-routine"))
    last._run_llm_cli.return_value = "fallback response"
    factories = {"codex": lambda: first, "aider": AiderClient, "claude-routine": lambda: last}
    manager = BackendManager("codex", first, factories, order=list(factories), automatic_session_resume=False)
    manager._state_manager = BackendStateManager(str(tmp_path / "backend-state.json"))
    assert manager._run_llm_cli("hello", is_noedit=True) == "fallback response"
    assert manager._current_backend_name() == "claude-routine"
    first._run_llm_cli.assert_called_once_with("hello", is_noedit=True)
    last._run_llm_cli.assert_called_once_with("hello", is_noedit=True)
    assert manager._clients["aider"] is None


def test_aider_prerequisite_fails_without_installing(monkeypatch):
    import click

    from auto_coder.cli_helpers import check_backend_prerequisites

    monkeypatch.setattr("auto_coder.cli_helpers.importlib.util.find_spec", lambda name: None)
    with pytest.raises(click.ClickException, match="Install aider-chat manually"):
        check_backend_prerequisites(["aider"])


def test_manually_installed_api_alias_needs_no_executable(monkeypatch, aider_config, fake_aider_api):
    from auto_coder.cli_helpers import build_backend_manager, check_backend_prerequisites
    from auto_coder.llm_backend_config import BackendConfig

    aider_config.backends["installed-aider"] = BackendConfig(name="installed-aider", backend_type="aider", model="test/model")
    monkeypatch.setattr("auto_coder.cli_helpers.get_llm_config", lambda: aider_config)
    monkeypatch.setattr("auto_coder.cli_helpers.shutil.which", lambda name: None)
    check_backend_prerequisites(["installed-aider"])
    manager = build_backend_manager(["installed-aider"], "installed-aider", {}, automatic_session_resume=False)
    assert isinstance(manager._clients["installed-aider"], AiderClient)
    assert manager._clients["installed-aider"].model_name == "test/model"
