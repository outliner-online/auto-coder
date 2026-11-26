"""Regressions for retiring Codex login publication from process startup."""

from pathlib import Path

import httpx
from click.testing import CliRunner

from auto_coder.cli_commands_main import process_issues


def test_legacy_config_cannot_read_or_publish_codex_auth_during_startup(monkeypatch, tmp_path):
    config_dir = tmp_path / ".auto-coder"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[github_secrets]\nenabled = true\ntoken = "retired-secret-token"\nrepository = "owner/name"\n',
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    original = b'{"tokens":{"access_token":"local-auth-marker"}}\n'
    auth_path.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    auth_reads = []
    read_bytes = Path.read_bytes

    def record_read(path):
        if path == auth_path:
            auth_reads.append(path)
        return read_bytes(path)

    sent = []

    def refuse_transport(self, request):
        sent.append(request)
        raise AssertionError("No startup publication may reach a transport")

    monkeypatch.setattr(Path, "read_bytes", record_read)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse_transport)

    class BackendConfig:
        def get_ordinary_priority_groups(self):
            return [["codex"]]

        def resolve_backend_type(self, _backend_name):
            return "codex"

    initialized = []

    def stop_after_github_initialization(*args, **kwargs):
        initialized.append((args, kwargs))
        raise RuntimeError("ordinary-github-initialized")

    monkeypatch.setattr("auto_coder.cli_commands_main.get_llm_config", lambda **kwargs: BackendConfig())
    monkeypatch.setattr("auto_coder.cli_commands_main.set_active_repo_name", lambda _repo: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.is_cloud_mode_enabled", lambda **kwargs: False)
    monkeypatch.setattr("auto_coder.cli_commands_main.build_models_map", lambda: {})
    monkeypatch.setattr("auto_coder.cli_commands_main.setup_logger", lambda **kwargs: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.setup_progress_footer_logging", lambda: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.start_health_monitoring", lambda: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.get_github_token_or_fail", lambda _token: "ordinary-token")
    monkeypatch.setattr("auto_coder.cli_commands_main.check_backend_prerequisites", lambda _backends: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.ensure_test_script_or_fail", lambda: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.print_configuration_summary", lambda *args: None)
    monkeypatch.setattr("auto_coder.cli_commands_main.GitHubClient.get_instance", stop_after_github_initialization)

    result = CliRunner().invoke(process_issues, ["--repo", "owner/name"])

    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "ordinary-github-initialized"
    assert initialized == [(("ordinary-token",), {"disable_labels": False})]
    assert auth_reads == []
    assert sent == []
    assert read_bytes(auth_path) == original


def test_retired_sync_option_is_not_silently_accepted():
    result = CliRunner().invoke(process_issues, ["--skip-codex-auth-secret-sync"])

    assert result.exit_code == 2
    assert "No such option: --skip-codex-auth-secret-sync" in result.output
