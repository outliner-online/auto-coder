from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.pr_repair_contract

from auto_coder.ci_observation import (
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)
from auto_coder.ci_repair_authority import current_ci_failure_authority, evaluate_ci_repair_snapshot


def snapshot(*facts, availability=ObservationAvailability.KNOWN):
    return CIObservationSnapshot(
        ObservationSubject("https://api.github.com", "owner/repo", 7, "head"),
        ObservationRequest("github-actions", "complete"),
        "cycle",
        0,
        availability,
        tuple(facts),
        unavailable_reason="unavailable" if availability is ObservationAvailability.UNAVAILABLE else None,
    )


def workflow(run, attempt, conclusion):
    return WorkflowObservation(WorkflowExecutionIdentity("workflow", run, attempt), conclusion)


def test_snapshot_requires_complete_settled_current_failure():
    assert evaluate_ci_repair_snapshot(snapshot(workflow("2", 2, CIConclusion.FAILURE))) == (
        True,
        "current exact-head CI failure",
    )
    for conclusion in (CIConclusion.PENDING, CIConclusion.UNKNOWN, CIConclusion.ACTION_REQUIRED):
        allowed, reason = evaluate_ci_repair_snapshot(snapshot(workflow("2", 2, CIConclusion.FAILURE), workflow("3", 3, conclusion)))
        assert allowed is False
        assert "pending, unknown, or action-required" in reason
    assert evaluate_ci_repair_snapshot(snapshot(availability=ObservationAvailability.KNOWN_EMPTY))[0] is False
    assert evaluate_ci_repair_snapshot(snapshot(availability=ObservationAvailability.UNAVAILABLE))[0] is False


def test_explicit_newer_attempt_supersedes_old_failure():
    allowed, reason = evaluate_ci_repair_snapshot(
        snapshot(
            workflow("same-run", 1, CIConclusion.FAILURE),
            workflow("same-run", 2, CIConclusion.SUCCESS),
        )
    )
    assert allowed is False
    assert reason == "CI has no current terminal failure"

    allowed, reason = evaluate_ci_repair_snapshot(
        snapshot(
            workflow("rerun", 2, CIConclusion.SUCCESS),
            WorkflowObservation(
                WorkflowExecutionIdentity("independent-workflow", "other-run", 1),
                CIConclusion.FAILURE,
            ),
        )
    )
    assert allowed is True
    assert reason == "current exact-head CI failure"


def test_authority_binds_open_exact_head_and_observation():
    client = MagicMock(token="token")
    client.get_pull_request_metadata_strict.return_value = {
        "number": 7,
        "state": "open",
        "merged_at": None,
        "head": {"sha": "head"},
    }
    observed = snapshot(workflow("2", 2, CIConclusion.FAILURE))
    client.api_for.return_value = MagicMock()
    client.authority_key_for.return_value = "controller-authority"
    with (
        patch("auto_coder.ci_repair_authority.observe_ci", return_value=observed),
        patch(
            "auto_coder.ci_repair_authority.ci_observation_merge_authority",
            return_value=nullcontext(True),
        ),
    ):
        with current_ci_failure_authority(client, "owner/repo", 7, "head") as authority:
            assert authority.allowed is True
            assert authority.failure_identities == ("workflow:workflow:run:2:attempt:2",)


def test_changed_or_closed_target_refuses_before_ci_read():
    for metadata, reason in (
        ({"state": "closed", "merged_at": None, "head": {"sha": "head"}}, "no longer open"),
        ({"state": "open", "merged_at": None, "head": {"sha": "new"}}, "head changed"),
    ):
        client = MagicMock(token="token")
        client.get_pull_request_metadata_strict.return_value = metadata
        with patch("auto_coder.ci_repair_authority.observe_ci") as observe:
            with current_ci_failure_authority(client, "owner/repo", 7, "head") as authority:
                assert authority.allowed is False
                assert reason in authority.reason
            observe.assert_not_called()


@pytest.mark.parametrize("conclusion", [CIConclusion.CANCELLED, CIConclusion.SUCCESS, CIConclusion.SKIPPED])
def test_non_failure_terminal_results_do_not_admit_code_repair(conclusion):
    observed = snapshot(workflow("2", 2, conclusion))
    assert evaluate_ci_repair_snapshot(observed) == (False, "CI has no current terminal failure")


def test_cancelled_root_does_not_hide_an_independent_test_failure():
    observed = snapshot(
        workflow("2", 2, CIConclusion.CANCELLED),
        WorkflowObservation(WorkflowExecutionIdentity("independent", "3", 1), CIConclusion.FAILURE),
    )
    assert evaluate_ci_repair_snapshot(observed) == (True, "current exact-head CI failure")
    from auto_coder.ci_repair_authority import CIRepairAuthority

    assert CIRepairAuthority(True, "failure", "head", observed).failure_identities == ("workflow:independent:run:3:attempt:1",)


def test_interruption_annotation_preserves_native_red_merge_evidence():
    from auto_coder.ci_repair_authority import CIRepairAuthority

    aggregate = WorkflowObservation(WorkflowExecutionIdentity("workflow", "2", 2), CIConclusion.FAILURE, repair_trigger=False)
    observed = snapshot(aggregate, workflow("3", 1, CIConclusion.CANCELLED))
    assert evaluate_ci_repair_snapshot(observed) == (False, "CI has no current terminal failure")
    assert aggregate.conclusion is CIConclusion.FAILURE
    assert observed.facts[1].conclusion is CIConclusion.CANCELLED
    assert CIRepairAuthority(False, "interrupted", "head", observed).failure_identities == ()
