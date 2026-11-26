"""Fresh, exact-head authority for starting CI-triggered repair work."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Iterator

from .ci_observation import (
    CheckObservation,
    CIConclusion,
    CIObservationFact,
    CIObservationSnapshot,
    ObservationAvailability,
    WorkflowObservation,
)
from .github_ci_observer import ci_observation_merge_authority, ci_read_phase, observe_ci
from .util.gh_cache import get_ghapi_client

if TYPE_CHECKING:
    from .ci_cancellation_recovery import CancellationEvidence, RecoveryPolicy

_FAILURES = frozenset({CIConclusion.FAILURE, CIConclusion.TIMED_OUT})
_SETTLED = _FAILURES | frozenset({CIConclusion.SUCCESS, CIConclusion.SKIPPED, CIConclusion.NEUTRAL, CIConclusion.CANCELLED})


def _is_repair_failure(fact: CIObservationFact) -> bool:
    return fact.conclusion in _FAILURES and fact.repair_trigger is not False


@dataclass(frozen=True)
class CIRepairAuthority:
    """Result of the final admission read at a repair-effect boundary."""

    allowed: bool
    reason: str
    head_sha: str
    snapshot: CIObservationSnapshot | None = None

    @property
    def failure_identities(self) -> tuple[str, ...]:
        if self.snapshot is None:
            return ()
        identities: list[str] = []
        for fact in _current_attempt_facts(self.snapshot):
            if not _is_repair_failure(fact):
                continue
            if isinstance(fact, WorkflowObservation):
                workflow_execution = fact.execution
                identities.append(f"workflow:{workflow_execution.workflow_id}:run:{workflow_execution.run_id}:attempt:{workflow_execution.attempt}")
            elif isinstance(fact, CheckObservation):
                check_execution = fact.execution
                identities.append(f"check:{check_execution.app_id}:{check_execution.check_id}")
        return tuple(identities)


def _current_attempt_facts(snapshot: CIObservationSnapshot) -> tuple[Any, ...]:
    """Discard explicitly superseded workflow attempts, without guessing identity."""
    newest: dict[tuple[str, str], int] = {}
    for fact in snapshot.facts:
        if isinstance(fact, WorkflowObservation) and fact.execution.attempt is not None:
            execution_key = (fact.execution.workflow_id, fact.execution.run_id)
            newest[execution_key] = max(newest.get(execution_key, 0), fact.execution.attempt)
    return tuple(fact for fact in snapshot.facts if not isinstance(fact, WorkflowObservation) or fact.execution.attempt is None or fact.execution.attempt == newest[(fact.execution.workflow_id, fact.execution.run_id)])


def evaluate_ci_repair_snapshot(snapshot: CIObservationSnapshot) -> tuple[bool, str]:
    """Apply the fail-closed selected-fact policy to one complete read cycle."""
    if snapshot.availability is not ObservationAvailability.KNOWN:
        return False, f"CI evidence is {snapshot.availability.value}"
    facts = _current_attempt_facts(snapshot)
    if not facts:
        return False, "CI evidence is empty"
    if any(fact.conclusion not in _SETTLED for fact in facts):
        return False, "CI evidence contains pending, unknown, or action-required facts"
    if not any(_is_repair_failure(fact) for fact in facts):
        return False, "CI has no current terminal failure"
    return True, "current exact-head CI failure"


def _annotate_cancellation_repair(snapshot: CIObservationSnapshot, evidence: CancellationEvidence) -> CIObservationSnapshot:
    """Keep native merge facts while separating interrupted gates from failures.

    An aggregate's name has authority only after explicit workflow configuration
    resolves it to a unique native job and check identity in this complete read.
    Non-aggregate failures remain repair evidence even alongside cancellations.
    """
    if not evidence.available:
        raise ValueError(evidence.reason)
    workflow_triggers: dict[tuple[str, str, int], bool] = {}
    check_triggers: dict[str, tuple[bool, str]] = {}
    selected_workflows: dict[str, tuple[str, int]] = {}
    for run in evidence.runs:
        selected_workflows[run.workflow_path] = (str(run.run_id), run.attempt)
        matching = [fact for fact in snapshot.facts if isinstance(fact, WorkflowObservation) and fact.execution.run_id == str(run.run_id) and fact.execution.attempt == run.attempt and fact.execution.workflow_id == str(run.workflow_id)]
        if len(matching) != 1 or matching[0].workflow_path != run.workflow_path or matching[0].conclusion.value != run.conclusion:
            raise ValueError("native cancellation evidence changed during the CI read")
        if run.status != "completed" or any(job.status != "completed" for job in run.jobs):
            raise ValueError("native cancellation evidence includes active execution")
        if not run.cancelled_roots:
            continue
        # Unknown check provenance cannot be converted into an aggregate exemption.
        if any(job.check_run_id is None for job in run.jobs):
            raise ValueError("native cancellation evidence lacks exact check identity")
        aggregates = frozenset(run.policy.aggregate_job_names)
        independent_failure = any(job.conclusion in {"failure", "timed_out"} and job.name not in aggregates for job in run.jobs)
        workflow_triggers[(str(run.workflow_id), str(run.run_id), run.attempt)] = independent_failure
        for job in run.jobs:
            identity = str(job.check_run_id)
            if identity in check_triggers:
                raise ValueError("native cancellation evidence has ambiguous check identity")
            check_triggers[identity] = (job.name not in aggregates and job.conclusion in {"failure", "timed_out"}, job.conclusion)
    annotated: list[CIObservationFact] = []
    for fact in snapshot.facts:
        if isinstance(fact, WorkflowObservation):
            execution = fact.execution
            selected = selected_workflows.get(fact.workflow_path)
            if selected is not None and (execution.run_id, execution.attempt) != selected:
                annotated.append(replace(fact, repair_trigger=False))
                continue
            trigger = workflow_triggers.get((execution.workflow_id, execution.run_id, execution.attempt)) if execution.attempt is not None else None
            annotated.append(replace(fact, repair_trigger=trigger) if trigger is not None else fact)
        else:
            classified = check_triggers.get(fact.execution.check_id)
            if classified is None:
                annotated.append(fact)
                continue
            trigger, native_conclusion = classified
            if fact.conclusion.value != native_conclusion:
                raise ValueError("native job and check conclusions changed during the CI read")
            annotated.append(replace(fact, repair_trigger=trigger))
    return replace(snapshot, facts=tuple(annotated))


@contextmanager
def current_ci_failure_authority(
    github_client: Any,
    repository: str,
    pr_number: int,
    expected_head: str,
    *,
    recovery_policies: tuple[RecoveryPolicy, ...] = (),
) -> Iterator[CIRepairAuthority]:
    """Re-read PR and CI, then fence accepted invalidations through first effect.

    Callers must enter this context only after read-only preparation and put the
    first provider request or local repair effect inside it.  The authority is
    deliberately neither persisted nor reusable by a later repair initiation.
    """
    if github_client is None:
        yield CIRepairAuthority(False, "an authoritative GitHub client is unavailable", expected_head)
        return
    try:
        metadata = github_client.get_pull_request_metadata_strict(repository, pr_number)
    except Exception as exc:
        yield CIRepairAuthority(False, f"strict PR read is unavailable: {exc}", expected_head)
        return
    head = metadata.get("head") if isinstance(metadata, dict) else None
    live_head = head.get("sha") if isinstance(head, dict) else None
    if metadata.get("state") != "open" or metadata.get("merged_at") is not None:
        yield CIRepairAuthority(False, "PR is no longer open", expected_head)
        return
    if live_head != expected_head:
        yield CIRepairAuthority(False, "PR head changed before repair initiation", expected_head)
        return
    stack = ExitStack()
    try:
        permissions = {"actions": "read", "checks": "read", "pull_requests": "read"}
        if callable(getattr(type(github_client), "api_for", None)):
            api = github_client.api_for(repository, required_permissions=permissions, subsystem="github-ci-observer", extra_headers={"Cache-Control": "no-cache"})
            authority_key = github_client.authority_key_for(repository, required_permissions=permissions)
        else:
            api = get_ghapi_client(github_client.token, subsystem="github-ci-observer", extra_headers={"Cache-Control": "no-cache"})
            authority_key = f"legacy:{repository}:{tuple(sorted(permissions.items()))}"
        stack.enter_context(ci_read_phase("ci-repair-final-admission"))
        snapshot = observe_ci(api, authority_key, repository, pr_number, expected_head)
        original_snapshot = snapshot
        allowed, reason = evaluate_ci_repair_snapshot(snapshot)
        if allowed and recovery_policies:
            from .ci_cancellation_recovery import inspect_cancellation_evidence

            evidence = inspect_cancellation_evidence(api, repository, pr_number, expected_head, recovery_policies)
            configured_paths = {policy.workflow_path for policy in recovery_policies}
            observed_paths = {fact.workflow_path for fact in snapshot.facts if isinstance(fact, WorkflowObservation) and fact.workflow_path in configured_paths}
            if observed_paths - {run.workflow_path for run in evidence.runs}:
                raise ValueError("configured workflows disappeared during the native CI read")
            snapshot = _annotate_cancellation_repair(snapshot, evidence)
            allowed, reason = evaluate_ci_repair_snapshot(snapshot)
        if allowed:
            # An annotated copy is not a cached observation capability. The
            # original read fences all additional native reads and governor waits.
            current = stack.enter_context(ci_observation_merge_authority(original_snapshot))
    except Exception as exc:
        stack.close()
        yield CIRepairAuthority(False, f"CI observation is unavailable: {exc}", expected_head)
        return
    if not allowed:
        stack.close()
        yield CIRepairAuthority(False, reason, expected_head, snapshot)
        return
    if not current:
        stack.close()
        yield CIRepairAuthority(
            False,
            "CI evidence was invalidated before repair initiation",
            expected_head,
            snapshot,
        )
        return
    try:
        yield CIRepairAuthority(True, reason, expected_head, snapshot)
    finally:
        stack.close()
