"""Clock, reservation and restart oracles for the shared PR correction journal."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from auto_coder.exceptions import DeliveryCertainty
from auto_coder.pr_no_commit_recovery import NoCommitRecovery
from auto_coder.pr_processor import _record_review_feedback_state
from auto_coder.pr_repair import ExistingPrRepairTarget

TARGET = ExistingPrRepairTarget("owner/repo", 88, "implementation", "main", "1" * 40)


@pytest.fixture
def series(tmp_path):
    store = NoCommitRecovery(tmp_path / "cloud_review_repairs.json")
    store.record_acceptance("claude-routine", "session-88", TARGET, ("review-1",), "fix edge case", 1000.0)
    return store


def snapshot(series):
    return list(json.loads(series.path.read_text())["no_commit_series"].values())


def test_sixty_minutes_and_three_acceptance_relative_intervals_across_restart(series):
    clock = [1000.0]
    sends = []

    def send(logical, target):
        saved = snapshot(series)[0]
        assert saved["status"] == "reserved"
        assert logical.endswith(f":{saved['ordinal']}")
        assert target == TARGET
        sends.append(logical)
        clock[0] += 17  # HTTP acceptance takes time; interval starts afterward.
        return True

    for ordinal in range(1, 4):
        store = NoCommitRecovery(series.path)
        clock[0] = snapshot(series)[0]["accepted_at"] + 59 * 60
        result = store.checkpoint("claude-routine", "session-88", TARGET, lambda: clock[0], lambda: pytest.fail("too early"), send)
        assert result.reason == "not_due"
        clock[0] += 60
        result = store.checkpoint("claude-routine", "session-88", TARGET, lambda: clock[0], lambda: "eligible", send)
        assert (result.reason, result.ordinal) == ("accepted", ordinal)
        assert snapshot(series)[0]["accepted_at"] == clock[0]
        assert store.checkpoint("claude-routine", "session-88", TARGET, lambda: clock[0], lambda: "eligible", send).reason in {"not_due", "limit_reached"}
    clock[0] += 100000
    assert store.checkpoint("claude-routine", "session-88", TARGET, lambda: clock[0], lambda: pytest.fail("exhausted"), send).reason == "limit_reached"
    assert len(sends) == len(set(sends)) == 3


def test_concurrent_reobservations_reserve_only_one_ordinal(series):
    sends = []

    def observe(_):
        return NoCommitRecovery(series.path).checkpoint("claude-routine", "session-88", TARGET, lambda: 4600.0, lambda: "eligible", lambda logical, _target: sends.append(logical) or True).reason

    with ThreadPoolExecutor(max_workers=4) as workers:
        outcomes = list(workers.map(observe, range(8)))
    assert outcomes.count("accepted") == 1
    assert outcomes.count("not_due") == 7
    assert len(sends) == snapshot(series)[0]["ordinal"] == 1


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_unknown_send_never_mints_another_logical_request(series, failure):
    def send(_logical, _target):
        if failure == "exception":
            raise RuntimeError("response lost")
        return False

    if failure == "exception":
        with pytest.raises(RuntimeError, match="response lost"):
            series.checkpoint("claude-routine", "session-88", TARGET, lambda: 4600.0, lambda: "eligible", send)
    else:
        assert series.checkpoint("claude-routine", "session-88", TARGET, lambda: 4600.0, lambda: "eligible", send).reason == "delivery_unknown"
    resumed = NoCommitRecovery(series.path)
    assert resumed.checkpoint("claude-routine", "session-88", TARGET, lambda: 50000.0, lambda: pytest.fail("unknown"), lambda *_: pytest.fail("resend")).reason == "delivery_unknown"
    assert snapshot(series)[0]["ordinal"] == 1


def test_definite_pre_send_quota_refusal_reuses_same_ordinal(series):
    class NotSent(RuntimeError):
        delivery_certainty = DeliveryCertainty.NOT_SENT

    identities = []

    def refuse(logical, target):
        identities.append(logical)
        raise NotSent("quota")

    with pytest.raises(NotSent):
        series.checkpoint("claude-routine", "session-88", TARGET, lambda: 4600.0, lambda: "eligible", refuse)
    result = series.checkpoint("claude-routine", "session-88", TARGET, lambda: 4700.0, lambda: "eligible", lambda logical, _target: identities.append(logical) or True)
    assert (result.reason, result.ordinal) == ("accepted", 1)
    assert identities[0] == identities[1]


@pytest.mark.parametrize("reason", ["closed", "owner_changed", "no_change", "input_blocked"])
def test_terminal_outcomes_stop_the_original_series(series, reason):
    assert series.checkpoint("claude-routine", "session-88", TARGET, lambda: 4600.0, lambda: reason, lambda *_: pytest.fail("terminal")).reason == reason
    assert snapshot(series)[0]["outcome"] == reason
    assert NoCommitRecovery(series.path).checkpoint("claude-routine", "session-88", TARGET, lambda: 50000.0, lambda: "eligible", lambda *_: pytest.fail("ended")).reason == "no_accepted_request"


@pytest.mark.parametrize("reason", ["allowance_exhausted", "pending_delivery", "target_unavailable", "ownership_unavailable", "ci_wait", "review_wait"])
def test_context_holds_do_not_spend_an_ordinal(series, reason):
    assert series.checkpoint("claude-routine", "session-88", TARGET, lambda: 4600.0, lambda: reason, lambda *_: pytest.fail("hold")).reason == reason
    assert snapshot(series)[0]["ordinal"] == 0


def test_changed_commit_or_branch_invalidates_without_claiming_implementation(series):
    result = series.checkpoint("claude-routine", "session-88", replace(TARGET, head_sha="2" * 40), lambda: 4600.0, lambda: pytest.fail("changed"), lambda *_: pytest.fail("send"))
    assert result.reason == snapshot(series)[0]["outcome"] == "baseline_changed"
    assert snapshot(series)[0]["ordinal"] == 0


def test_acceptance_reobservation_never_resets_clock_or_exhausted_history(series):
    series.record_acceptance("claude-routine", "session-88", TARGET, ("review-1",), "fix edge case", 9000.0)
    assert snapshot(series)[0]["accepted_at"] == 1000.0
    series.supersede_prior_request("claude-routine", "session-88", TARGET, ("review-2",), "new correction")
    assert snapshot(series)[0]["outcome"] == "superseded_request"
    series.record_acceptance("claude-routine", "session-88", TARGET, ("review-2",), "new correction", 9000.0)
    series.record_acceptance("claude-routine", "session-88", TARGET, ("review-1",), "fix edge case", 10000.0)
    active = [record for record in snapshot(series) if record["status"] == "accepted"]
    assert len(active) == 1
    assert active[0]["accepted_at"] == 9000.0


def test_old_delivery_writes_preserve_recovery_and_recovery_preserves_receipts(series):
    _record_review_feedback_state(series.path, {"review-1"}, set(), {"v": "generation"}, {"snapshot": {"finding": "turn"}})
    assert len(snapshot(series)) == 1
    series.checkpoint("claude-routine", "session-88", TARGET, lambda: 4600.0, lambda: "eligible", lambda *_: True)
    document = json.loads(series.path.read_text())
    assert document["delivered_feedback"] == ["review-1"]
    assert document["validation_generations"] == {"v": "generation"}
    assert document["validation_snapshots"] == {"snapshot": {"finding": "turn"}}


def test_unsupported_provider_or_missing_baseline_has_no_recovery_authority(tmp_path):
    store = NoCommitRecovery(tmp_path / "cloud_review_repairs.json")
    store.record_acceptance("unsupported", "task", TARGET, (), "do work", 0.0)
    assert store.checkpoint("unsupported", "task", TARGET, lambda: 9999.0, lambda: pytest.fail("unsupported"), lambda *_: pytest.fail("unsupported")).reason == "unsupported_provider"
    assert store.checkpoint("codex-cloud", "task", TARGET, lambda: 9999.0, lambda: pytest.fail("legacy"), lambda *_: pytest.fail("legacy")).reason == "no_accepted_request"


def test_existing_quota_hold_is_definitely_before_send(series):
    from auto_coder.claude_followup_waits import ClaudeFollowupHoldActive

    def held(_logical, _target):
        raise ClaudeFollowupHoldActive(8000.0)

    with pytest.raises(ClaudeFollowupHoldActive):
        series.checkpoint("claude-routine", "session-88", TARGET, lambda: 4600.0, lambda: "eligible", held)
    assert (snapshot(series)[0]["status"], snapshot(series)[0]["ordinal"]) == ("accepted", 0)


def test_other_pr_can_finish_while_transport_is_in_flight_without_lost_receipts(series):
    from threading import Barrier

    other = replace(TARGET, pr_number=89, head_branch="other")
    series.record_acceptance("claude-routine", "session-89", other, ("review-89",), "other fix", 1000.0)
    entered = Barrier(2)

    def run(target):
        def send(_logical, _target):
            entered.wait(timeout=2)
            return True

        return NoCommitRecovery(series.path).checkpoint("claude-routine", f"session-{target.pr_number}", target, lambda: 4600.0, lambda: "eligible", send)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(run, (TARGET, other)))
    assert [result.reason for result in results] == ["accepted", "accepted"]
    assert {(record["pr_number"], record["ordinal"], record["status"]) for record in snapshot(series)} == {(88, 1, "accepted"), (89, 1, "accepted")}


def test_a_different_pr_repair_sender_invalidates_without_registering_a_budget(series):
    from unittest.mock import Mock, patch

    from auto_coder.pr_processor import _send_followup_with_quota_admission

    native = Mock()
    native.send_followup.return_value = True
    with patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=series.path):
        assert _send_followup_with_quota_admission(native, "owner/repo", "session-88", "new conflict correction", repair_target=TARGET)
    native.send_followup.assert_called_once_with("session-88", "new conflict correction")
    assert len(snapshot(series)) == 1
    assert snapshot(series)[0]["outcome"] == "superseded_request"


def test_missing_observed_baseline_does_not_poison_delivery_state(series):
    with pytest.raises(ValueError, match="observed PR baseline"):
        series.record_acceptance("claude-routine", "session-89", replace(TARGET, head_sha=""), ("new",), "fix", 1000.0)
    assert snapshot(series)[0]["status"] == "accepted"


def test_known_terminal_pr_callback_prevents_reopen_from_reviving_old_series(series):
    from unittest.mock import MagicMock, patch

    from auto_coder.automation_config import AutomationConfig
    from auto_coder.automation_engine import AutomationEngine

    engine = AutomationEngine(MagicMock(), AutomationConfig())
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=series.path),
        patch("auto_coder.automation_engine.schedule_reevaluation_for_pr_owner") as reclamation,
    ):
        engine._schedule_pr_owner_reclamation("owner/repo", {"number": 88, "state": "closed"})
    reclamation.assert_called_once()
    assert (snapshot(series)[0]["status"], snapshot(series)[0]["outcome"]) == ("ended", "closed")
    # Reopen with the same task/head does not erase the known terminal outcome.
    assert series.checkpoint("claude-routine", "session-88", TARGET, lambda: 50000.0, lambda: "eligible", lambda *_: pytest.fail("revived")).reason == "no_accepted_request"
