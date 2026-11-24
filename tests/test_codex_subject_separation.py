"""Different verified PR subjects do not quarantine their shared Issue owner."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.cloud_task_client_base import CloudTaskState
from auto_coder.codex_cloud_client import CodexCloudClient, _codex_followup_state_path
from auto_coder.codex_observation import CodexExecutionEvidence, CodexRunObservation, ObservationBinding, PullRequestEvidence, PullRequestPresence
from auto_coder.codex_pr_recovery import CodexPRRecoveryMonitor, CodexPRRecoveryStore
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult
from auto_coder.codex_work_accounting import CodexWorkAccounting
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotUnavailable
from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
from tests.test_legacy_review_source_evidence import ordinary  # noqa: F401
from tests.test_scoped_review_followup import HEAD, PR, REPO, TASK

SECOND = PR + 1
SECOND_TASK = "task_e_second"


def second_run(*, published=True, branch="second-branch", repository=REPO):
    return CloudRun(
        repository, 9, 2, "codex-cloud", task_id=SECOND_TASK, backend_name="codex-cloud", environment_id="environment", base_branch="main", launch_identity="launch-second", publication_head_repository=repository, publication_head_ref=branch, pull_request_numbers=[SECOND] if published else []
    )


def operations(incarnation):
    return CodexWorkAccounting(ImplementationSlotRepository(REPO, 1)).snapshot(ImplementationOwner("issue", 9), incarnation).operations


@pytest.mark.parametrize(
    "control",
    [
        "different_pr",
        "same_pr_new_head",
        "same_pr_reopened",
        "missing_old_association",
        "missing_new_association",
        "legacy_old",
        "missing_target",
        "cross_repo_target",
        "missing_slot_membership",
        "same_head_different_base",
        "missing_old_head_identity",
        "missing_new_head_identity",
        "url_only_head_identity",
    ],
)
@pytest.mark.parametrize("force", [False, True])
def test_actual_ordinary_unknown_then_native_ci_continuation_uses_verified_subject(ordinary, control, force):
    run, old, _new, wham, _github, incarnation = ordinary
    run((old,))
    before = operations(incarnation)
    old_operation = next(item for item in before if item.kind == "follow-up")
    native = json.loads(_codex_followup_state_path(REPO).read_text())
    slots = ImplementationSlotRepository(REPO, 1)
    other = second_run()
    if control != "missing_slot_membership":
        assert slots.record_implementation_pr(ImplementationOwner("issue", 9), SECOND)
    if control != "same_head_different_base":
        CloudRunRepository(REPO).save(other)
    target = ExistingPrRepairTarget(REPO, SECOND, "second-branch", "main", "b" * 40)
    if control in {"same_pr_new_head", "same_pr_reopened"}:
        # Reopening cannot create a different PR subject; durable association
        # retains the same numeric PR while SHA/base activity changes.
        target = ExistingPrRepairTarget(REPO, PR, "work-branch", "main", "c" * 40)
        other.pull_request_numbers = [PR]
        CloudRunRepository(REPO).save(other)
    elif control == "missing_old_association":
        original = CloudRunRepository(REPO).get(9, 1)
        original.pull_request_numbers = []
        # Explicit deficient legacy fixture; save normally preserves associations.
        path = CloudRunRepository(REPO).storage_path
        document = json.loads(path.read_text())
        document["9:1"] = original.to_dict()
        path.write_text(json.dumps(document))
    elif control == "missing_new_association":
        other.pull_request_numbers = []
        # This is a new run, so a source without PR association stays unverified.
        repository = CloudRunRepository(REPO)
        path = repository.storage_path
        document = json.loads(path.read_text())
        document["9:2"] = other.to_dict()
        path.write_text(json.dumps(document))
    elif control == "legacy_old":
        with slots._state_lock():
            records = slots._read()
            records[ImplementationOwner("issue", 9).key][CodexWorkAccounting._FIELD]["operations"][old_operation.logical_operation_id].pop("send_provenance")
            slots._write(records)
        old_operation = next(item for item in operations(incarnation) if item.logical_operation_id == old_operation.logical_operation_id)
    elif control == "same_head_different_base":
        other = CloudRunRepository(REPO).get(9, 1)
        other.add_pull_request(SECOND)
        CloudRunRepository(REPO).save(other)
        target = ExistingPrRepairTarget(REPO, SECOND, "work-branch", "release", "b" * 40)
    elif control in {"missing_old_head_identity", "missing_new_head_identity"}:
        path = CloudRunRepository(REPO).storage_path
        document = json.loads(path.read_text())
        document["9:1" if control == "missing_old_head_identity" else "9:2"]["publication_head_repository"] = ""
        path.write_text(json.dumps(document))
    elif control == "cross_repo_target":
        target.repo_name = "other/repo"
    if control in {"different_pr", "same_head_different_base", "url_only_head_identity"}:
        from auto_coder.codex_pr_attribution import CodexPrAttributionRepository, resolve_codex_pr_origin

        metadata = {"number": SECOND, "body": "Fixes #9" + ("\nhttps://chatgpt.com/codex/tasks/" + SECOND_TASK if control == "url_only_head_identity" else ""), "head": {"ref": target.head_branch, "repo": {"full_name": REPO}}}
        _github.get_pull_request_metadata_strict.return_value = metadata
        resolved = resolve_codex_pr_origin(REPO, _github.get_pull_request_metadata_strict(REPO, SECOND), CloudRunRepository(REPO), CodexPrAttributionRepository(REPO))
        assert resolved.origin is not None
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)
    wham.resolve_latest_assistant_turn.side_effect = lambda task_id: task_id + "~assttrn_1"
    client = CodexCloudClient(repo_name=REPO)
    prompt = build_existing_pr_repair_prompt(target, "Existing authorized CI continuation")
    accepted = client.continue_if_paused(TASK if control == "same_head_different_base" else SECOND_TASK, prompt=prompt, repair_target=None if control == "missing_target" else target, force=force)
    assert accepted is (control == "different_pr")
    assert wham.send_follow_up.call_count == (2 if control == "different_pr" else 1)
    retained = operations(incarnation)
    assert next(item for item in retained if item.logical_operation_id == old_operation.logical_operation_id) == old_operation
    current = json.loads(_codex_followup_state_path(REPO).read_text())
    assert all(current[key] == value for key, value in native.items())


@pytest.mark.parametrize("control", ["independent", "same_branch", "missing_launch", "missing_head", "closed", "reopened_same_subject", "already_published", "noncanonical_ref", "publication_unknown", "case_only_head_repository"])
def test_actual_initial_publication_monitor_requires_positive_branch_and_fresh_observation(ordinary, control):
    run, old, _new, wham, _github, incarnation = ordinary
    run((old,))
    old_operation = next(item for item in operations(incarnation) if item.kind == "follow-up")
    other = second_run(published=False, branch="work-branch" if control in {"same_branch", "reopened_same_subject"} else "second-branch")
    if control == "case_only_head_repository":
        other.publication_head_repository = REPO.upper()
        other.publication_head_ref = "work-branch"
    if control == "noncanonical_ref":
        other.publication_head_ref = "refs/heads/work-branch"
    if control == "missing_launch":
        other.launch_identity = ""
    if control == "missing_head":
        other.publication_head_ref = ""
    runs = CloudRunRepository(REPO)
    runs.save(other)
    publication_allowed = control in {"independent", "publication_unknown"}
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE if control == "publication_unknown" else FollowUpDeliveryOutcome.DELIVERED, 502 if control == "publication_unknown" else 200)
    clock = [1000.0]
    observations = MagicMock()
    reopened = [control != "reopened_same_subject"]

    def observe(observed):
        assert observed.task_id == SECOND_TASK
        return CodexRunObservation(
            ObservationBinding.from_run(observed),
            1,
            1,
            CodexExecutionEvidence(CloudTaskState.COMPLETED, SECOND_TASK + "~asst_1", SECOND_TASK + "~user_1", True, "completed"),
            PullRequestEvidence(PullRequestPresence.PR_PRESENT, PR) if control == "already_published" else PullRequestEvidence(PullRequestPresence.NO_MATCHING_PR),
            "closed" if control == "closed" or not reopened[0] else "open",
        )

    observations.observe.side_effect = observe

    async def enqueue(_number):
        return True

    store = CodexPRRecoveryStore()
    monitor = CodexPRRecoveryMonitor(runs, observations, wham, store, enqueue, now=lambda: clock[0], grace_period=120)
    asyncio.run(monitor.tick(asyncio.Event()))
    reopened[0] = True
    clock[0] += 120
    monitor._next_due.clear()
    asyncio.run(monitor.tick(asyncio.Event()))
    if control == "reopened_same_subject":
        clock[0] += 120
        monitor._next_due.clear()
        asyncio.run(monitor.tick(asyncio.Event()))
    assert wham.send_follow_up.call_count == (2 if publication_allowed else 1)
    assert next(item for item in operations(incarnation) if item.logical_operation_id == old_operation.logical_operation_id) == old_operation
    if publication_allowed:
        assert wham.send_follow_up.call_args.args == (SECOND_TASK, SECOND_TASK + "~asst_1", "Create PR", False)
        publication = next(item for item in operations(incarnation) if item.kind == "publication")
        assert publication.send_provenance.publication_head_ref == "second-branch"
        assert publication.send_provenance.outcome == ("unknown" if control == "publication_unknown" else "accepted")
        restarted = CodexPRRecoveryMonitor(runs, observations, wham, store, enqueue, now=lambda: clock[0], grace_period=120)
        asyncio.run(restarted.tick(asyncio.Event()))
        assert wham.send_follow_up.call_count == 2

        # Later authoritative PR association dominates prospective branch scope.
        # This also exercises production reconstruction of publication wire proof.
        assert ImplementationSlotRepository(REPO, 1).record_implementation_pr(ImplementationOwner("issue", 9), SECOND)
        other.add_pull_request(SECOND)
        runs.save(other)
        from auto_coder.codex_pr_attribution import CodexPrAttributionRepository, resolve_codex_pr_origin

        published = {"number": SECOND, "body": "Fixes #9", "head": {"ref": "second-branch", "repo": {"full_name": REPO}}}
        _github.get_pull_request_metadata_strict.return_value = published
        assert resolve_codex_pr_origin(REPO, _github.get_pull_request_metadata_strict(REPO, SECOND), runs, CodexPrAttributionRepository(REPO)).origin is not None
        target = ExistingPrRepairTarget(REPO, SECOND, "second-branch", "main", "d" * 40)
        wham.resolve_latest_assistant_turn.side_effect = lambda task: task + "~assttrn_1"
        wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)
        delivered = CodexCloudClient(repo_name=REPO).continue_if_paused(SECOND_TASK, prompt=build_existing_pr_repair_prompt(target, "Authorized current PR continuation"), repair_target=target, force=True)
        assert delivered is (control == "independent")
        assert wham.send_follow_up.call_count == (3 if control == "independent" else 2)
        assert next(item for item in operations(incarnation) if item.kind == "publication").send_provenance == publication.send_provenance


@pytest.mark.parametrize("force", [False, True])
def test_old_incarnation_cannot_be_reinterpreted_as_verified_current_subject(ordinary, force):
    from tests.test_codex_pr_recovery import _commit_retirement

    run, old, _new, wham, _github, incarnation = ordinary
    run((old,))
    original_native = _codex_followup_state_path(REPO).read_bytes()
    slots = ImplementationSlotRepository(REPO, 1)
    owner = ImplementationOwner("issue", 9)
    assert _commit_retirement(slots, owner) == incarnation
    assert slots.reserve(owner)
    fresh = slots.establish_incarnation(owner)
    assert fresh != incarnation
    CodexWorkAccounting(slots).initialize_fresh(owner, fresh)
    assert slots.record_implementation_pr(owner, SECOND)
    CloudRunRepository(REPO).save(second_run())
    wham.resolve_latest_assistant_turn.side_effect = lambda task_id: task_id + "~assttrn_1"
    target = ExistingPrRepairTarget(REPO, SECOND, "second-branch", "main", "b" * 40)
    with pytest.raises(ImplementationSlotUnavailable, match="inventory is incomplete"):
        CodexCloudClient(repo_name=REPO).continue_if_paused(SECOND_TASK, prompt=build_existing_pr_repair_prompt(target, "Authorized continuation"), repair_target=target, force=force)
    wham.send_follow_up.assert_called_once()
    assert _codex_followup_state_path(REPO).read_bytes() == original_native
    assert all(item.send_provenance is None for item in operations(fresh) if item.task_id == TASK)


def test_different_repository_same_issue_number_has_its_own_verified_subject(ordinary):
    run, old, _new, wham, _github, incarnation = ordinary
    run((old,))
    original = operations(incarnation)
    repository = "independent/repo"
    slots = ImplementationSlotRepository(repository, 1)
    owner = ImplementationOwner("issue", 9)
    assert slots.reserve(owner)
    fresh = slots.establish_incarnation(owner)
    CodexWorkAccounting(slots).initialize_fresh(owner, fresh)
    assert slots.record_implementation_pr(owner, SECOND)
    CloudRunRepository(repository).save(second_run(repository=repository))
    wham.resolve_latest_assistant_turn.side_effect = lambda task_id: task_id + "~assttrn_1"
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)
    target = ExistingPrRepairTarget(repository, SECOND, "second-branch", "main", "b" * 40)
    assert CodexCloudClient(repo_name=repository).continue_if_paused(SECOND_TASK, prompt=build_existing_pr_repair_prompt(target, "Authorized continuation"), repair_target=target)
    assert wham.send_follow_up.call_count == 2
    assert operations(incarnation) == original


@pytest.mark.parametrize("subject", ["different_branch", "same_pr", "same_head_other_base"])
@pytest.mark.parametrize("force", [False, True])
def test_real_ci_producer_retains_authority_and_separates_only_another_pr(ordinary, subject, force):
    from contextlib import nullcontext
    from unittest.mock import patch

    from auto_coder.automation_config import AutomationConfig
    from auto_coder.ci_repair_authority import CIRepairAuthority
    from auto_coder.pr_processor import _send_codex_cloud_error_feedback
    from auto_coder.util.gh_cache import PullRequestRepairMetadata

    run, old, _new, wham, github, incarnation = ordinary
    run((old,))
    original = next(item for item in operations(incarnation) if item.kind == "follow-up")
    same_pr = subject == "same_pr"
    separate = subject == "different_branch"
    number = PR if same_pr else SECOND
    task_id = TASK if not separate else SECOND_TASK
    branch = "second-branch" if separate else "work-branch"
    base = "release" if subject == "same_head_other_base" else "main"
    if not same_pr:
        assert ImplementationSlotRepository(REPO, 1).record_implementation_pr(ImplementationOwner("issue", 9), SECOND)
        if subject == "same_head_other_base":
            other = CloudRunRepository(REPO).get(9, 1)
            other.add_pull_request(SECOND)
        else:
            other = second_run(branch=branch)
            other.base_branch = base
        CloudRunRepository(REPO).save(other)
    data = {"number": number, "state": "open", "body": "Fixes #9", "head": {"ref": branch, "sha": "b" * 40, "repo": {"full_name": REPO}}, "base": {"ref": base, "sha": "c" * 40, "repo": {"id": 3}}, "labels": [], "user": {"login": "maintainer"}}
    github.get_pull_request_metadata_strict.return_value = data
    github.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(branch, "b" * 40, base)
    wham.resolve_latest_assistant_turn.side_effect = lambda task: task + "~assttrn_1"
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)
    with patch("auto_coder.pr_processor.current_ci_failure_authority", return_value=nullcontext(CIRepairAuthority(True, "current controlled exact-head CI failure", "b" * 40))):
        result = _send_codex_cloud_error_feedback(REPO, data, [{"name": "PR Tests", "conclusion": "failure"}], AutomationConfig(), github, force=force)
    assert result.delivered is separate, result.actions
    assert wham.send_follow_up.call_count == (2 if separate else 1)
    if separate:
        assert wham.send_follow_up.call_args.kwargs["task_id"] == task_id
    assert next(item for item in operations(incarnation) if item.logical_operation_id == original.logical_operation_id) == original
