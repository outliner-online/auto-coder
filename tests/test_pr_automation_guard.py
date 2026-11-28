"""PR-history stop policy from strict REST reads through production admission."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auto_coder import pr_processor
from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome, ProcessedPRResult, PRProcessingOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.pr_automation_guard import PRCommit, PRCommitHistory, PRGuardStore, assess_pr_automation
from auto_coder.util.gh_cache import GitHubClient


def history(count: int, *, start: int = 1, revert: int = 0) -> PRCommitHistory:
    commits = tuple(PRCommit(f"{index:040x}", 'Revert "failed correction"' if index == revert else f"Implement change {index}") for index in range(start, start + count))
    return PRCommitHistory(commits[-1].sha if commits else "0" * 40, count, commits)


def reader(value: PRCommitHistory):
    return SimpleNamespace(get_pr_automation_history_strict=Mock(return_value=value))


@pytest.mark.parametrize("message,expected", [('Revert "broken fix"', True), ("revert: broken fix", True), ("Undo bad correction\n\nThis reverts commit " + "a" * 40 + ".", True), ("Fix revert detection", False), ("Implement a revert button", False), ("Prevent reverting prior fixes", False)])
def test_revert_detection_is_explicit(message: str, expected: bool) -> None:
    assert PRCommit("a" * 40, message).is_revert is expected


def test_first_revert_latches_without_reading_again_after_restart_or_head_change(tmp_path) -> None:
    path = tmp_path / "guard.sqlite3"
    client = reader(history(2, revert=2))
    first = assess_pr_automation(client, "Owner/Repo", 42, store=PRGuardStore(path))
    assert first.stopped and first.revert_sha == f"{2:040x}"
    client.get_pr_automation_history_strict.return_value = history(1, start=50)
    again = assess_pr_automation(client, " owner/repo ", 42, store=PRGuardStore(path))
    assert again == first
    assert client.get_pr_automation_history_strict.call_count == 1
    assert len(PRGuardStore(path).events("OWNER/REPO", 42)) == 1
    assert assess_pr_automation(client, "owner/other", 42, store=PRGuardStore(path)).allowed


def test_commit_boundary_uses_distinct_observed_commits(tmp_path) -> None:
    store = PRGuardStore(tmp_path / "guard.sqlite3")
    assert store.observe("owner/repo", 42, history(29)).allowed
    assert store.observe("owner/repo", 42, history(29)).observed_commits == 29
    # A rewritten branch cannot erase work the controller has already observed.
    stopped = store.observe("owner/repo", 42, history(1, start=50))
    assert stopped.stopped and stopped.observed_commits == 30
    assert "30/30" in stopped.reason
    assert store.observe("owner/repo", 43, history(30)).stopped
    assert store.observe("owner/repo", 44, history(97)).stopped


def test_resume_requires_exact_head_and_reason_and_only_acknowledges_prior_reverts(tmp_path) -> None:
    store = PRGuardStore(tmp_path / "guard.sqlite3")
    previous = history(30, revert=25)
    assert store.observe("owner/repo", 42, previous).stopped
    for expected_head, reason in (("wrong", "investigated"), (previous.head_sha, " ")):
        with pytest.raises(ValueError):
            store.resume("owner/repo", 42, previous, expected_head=expected_head, reason=reason, expected_stop_generation=store.stop_generation("owner/repo", 42))
    assert store.status("owner/repo", 42).stopped
    granted = store.resume("OWNER/REPO", 42, previous, expected_head=previous.head_sha, reason="Fixed the underlying contract", expected_stop_generation=store.stop_generation("owner/repo", 42))
    assert granted.allowed and granted.observed_commits == 0
    assert store.observe("owner/repo", 42, history(59, revert=25)).allowed
    assert store.observe("owner/repo", 42, history(60, revert=25)).stopped
    store.resume("owner/repo", 42, history(60, revert=25), expected_head=f"{60:040x}", reason="Second external intervention", expected_stop_generation=store.stop_generation("owner/repo", 42))
    assert store.observe("owner/repo", 42, history(61, revert=61)).stopped
    assert [event["event"] for event in store.events("owner/repo", 42)] == ["stopped", "operator_resumed", "stopped", "operator_resumed", "stopped"]


def test_incomplete_or_unavailable_history_never_allows_or_invents_a_latch(tmp_path) -> None:
    store = PRGuardStore(tmp_path / "guard.sqlite3")
    client = reader(history(1))
    client.get_pr_automation_history_strict.side_effect = OSError("unavailable")
    assert assess_pr_automation(client, "owner/repo", 42, store=store).disposition == "deferred"
    assert not store.status("owner/repo", 42).stopped
    assert assess_pr_automation(object(), "owner/repo", 42, store=store).disposition == "deferred"
    assert store.observe("owner/repo", 42, PRCommitHistory("a", 5, (), complete=False)).disposition == "deferred"
    assert store.observe("owner/repo", 42, PRCommitHistory("a", 300, (), complete=False)).stopped
    with pytest.raises(ValueError):
        store.resume("owner/repo", 42, PRCommitHistory("a", 300, (), complete=False), expected_head="a", reason="reviewed", expected_stop_generation=store.stop_generation("owner/repo", 42))


def test_unknown_schema_and_corrupt_baseline_defer(tmp_path) -> None:
    path = tmp_path / "guard.sqlite3"
    store = PRGuardStore(path)
    assert store.observe("owner/repo", 42, history(2)).allowed
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE pr_guard SET baseline='{}'")
    assert assess_pr_automation(reader(history(3)), "owner/repo", 42, store=store).disposition == "deferred"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE pr_guard_metadata SET value='999'")
    with pytest.raises(ValueError, match="schema"):
        PRGuardStore(path)


def _strict_client(monkeypatch, count: int, *, fail_page: int = 0, change_head: bool = False):
    client = GitHubClient("test-token")
    complete = history(count)
    reads: list[tuple[str, int]] = []
    metadata_reads = 0

    def request(method, url, **kwargs):
        nonlocal metadata_reads
        assert method == "GET"
        if url.endswith("/commits"):
            page = kwargs["params"]["page"]
            reads.append(("commits", page))
            if page == fail_page:
                raise OSError("partial page failed")
            payload = [{"sha": item.sha, "commit": {"message": item.message}} for item in complete.commits[(page - 1) * 100 : page * 100]]
        else:
            metadata_reads += 1
            reads.append(("metadata", metadata_reads))
            payload = {"number": 42, "head": {"sha": "different" if change_head and metadata_reads > 1 else complete.head_sha}, "base": {"sha": "base"}, "commits": count}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    monkeypatch.setattr("auto_coder.util.gh_cache._strict_request", request)
    return client, reads


def test_real_strict_reader_paginates_and_rechecks_before_allowing(monkeypatch) -> None:
    client, reads = _strict_client(monkeypatch, 124)
    observed = client.get_pr_automation_history_strict("owner/repo", 42)
    assert observed.commit_count == len(observed.commits) == 124
    assert reads == [("metadata", 1), ("commits", 1), ("commits", 2), ("metadata", 2)]
    assert client.get_pr_automation_history_strict("owner/repo", 42) == observed
    assert reads[-1] == ("metadata", 3)
    assert len(reads) == 5


@pytest.mark.parametrize("failure", ["page", "changed-head"])
def test_real_strict_reader_rejects_partial_and_moving_snapshots(monkeypatch, failure) -> None:
    client, _ = _strict_client(monkeypatch, 124, fail_page=2 if failure == "page" else 0, change_head=failure == "changed-head")
    with pytest.raises((OSError, ValueError)):
        client.get_pr_automation_history_strict("owner/repo", 42)


def test_production_pr_ingress_blocks_before_cleanup_review_or_provider(monkeypatch) -> None:
    client = reader(history(3, revert=2))
    effects = Mock(side_effect=AssertionError("stopped PR reached a business effect"))
    monkeypatch.setattr(pr_processor, "_reject_unsafe_codex_cloud_pr", effects)
    monkeypatch.setattr(pr_processor, "_close_empty_pr", effects)
    result = pr_processor.process_pull_request(client, AutomationConfig(), "owner/repo", {"number": 42}, force_adversarial_validation=True)
    assert result.outcome is PRProcessingOutcome.BLOCKED
    assert "external intervention" in result.target_reason
    effects.assert_not_called()


def test_production_engine_common_entry_blocks_before_adjudication_or_slots() -> None:
    engine = AutomationEngine.__new__(AutomationEngine)
    engine.github = reader(history(30))
    engine.refresh_review_adjudications = Mock(side_effect=AssertionError("must stop before review"))
    result = engine._process_single_candidate_unified_impl("owner/repo", Candidate(type="pr", data={"number": 42}, priority=0), AutomationConfig(), explicit_only=True, force=True)
    assert result.target_outcome is ExplicitTargetOutcome.BLOCKED
    assert result.outcome is PRProcessingOutcome.BLOCKED
    assert not result.refill_retry_required and result.retry_not_before is None
    engine.refresh_review_adjudications.assert_not_called()


def test_saved_correction_and_final_merge_share_the_latch(monkeypatch) -> None:
    client = reader(history(30))
    pr_data = {"number": 42}
    status = ProcessedPRResult(pr_data=pr_data, actions_taken=[])
    effects = Mock(side_effect=AssertionError("must not load/replay a saved correction"))
    monkeypatch.setattr(pr_processor, "EffectiveDecisionStore", effects)
    actions = []
    retained, stop = pr_processor._resume_closure_before_admission(client, "owner/repo", pr_data, "head", True, actions, status, AutomationConfig())
    assert retained is None and stop and status.outcome is PRProcessingOutcome.BLOCKED
    assert not pr_processor._merge_pr("owner/repo", 42, {}, AutomationConfig(), github_client=client)
    assert not pr_processor._review_publication_authorized(client, "owner/repo", 42, "head", "branch")
    effects.assert_not_called()
    assert client.get_pr_automation_history_strict.call_count == 1


def test_worker_acknowledges_stop_without_retry_and_dashboard_shows_intervention(tmp_path, monkeypatch, _use_real_sleep) -> None:
    import asyncio

    from auto_coder.dashboard import prepare_log_rows
    from auto_coder.trace_logger import get_trace_logger

    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "queue.sqlite3"))
    engine = AutomationEngine(Mock(), AutomationConfig())
    engine.github = reader(history(2, revert=2))
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda _repo, _kind, number, *_args, **_kwargs: Candidate("pr", {"number": number, "head": {"sha": f"{2:040x}"}}, 0))
    monkeypatch.setattr(engine, "_check_if_pr_merged_or_closed", lambda *_args: False)

    async def run_worker():
        await engine.invalidate_entity("owner/repo", "pr", 42)
        task = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        try:
            for _ in range(300):
                events = get_trace_logger().get_logs(item_type="pr", item_number=42)
                if any(event["details"].get("outcome") == "blocked" for event in events) and engine.invalidations.pending_count("owner/repo") == 0:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("worker did not acknowledge the latched stop")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run_worker())
    events = [event for event in get_trace_logger().get_logs(item_type="pr", item_number=42) if event["details"].get("outcome") == "blocked"]
    assert len(events) == 1
    assert events[0]["details"]["requires_external_intervention"] is True
    assert "Revert commit" in prepare_log_rows(events)[0]["details"]
    assert engine.github.get_pr_automation_history_strict.call_count == 1


def test_complete_history_cache_is_invalidated_by_base_change(monkeypatch) -> None:
    client = GitHubClient("test-token")
    original = history(2)
    base = "base-a"
    commit_reads = []

    def request(method, url, **_kwargs):
        assert method == "GET"
        if url.endswith("/commits"):
            commit_reads.append(base)
            commits = original.commits if base == "base-a" else (PRCommit("b" * 40, 'Revert "previous base change"'), original.commits[-1])
            payload = [{"sha": item.sha, "commit": {"message": item.message}} for item in commits]
        else:
            payload = {"number": 42, "head": {"sha": original.head_sha}, "base": {"sha": base}, "commits": 2}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    monkeypatch.setattr("auto_coder.util.gh_cache._strict_request", request)
    assert not any(item.is_revert for item in client.get_pr_automation_history_strict("owner/repo", 42).commits)
    base = "base-b"
    changed = client.get_pr_automation_history_strict("owner/repo", 42)
    assert changed.head_sha == original.head_sha and changed.commit_count == original.commit_count
    assert any(item.is_revert for item in changed.commits)
    assert commit_reads == ["base-a", "base-b"]


def test_stale_concurrent_operator_resume_cannot_acknowledge_new_stop(tmp_path) -> None:
    store = PRGuardStore(tmp_path / "guard.sqlite3")
    first = history(2, revert=2)
    assert store.observe("owner/repo", 42, first).stopped
    stale_generation = store.stop_generation("owner/repo", 42)
    store.resume("owner/repo", 42, first, expected_head=first.head_sha, reason="First external intervention", expected_stop_generation=stale_generation)
    second = history(3, revert=3)
    assert store.observe("owner/repo", 42, second).stopped
    with pytest.raises(ValueError, match="changed during acknowledgement"):
        store.resume("owner/repo", 42, first, expected_head=first.head_sha, reason="Delayed acknowledgement of first stop", expected_stop_generation=stale_generation)
    assert store.status("owner/repo", 42).revert_sha == second.head_sha
    assert [entry["event"] for entry in store.events("owner/repo", 42)] == ["stopped", "operator_resumed", "stopped"]


def test_pr_safety_cli_requires_current_head_and_preserves_audit(monkeypatch) -> None:
    import json

    from click.testing import CliRunner

    from auto_coder import cli_commands_pr_safety as commands

    store = PRGuardStore()
    stopped = history(30, revert=25)
    assert store.observe("owner/repo", 42, stopped).stopped
    client = reader(stopped)
    monkeypatch.setattr(commands, "get_github_token_or_fail", lambda _provided: "operator-token")
    monkeypatch.setattr(commands, "GitHubClient", lambda **_kwargs: client)
    runner = CliRunner()
    result = runner.invoke(commands.pr_safety_group, ["status", "--repo", "OWNER/REPO", "--pr", "42"])
    assert result.exit_code == 0
    assert json.loads(result.output)["decision"]["disposition"] == "stopped"
    client.get_pr_automation_history_strict.assert_not_called()
    arguments = ["resume", "--repo", "OWNER/REPO", "--pr", "42", "--reason", "Corrected and verified the contract", "--expected-head"]
    refused = runner.invoke(commands.pr_safety_group, arguments + ["stale-head"])
    assert refused.exit_code != 0 and store.status("owner/repo", 42).stopped
    resumed = runner.invoke(commands.pr_safety_group, arguments + [stopped.head_sha])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output)["decision"]["disposition"] == "allowed"
    assert len(store.events("owner/repo", 42)) == 2
    assert store.events("owner/repo", 42)[-1]["evidence"]["reason"] == "Corrected and verified the contract"


@pytest.mark.parametrize("credential_source,status", [("explicit", 200), ("environment", 200), ("discovery", 200), ("explicit", 403)])
def test_pr_safety_uses_operator_credentials_without_permission_fallback(monkeypatch, credential_source, status) -> None:
    from pathlib import Path

    import httpx
    from click.testing import CliRunner

    from auto_coder import cli_commands_pr_safety as commands
    from auto_coder import cli_commands_utils, github_controller_auth

    store = PRGuardStore()
    current = history(2, revert=2)
    stopped = store.observe("owner/repo", 42, current)
    prior_events = store.events("owner/repo", 42)
    config = Path.home() / ".auto-coder" / "config.toml"
    config.write_text('[github-app-auto-coder-controller]\napp_id = 12345\nprivate_key_path = "/unused/controller.pem"\n')
    app_credentials = Mock(side_effect=AssertionError("Operator resume selected controller App credentials"))
    monkeypatch.setattr(github_controller_auth.GitHubAppControllerAuth, "credential_for", app_credentials)
    discovery = Mock(return_value="discovered-operator-token")
    monkeypatch.setattr(cli_commands_utils, "get_github_token", discovery)
    if credential_source == "discovery":
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GITHUB_TOKEN", "environment-operator-token")
    expected_token = {"explicit": "explicit-operator-token", "environment": "environment-operator-token", "discovery": "discovered-operator-token"}[credential_source]
    existing = GitHubClient(token="existing-singleton-token")
    monkeypatch.setattr(GitHubClient, "_instance", existing)
    requests = []

    def request(method, url, **kwargs):
        assert method == "GET"
        assert kwargs["headers"]["Authorization"] == f"Bearer {expected_token}"
        requests.append(url)
        if status == 403:
            return httpx.Response(403, json={"message": "Forbidden"}, request=httpx.Request(method, url))
        payload = [{"sha": commit.sha, "commit": {"message": commit.message}} for commit in current.commits] if url.endswith("/commits") else {"number": 42, "head": {"sha": current.head_sha}, "base": {"sha": "b" * 40}, "commits": 2}
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))

    monkeypatch.setattr("auto_coder.util.gh_cache._strict_request", request)
    arguments = ["resume", "--repo", "owner/repo", "--pr", "42", "--expected-head", current.head_sha, "--reason", "Operator investigated the correction"]
    if credential_source == "explicit":
        arguments += ["--github-token", expected_token]
    result = CliRunner().invoke(commands.pr_safety_group, arguments)
    if status == 403:
        assert result.exit_code != 0
        assert "PR automation was not resumed" in result.output
        assert store.status("owner/repo", 42) == stopped
        assert store.events("owner/repo", 42) == prior_events
        assert len(requests) == 1
    else:
        assert result.exit_code == 0, result.output
        assert store.status("owner/repo", 42).allowed
        assert [event["event"] for event in store.events("owner/repo", 42)] == ["stopped", "operator_resumed"]
        assert len(requests) == 3
    assert expected_token not in result.output
    assert GitHubClient._instance is existing
    assert discovery.call_count == int(credential_source == "discovery")
    app_credentials.assert_not_called()


def test_stopped_open_pr_preserves_linked_issue_exclusion_and_unrelated_work(monkeypatch, mock_github_client) -> None:
    from contextlib import nullcontext

    from auto_coder.automation_config import EmptyPRResult, StaleJulesPRResult
    from tests.ci_observation_fakes import authoritative_ci_status

    client = mock_github_client
    client.get_pr_automation_history_strict.side_effect = lambda _repo, number: history(2, revert=2) if number == 42 else history(1)
    client.get_open_prs_json.return_value = [
        {"number": number, "title": "Existing PR", "body": "<!-- auto-coder:local-llm -->" if number == 43 else "", "created_at": "2023-01-01T00:00:00Z", "labels": [], "user": {"login": "developer"}, "head": {"ref": "feature", "sha": history(1).head_sha}, "mergeable": True} for number in (42, 43)
    ]
    client.get_open_issues_json.return_value = [
        {"number": 10, "title": "Covered Issue", "body": "", "labels": [], "linked_pr_numbers": [42]},
        {"number": 11, "title": "Unrelated Issue", "body": "", "labels": [], "linked_pr_numbers": []},
    ]
    cleanup_calls = []

    def empty(_github, _repository, pr_data, _config):
        cleanup_calls.append(pr_data["number"])
        return EmptyPRResult()

    monkeypatch.setattr(pr_processor, "_reject_unsafe_codex_cloud_pr", lambda *_args, **_kwargs: pr_processor.UnsafeCodexCloudPRResult())
    monkeypatch.setattr(pr_processor, "_close_empty_pr", empty)
    monkeypatch.setattr(pr_processor, "_close_stale_jules_pr", lambda *_args: StaleJulesPRResult())
    monkeypatch.setattr(pr_processor, "_should_skip_waiting_for_jules", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("auto_coder.automation_engine._should_skip_waiting_for_jules", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("auto_coder.automation_engine.LabelManager", lambda *_args, **_kwargs: nullcontext(True))
    monkeypatch.setattr("auto_coder.util.github_action._check_github_actions_status", authoritative_ci_status(success=True, ids=[1]))
    monkeypatch.setattr("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress", lambda *_args, **_kwargs: True)
    config = AutomationConfig(pr_adversarial_validation=False)
    engine = AutomationEngine(client, config)
    candidates = engine._get_candidates("owner/repo")
    assert {(candidate.type, candidate.data["number"]) for candidate in candidates} == {("pr", 43), ("issue", 11)}
    assert cleanup_calls == [43]
    assert PRGuardStore().status("owner/repo", 42).stopped
    assert PRGuardStore().status("owner/repo", 43).allowed


def test_stop_after_approval_lookup_is_unsent_and_reports_blocked(tmp_path, monkeypatch) -> None:
    from auto_coder import merge_operation_adapter, merge_operation_state
    from auto_coder.merge_operation_state import EffectName, EffectState, MergeOperationIdentity, MergeOperationStore

    client = reader(history(1))
    client.token = "test-token"
    api = Mock()
    api.pulls.get.return_value = {"number": 42, "head": {"sha": history(1).head_sha}, "user": {"login": "dependabot[bot]"}}

    def list_reviews(*_args, **_kwargs):
        client.get_pr_automation_history_strict.return_value = history(2, revert=2)
        return []

    api.pulls.list_reviews.side_effect = list_reviews
    store = MergeOperationStore(tmp_path / "merge.sqlite3")
    monkeypatch.setattr(merge_operation_state, "get_merge_operation_store", lambda: store)
    monkeypatch.setattr(merge_operation_adapter, "get_ghapi_client", lambda _token: api)
    monkeypatch.setattr("auto_coder.util.gh_cache.get_ghapi_client", lambda _token: api)
    monkeypatch.setattr(pr_processor, "_is_pr_review_thread_gate_enabled", lambda *_args: False)
    monkeypatch.setattr(pr_processor, "get_speculative_jules_lifecycle", lambda _client, **_kwargs: None)
    monkeypatch.setattr(pr_processor, "_is_dependabot_pr", lambda *_args: True)
    disposition = pr_processor.MergeRouteDisposition()
    assert not pr_processor._merge_pr("owner/repo", 42, {}, AutomationConfig(), github_client=client, route_disposition=disposition)
    assert disposition.outcome is PRProcessingOutcome.BLOCKED and "Revert" in disposition.reason
    api.pulls.create_review.assert_not_called()
    api.pulls.merge.assert_not_called()
    operation = store.get(MergeOperationIdentity("https://api.github.com", "owner/repo", 42))
    assert operation.effect(EffectName.APPROVAL).state is EffectState.CONFIRMED_UNSENT
    assert operation.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED


def test_stopped_pr_settles_prior_merge_receipt_without_new_effect(tmp_path, monkeypatch) -> None:
    from auto_coder import merge_operation_adapter, merge_operation_state
    from auto_coder.merge_operation_state import EffectName, EffectState, MergeOperationIdentity, MergeOperationStore

    current = history(30)
    client = reader(current)
    client.token = "test-token"
    store = MergeOperationStore(tmp_path / "merge.sqlite3")
    identity = MergeOperationIdentity("https://api.github.com", "owner/repo", 42)
    store.get_or_create(identity, expected_head_sha=current.head_sha, merge_method="squash", approval_credential_role="role", reviewer_identity="", needs_approval=False)
    reservation = store.reserve_attempt(identity, EffectName.MERGE)
    assert reservation.granted
    store.record_delivery_unknown(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, detail="Lost response after send")
    api = Mock()
    api.pulls.get.return_value = {"merged": True, "head": {"sha": current.head_sha}, "merge_commit_sha": "merged-commit"}
    monkeypatch.setattr(merge_operation_state, "get_merge_operation_store", lambda: store)
    monkeypatch.setattr(merge_operation_adapter, "get_ghapi_client", lambda _token: api)
    disposition = pr_processor.MergeRouteDisposition()
    assert not pr_processor._merge_pr("owner/repo", 42, {}, AutomationConfig(), github_client=client, route_disposition=disposition)
    assert disposition.outcome is PRProcessingOutcome.BLOCKED
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE
    api.pulls.create_review.assert_not_called()
    api.pulls.merge.assert_not_called()
    assert PRGuardStore().status("owner/repo", 42).stopped


@pytest.mark.parametrize("base", [None, "", 123])
def test_strict_reader_rejects_missing_base_even_with_cached_history(monkeypatch, base) -> None:
    client, reads = _strict_client(monkeypatch, 1)
    assert client.get_pr_automation_history_strict("owner/repo", 42).complete
    count = len(reads)
    monkeypatch.setattr(client, "get_pull_request_metadata_strict", lambda *_: {"number": 42, "head": {"sha": history(1).head_sha}, "commits": 1, "base": {"sha": base}})
    with pytest.raises(ValueError, match="base identity"):
        client.get_pr_automation_history_strict("owner/repo", 42)
    assert len(reads) == count
