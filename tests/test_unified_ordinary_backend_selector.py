from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from auto_coder.llm_backend_config import LLMBackendConfiguration
from auto_coder.quota_selector import BackendQuotaEvaluation, rank_high_score_backends_by_quota
from tests.test_jules_ordinary_startup import _invoke_only, _ordinary_issue_github, _write_config


def _mixed_config(selector: dict) -> LLMBackendConfiguration:
    return LLMBackendConfiguration.load_from_dict(
        {
            "backend": selector,
            "backends": {
                "remote-a": {"backend_type": "codex-cloud"},
                "local-b": {"backend_type": "codex"},
                "remote-c": {"backend_type": "claude-routine"},
            },
        }
    )


def test_mixed_priority_groups_round_trip_and_preserve_explicit_empty_pool(tmp_path):
    config = _mixed_config(
        {
            "priority_groups": [["remote-a", "local-b"], ["remote-c"]],
            "default": "local-b",
        }
    )
    assert config.get_ordinary_priority_groups() == [["remote-a", "local-b"], ["remote-c"]]

    path = tmp_path / "llm.toml"
    config.save_to_file(str(path))
    restored = LLMBackendConfiguration.load_from_file(str(path))
    assert restored.get_ordinary_priority_groups() == [["remote-a", "local-b"], ["remote-c"]]
    assert "backend_cloud" not in path.read_text(encoding="utf-8")

    empty = _mixed_config({"priority_groups": [], "default": "local-b"})
    assert empty.get_ordinary_priority_groups() == []


@pytest.mark.parametrize(
    "backend",
    [
        {"order": "codex"},
        {"priority_groups": ["codex"]},
        {"priority_groups": [[]]},
        {"priority_groups": [[""]]},
        {"order": ["missing"]},
        {"order": [], "priority_groups": []},
    ],
)
def test_invalid_ordinary_selector_is_rejected(backend):
    with pytest.raises(ValueError):
        LLMBackendConfiguration.load_from_dict({"backend": backend})


@pytest.mark.parametrize("retired_value", [{}, {"enabled": False}, "malformed"])
def test_backend_cloud_is_rejected_even_when_empty(retired_value):
    with pytest.raises(ValueError, match=r"backend_cloud.*\[backend\]"):
        LLMBackendConfiguration.load_from_dict({"backend": {"order": ["codex"]}, "backend_cloud": retired_value})


def test_base_backend_cloud_reports_its_source_before_override_merge(tmp_path, monkeypatch):
    base = tmp_path / "base.toml"
    base.write_text("[backend_cloud]\n", encoding="utf-8")
    override = tmp_path / "owner" / "repo" / "llm_config.toml"
    override.parent.mkdir(parents=True)
    override.write_text('[backend]\norder = ["codex"]\n', encoding="utf-8")
    monkeypatch.setattr(
        "auto_coder.llm_backend_config.resolve_repo_override_path",
        lambda _repo: str(override),
    )

    with pytest.raises(ValueError, match=str(base)):
        LLMBackendConfiguration.load_from_file(str(base), repo_name="owner/repo")


def test_quota_ranking_keeps_groups_and_first_duplicate(monkeypatch):
    observations = {
        "a": BackendQuotaEvaluation(backend_name="a", quota_surplus=-0.4, actual_remaining_ratio=0.1),
        "b": BackendQuotaEvaluation(backend_name="b", quota_surplus=0.2, actual_remaining_ratio=0.5),
        "c": BackendQuotaEvaluation(backend_name="c", quota_surplus=10.0, actual_remaining_ratio=1.0),
    }
    monkeypatch.setattr(
        "auto_coder.quota_selector.evaluate_backend_quota",
        lambda backend_name, **_kwargs: observations[backend_name],
    )

    ranked = rank_high_score_backends_by_quota(
        [["a", "b"], ["c", "a"]],
        _mixed_config({"priority_groups": [["remote-a"]]}),
        now=datetime.now(timezone.utc),
    )
    assert ranked == ["b", "a", "c"]


@pytest.mark.parametrize(
    "backends",
    [
        {"custom": {"enabled": True}},
        {"custom": {"backend_type": "invented"}, "invented": {"enabled": True}},
    ],
)
def test_selected_alias_must_resolve_to_supported_implementation(backends):
    with pytest.raises(ValueError, match="resolvable implementation type"):
        LLMBackendConfiguration.load_from_dict({"backend": {"order": ["custom", "codex"]}, "backends": backends})


def test_public_json_export_preserves_priority_groups(tmp_path):
    import json

    from click.testing import CliRunner

    from auto_coder.cli_commands_config import config_to_dict, import_config

    config = LLMBackendConfiguration.load_from_dict({"backend": {"priority_groups": [["codex", "claude"], ["qwen"]]}})
    exported = config_to_dict(config)

    assert exported["backend"] == {
        "default": "codex",
        "priority_groups": [["codex", "claude"], ["qwen"]],
    }
    restored = LLMBackendConfiguration.load_from_dict(exported)
    assert restored.get_ordinary_priority_groups() == [["codex", "claude"], ["qwen"]]

    source = tmp_path / "export.json"
    target = tmp_path / "imported.toml"
    source.write_text(json.dumps(exported), encoding="utf-8")
    result = CliRunner().invoke(import_config, ["--file", str(target), str(source)])
    assert result.exit_code == 0, result.output
    imported = LLMBackendConfiguration.load_from_file(str(target))
    assert imported.get_ordinary_priority_groups() == [["codex", "claude"], ["qwen"]]


def test_inherited_noedit_filters_task_only_backends():
    config = _mixed_config({"order": ["remote-a", "local-b"]})
    assert config.get_active_noedit_backends() == ["local-b"]
    assert config.get_noedit_default_backend() == "local-b"

    cloud_only = _mixed_config({"order": ["remote-a"]})
    assert cloud_only.get_active_noedit_backends() == []
    with pytest.raises(ValueError, match="No synchronous backend"):
        cloud_only.get_noedit_default_backend()


def test_ordinary_selector_filters_jules_alias_without_reordering_supported_candidates():
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"priority_groups": [["retired", "remote"], ["local"]]},
            "backends": {
                "retired": {"backend_type": "jules"},
                "remote": {"backend_type": "codex-cloud"},
                "local": {"backend_type": "codex"},
            },
        }
    )

    assert config.get_ordinary_priority_groups() == [["remote"], ["local"]]


def test_jules_only_ordinary_selector_has_no_dispatch_candidate():
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["retired"]},
            "backends": {"retired": {"backend_type": "jules"}},
        }
    )

    assert config.get_ordinary_priority_groups() == []


def test_cli_filters_jules_alias_and_dispatches_quota_ranked_supported_winner(tmp_path, monkeypatch):
    """REQ-006: CLI projection cannot reintroduce a Jules-resolving alias."""
    _write_config(
        tmp_path,
        '[backend]\npriority_groups = [["retired-alias", "codex-first", "claude-winner"]]\n\n'
        '[backends.retired-alias]\nbackend_type = "jules"\nenabled = true\napi_key = "unused"\n\n'
        '[backends.codex-first]\nbackend_type = "codex-cloud"\nenabled = true\n\n'
        '[backends.claude-winner]\nbackend_type = "claude-routine"\nenabled = true\n',
        monkeypatch,
    )
    github = _ordinary_issue_github(2892)
    quota = {
        "codex-first": BackendQuotaEvaluation("codex-first", quota_surplus=0.1, actual_remaining_ratio=0.2),
        "claude-winner": BackendQuotaEvaluation("claude-winner", quota_surplus=0.9, actual_remaining_ratio=1.0),
    }
    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.automation_engine.AutomationEngine._check_and_handle_closed_branch", return_value=True),
        patch("auto_coder.automation_engine.get_current_branch", return_value="main"),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch("auto_coder.quota_selector.evaluate_backend_quota", side_effect=lambda backend_name, **_kwargs: quota[backend_name]),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.submit_task") as codex_send,
        patch("auto_coder.claude_routine_client.ClaudeRoutineClient.fire_routine", return_value=("routine-2892", None)) as claude_send,
        patch("auto_coder.issue_processor.JulesClient.start_session") as jules_send,
    ):
        result = _invoke_only("owner/repo", 2892)

    assert result.exit_code == 0, result.output
    claude_send.assert_called_once()
    codex_send.assert_not_called()
    jules_send.assert_not_called()
    from auto_coder.issue_dispatch import DispatchOutcome, IssueAttemptIdentity, IssueDispatchGuard

    handoff = IssueDispatchGuard().inspect(IssueAttemptIdentity("owner", "repo", 2892, "0"))
    assert handoff is not None
    assert handoff.outcome is DispatchOutcome.REMOTE_ACCEPTED
    assert handoff.backend_name == "claude-winner"
    assert handoff.provider == "claude-routine"
    assert handoff.provider_reference == "routine-2892"


def test_cli_jules_only_policy_reports_no_dispatch_without_fallback(tmp_path, monkeypatch):
    """REQ-006: a Jules-only production configuration fails closed at startup."""
    _write_config(
        tmp_path,
        '[backend]\norder = ["retired-alias"]\n\n[backends.retired-alias]\nbackend_type = "jules"\nenabled = true\napi_key = "unused"\n',
        monkeypatch,
    )
    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance") as github_factory,
        patch("auto_coder.issue_processor.JulesClient.start_session") as jules_send,
        patch("auto_coder.codex_cloud_client.CodexCloudClient.submit_task") as codex_send,
    ):
        result = _invoke_only("owner/repo", 2893)

    assert result.exit_code != 0
    assert "ordinary backend candidate pool is empty" in result.output
    jules_send.assert_not_called()
    codex_send.assert_not_called()
    github_factory.assert_not_called()


@pytest.mark.parametrize("submission_outcome", ["accepted", "indeterminate"])
def test_cli_historical_jules_handoff_suppresses_supported_replacement(tmp_path, monkeypatch, submission_outcome):
    """REQ-006: production persistence retains accepted or uncertain ownership."""
    from auto_coder.cloud_manager import CloudManager
    from auto_coder.cloud_run import CloudRun, CloudRunRepository

    issue_number = 2894
    _write_config(
        tmp_path,
        '[backend]\norder = ["supported-cloud"]\n\n[backends.supported-cloud]\nbackend_type = "codex-cloud"\nenabled = true\n',
        monkeypatch,
    )
    task_id = "historical-jules-task" if submission_outcome == "accepted" else ""
    historical = CloudRun(
        "owner/repo",
        issue_number,
        0,
        "jules",
        task_id=task_id,
        backend_name="historical-jules",
        submission_outcome=submission_outcome,
    )
    stored, created = CloudRunRepository("owner/repo").acquire_submission_claim(historical)
    assert created is True
    assert stored == historical
    if task_id:
        assert CloudManager("owner/repo").add_session(
            issue_number,
            task_id,
            provider="jules",
            backend_name="historical-jules",
        )

    github = _ordinary_issue_github(issue_number)
    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.automation_engine.AutomationEngine._check_and_handle_closed_branch", return_value=True),
        patch("auto_coder.automation_engine.get_current_branch", return_value="main"),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.submit_task") as codex_send,
        patch("auto_coder.issue_processor.JulesClient.start_session") as jules_send,
    ):
        result = _invoke_only("owner/repo", issue_number)

    assert result.exit_code == 0, result.output
    codex_send.assert_not_called()
    jules_send.assert_not_called()
    from auto_coder.issue_dispatch import DispatchOutcome, IssueAttemptIdentity, IssueDispatchGuard

    retained = IssueDispatchGuard().inspect(IssueAttemptIdentity("owner", "repo", issue_number, "0"))
    assert retained is not None
    expected = DispatchOutcome.REMOTE_ACCEPTED if submission_outcome == "accepted" else DispatchOutcome.INDETERMINATE
    assert retained.outcome is expected
    assert retained.provider == "jules"
    assert retained.backend_name == "historical-jules"
    assert retained.provider_reference == task_id


def test_cli_difficult_issue_retains_indeterminate_jules_handoff_before_claude_send(tmp_path, monkeypatch):
    """REQ-004: the difficult route shares the durable attempt dispatch guard."""
    from auto_coder.cloud_run import CloudRun, CloudRunRepository
    from auto_coder.issue_dispatch import DispatchOutcome, IssueAttemptIdentity, IssueDispatchGuard

    issue_number = 2895
    _write_config(
        tmp_path,
        '[backend]\norder = ["ordinary-cloud"]\n\n' '[backend_with_high_score_cloud]\norder = ["difficult-claude"]\n\n' '[backends.ordinary-cloud]\nbackend_type = "codex-cloud"\nenabled = true\n\n' '[backends.difficult-claude]\nbackend_type = "claude-routine"\nenabled = true\n',
        monkeypatch,
    )
    historical = CloudRun(
        "owner/repo",
        issue_number,
        0,
        "jules",
        backend_name="historical-jules",
        submission_outcome="indeterminate",
    )
    stored, created = CloudRunRepository("owner/repo").acquire_submission_claim(historical)
    assert created is True
    assert stored == historical
    github = _ordinary_issue_github(issue_number)
    github.get_issue_dispatch_snapshot_strict.return_value["labels"].append({"name": "difficult"})

    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.automation_engine.AutomationEngine._check_and_handle_closed_branch", return_value=True),
        patch("auto_coder.automation_engine.get_current_branch", return_value="main"),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch("auto_coder.claude_routine_client.ClaudeRoutineClient.fire_routine") as claude_send,
        patch("auto_coder.codex_cloud_client.CodexCloudClient.submit_task") as codex_send,
    ):
        result = _invoke_only("owner/repo", issue_number)

    assert result.exit_code == 0, result.output
    claude_send.assert_not_called()
    codex_send.assert_not_called()
    retained = IssueDispatchGuard().inspect(IssueAttemptIdentity("owner", "repo", issue_number, "0"))
    assert retained is not None
    assert retained.outcome is DispatchOutcome.INDETERMINATE
    assert retained.provider == "jules"
    assert retained.backend_name == "historical-jules"


def test_cli_difficult_issue_without_high_score_config_retains_indeterminate_jules_handoff(tmp_path, monkeypatch):
    """REQ-004: the difficult local fallback cannot bypass historical ownership."""
    from auto_coder.cloud_run import CloudRun, CloudRunRepository
    from auto_coder.issue_dispatch import DispatchOutcome, IssueAttemptIdentity, IssueDispatchGuard

    issue_number = 2896
    _write_config(
        tmp_path,
        '[backend]\norder = ["ordinary-local"]\n\n' '[backends.ordinary-local]\nbackend_type = "codex"\nenabled = true\n',
        monkeypatch,
    )
    historical = CloudRun(
        "owner/repo",
        issue_number,
        0,
        "jules",
        backend_name="historical-jules",
        submission_outcome="indeterminate",
    )
    stored, created = CloudRunRepository("owner/repo").acquire_submission_claim(historical)
    assert created is True
    assert stored == historical
    github = _ordinary_issue_github(issue_number)
    github.get_issue_dispatch_snapshot_strict.return_value["labels"].append({"name": "difficult"})

    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.automation_engine.AutomationEngine._check_and_handle_closed_branch", return_value=True),
        patch("auto_coder.automation_engine.get_current_branch", return_value="main"),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch("auto_coder.issue_processor._take_issue_actions") as local_send,
    ):
        result = _invoke_only("owner/repo", issue_number)

    assert result.exit_code == 0, result.output
    local_send.assert_not_called()
    retained = IssueDispatchGuard().inspect(IssueAttemptIdentity("owner", "repo", issue_number, "0"))
    assert retained is not None
    assert retained.outcome is DispatchOutcome.INDETERMINATE
    assert retained.provider == "jules"
    assert retained.backend_name == "historical-jules"


def test_production_dispatch_skips_raw_jules_alias_before_local_fallback(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from auto_coder.automation_config import AutomationConfig
    from auto_coder.issue_dispatch import DispatchOutcome
    from auto_coder.issue_processor import _dispatch_issue_candidates

    monkeypatch.setenv("HOME", str(tmp_path))
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["retired", "codex"]},
            "backends": {"retired": {"backend_type": "jules"}},
        }
    )
    local = MagicMock(return_value=["local completed"])
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.issue_processor._take_issue_actions", local)
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)
    monkeypatch.setattr("auto_coder.cli_helpers.build_backend_manager", lambda **_kwargs: MagicMock())

    execution = _dispatch_issue_candidates(
        "owner/repo",
        {"number": 2891, "title": "Retired provider", "labels": []},
        AutomationConfig(),
        MagicMock(),
        ["retired", "codex"],
    )

    assert execution.result.outcome is DispatchOutcome.LOCAL_COMPLETED
    assert execution.result.backend_name == "codex"
    local.assert_called_once()


def test_historical_jules_acceptance_suppresses_supported_replacement(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from auto_coder.automation_config import AutomationConfig
    from auto_coder.issue_dispatch import AdapterOutcome, CandidateHandoff, DispatchOutcome, IssueAttemptIdentity, IssueDispatchGuard
    from auto_coder.issue_processor import _dispatch_issue_candidates

    monkeypatch.setenv("HOME", str(tmp_path))
    identity = IssueAttemptIdentity("owner", "repo", 2891, "0")
    guard = IssueDispatchGuard()
    accepted = guard.dispatch_candidates(
        identity,
        [CandidateHandoff("jules", "jules")],
        lambda _candidate: AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, "historical-session"),
    )
    assert accepted.outcome is DispatchOutcome.REMOTE_ACCEPTED

    config = LLMBackendConfiguration.load_from_dict({"backend": {"order": ["codex"]}})
    local = MagicMock(return_value=["must not run"])
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.issue_processor._take_issue_actions", local)
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)

    execution = _dispatch_issue_candidates(
        "owner/repo",
        {"number": 2891, "title": "Owned attempt", "labels": []},
        AutomationConfig(),
        MagicMock(),
        ["codex"],
    )

    assert execution.result.outcome is DispatchOutcome.REMOTE_ACCEPTED
    assert execution.result.provider_reference == "historical-session"
    local.assert_not_called()


def test_message_manager_never_constructs_inherited_cloud_candidate(monkeypatch):
    from unittest.mock import MagicMock

    from auto_coder.cli_helpers import build_message_backend_manager

    config = _mixed_config({"order": ["remote-a", "local-b"]})
    local_client = MagicMock()
    temporary = MagicMock(
        _clients={"local-b": local_client},
        _factories={"local-b": MagicMock()},
    )
    built = MagicMock(return_value=temporary)
    final_manager = MagicMock()
    monkeypatch.setattr("auto_coder.cli_helpers.get_llm_config", lambda: config)
    monkeypatch.setattr("auto_coder.cli_helpers.build_backend_manager", built)
    noedit_instance = MagicMock(side_effect=[final_manager, final_manager])
    monkeypatch.setattr(
        "auto_coder.backend_manager.LLMBackendManager.get_noedit_instance",
        noedit_instance,
    )

    assert build_message_backend_manager() is final_manager
    assert built.call_args.kwargs["selected_backends"] == ["local-b"]
    assert built.call_args.kwargs["primary_backend"] == "local-b"


def test_ordinary_dispatch_runs_opencode_before_later_codex(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from auto_coder.automation_config import AutomationConfig
    from auto_coder.issue_dispatch import DispatchOutcome
    from auto_coder.issue_processor import _dispatch_issue_candidates

    monkeypatch.setenv("HOME", str(tmp_path))
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["local-open", "codex"]},
            "backends": {"local-open": {"backend_type": "opencode"}},
        }
    )
    built = []
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        "auto_coder.cli_helpers.build_backend_manager",
        lambda **kwargs: built.append(kwargs["selected_backends"]) or MagicMock(),
    )
    monkeypatch.setattr("auto_coder.issue_processor._take_issue_actions", lambda *_args, **_kwargs: ["done"])
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)

    execution = _dispatch_issue_candidates(
        "owner/repo",
        {"number": 2079},
        AutomationConfig(),
        MagicMock(),
        ["local-open", "codex"],
    )

    assert execution.result.outcome is DispatchOutcome.LOCAL_COMPLETED
    assert execution.result.backend_name == "local-open"
    assert built == [["local-open"]]


def test_ordinary_dispatch_reaches_candidate_when_binding_is_attributed_to_another_attempt(monkeypatch, tmp_path):
    """Issue #2285: a legacy binding already owned by another attempt must not
    refuse an independently authorized ordinary dispatch for this attempt."""
    from unittest.mock import MagicMock

    from auto_coder.automation_config import AutomationConfig
    from auto_coder.cloud_manager import CloudManager
    from auto_coder.cloud_run import CloudRun, CloudRunRepository
    from auto_coder.issue_dispatch import DispatchOutcome, IssueAttemptIdentity, IssueDispatchGuard
    from auto_coder.issue_processor import _dispatch_issue_candidates

    monkeypatch.setenv("HOME", str(tmp_path))
    repo_name = "owner/repo"
    issue_number = 2079

    # A previous attempt ("5") was already durably accepted by a remote
    # provider and recorded in the production cloud.csv projection.
    assert CloudRunRepository(repo_name).save(CloudRun(repo_name, issue_number, 5, "codex-cloud", "old-task", "remote-old"))
    assert CloudManager(repo_name).add_session(issue_number, "old-task", "codex-cloud", "remote-old")
    owner, repository = repo_name.split("/", 1)
    imported = IssueDispatchGuard().inspect(IssueAttemptIdentity(owner, repository, issue_number, "5"))
    assert imported is not None and imported.outcome == DispatchOutcome.REMOTE_ACCEPTED

    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["local-open"]},
            "backends": {"local-open": {"backend_type": "opencode"}},
        }
    )
    built = []
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        "auto_coder.cli_helpers.build_backend_manager",
        lambda **kwargs: built.append(kwargs["selected_backends"]) or MagicMock(),
    )
    monkeypatch.setattr("auto_coder.issue_processor._take_issue_actions", lambda *_args, **_kwargs: ["done"])
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)

    execution = _dispatch_issue_candidates(
        repo_name,
        {"number": issue_number},
        AutomationConfig(),
        MagicMock(),
        ["local-open"],
    )

    # Ordinary dispatch (no explicit retry authorization) reaches its
    # candidate instead of being refused for attempt "5"'s sake.
    assert execution.result.outcome is DispatchOutcome.LOCAL_COMPLETED
    assert built == [["local-open"]]

    # Attempt "5"'s own accepted record is untouched.
    still_accepted = IssueDispatchGuard().inspect(IssueAttemptIdentity(owner, repository, issue_number, "5"))
    assert still_accepted is not None
    assert still_accepted.outcome == DispatchOutcome.REMOTE_ACCEPTED
    assert still_accepted.provider_reference == "old-task"
