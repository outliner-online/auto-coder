"""Production pre-effect fence for ordinary Issue-owned Codex work."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from .codex_observation import CodexRunObservation, ObservationBinding, PullRequestPresence
from .codex_work_accounting import CodexSendProvenance, CodexWorkAccounting, CodexWorkPhase, WorkAccountingStatus
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotUnavailable

T = TypeVar("T")


class CodexWorkOutsideScope(LookupError):
    """The task has no durable ordinary Issue-owned Codex provenance."""


class CodexWorkAuthorityRefused(ImplementationSlotUnavailable):
    """The final bounded authority read refused before native reservation/wire."""


class CodexWorkOverlap(ImplementationSlotUnavailable):
    """An ambiguous owner operation has no exhaustively separate scope."""


@dataclass(frozen=True)
class CodexWorkIdentity:
    """Authoritative identity carried from a durable producer to transport."""

    repository: str
    issue_number: int
    incarnation: str
    operation_id: str
    operation_kind: str
    source_request_id: str
    task_id: Optional[str] = None
    causal_baseline: Optional[str] = None


@dataclass(frozen=True)
class _CodexSubject:
    pr_number: Optional[int]
    head_repository: str = ""
    head_ref: str = ""

    def separate_from(self, other: _CodexSubject) -> bool:
        if self.pr_number is not None and self.pr_number == other.pr_number:
            return False
        if any(ref.startswith("refs/") or any(character.isspace() for character in ref) for ref in (self.head_ref, other.head_ref)) or any(
            len(repo.split("/")) != 2 or not all(repo.split("/")) or ":" in repo or any(character.isspace() for character in repo) for repo in (self.head_repository, other.head_repository)
        ):
            return False  # Only canonical GitHub full_name + short head-ref evidence qualifies.
        # A first publication has no PR number yet. Distinct immutable GitHub
        # head branches are positive separation only after both bindings verify.
        return bool(self.head_repository and self.head_ref and other.head_repository and other.head_ref and (self.head_repository.casefold(), self.head_ref) != (other.head_repository.casefold(), other.head_ref))


class CodexWorkFence:
    """Register a mutation before invoking its external side effect.

    The caller remains responsible for deciding whether work is allowed.  This
    class only proves that an already-authorized logical operation is attached
    to the current Issue reservation before transport can observe it.
    """

    def __init__(self, accounting: CodexWorkAccounting) -> None:
        self.accounting = accounting

    def _subject(self, identity: CodexWorkIdentity, provenance: Optional[CodexSendProvenance], *, publication_observation: Optional[CodexRunObservation] = None, incoming: bool = False) -> Optional[_CodexSubject]:
        from .cloud_run import CloudRunRepository
        from .codex_pr_attribution import AttributionDisposition, CodexPrAttributionRepository
        from .implementation_slots import ImplementationSlotSnapshot

        if provenance is None:
            return None
        slots = self.accounting.slots.snapshot()
        owner = ImplementationOwner("issue", identity.issue_number)
        bound = next((item for item in slots.owners if item.owner == owner), None) if isinstance(slots, ImplementationSlotSnapshot) else None
        runs = [run for run in CloudRunRepository(identity.repository).list_all() if run.provider == "codex-cloud" and run.task_id == identity.task_id and run.issue_number == identity.issue_number and run.repo_name == identity.repository]
        if bound is None or bound.incarnation != identity.incarnation or len(runs) != 1:
            return None
        run = runs[0]

        def authenticated_head(pr_number: int, head_ref: Optional[str]) -> tuple[str, str]:
            # This existing authenticated attribution producer establishes this
            # evidence only after exact GitHub head repo/ref == launch intent.
            # A task URL or PR association alone does not establish the head.
            attribution = CodexPrAttributionRepository(identity.repository).get(pr_number)
            origin = attribution.origin
            if attribution.disposition is not AttributionDisposition.VERIFIED or origin is None:
                return "", ""
            if (origin.repository, origin.pr_number, origin.issue_number, origin.provider, origin.task_id, origin.attempt, origin.launch_identity, origin.evidence) != (
                identity.repository,
                pr_number,
                identity.issue_number,
                "codex-cloud",
                run.task_id,
                run.attempt,
                run.launch_identity,
                "retained-publication-intent+closing-reference",
            ):
                return "", ""
            if not head_ref or head_ref != run.publication_head_ref or run.publication_head_repository.casefold() != identity.repository.casefold():
                return "", ""
            return origin.repository, head_ref

        if provenance.pr_number is not None:
            if provenance.pr_number not in bound.implementation_prs or provenance.pr_number not in run.pull_request_numbers:
                return None
            head_repo, head_ref = authenticated_head(provenance.pr_number, provenance.head_branch)
            return _CodexSubject(provenance.pr_number, head_repo, head_ref)
        if not provenance.publication_launch_identity or (provenance.publication_launch_identity, provenance.publication_head_repository, provenance.publication_head_ref) != (run.launch_identity, run.publication_head_repository, run.publication_head_ref):
            return None
        if incoming and (
            publication_observation is None
            or publication_observation.binding != ObservationBinding.from_run(run)
            or publication_observation.errors
            or publication_observation.issue_state != "open"
            or publication_observation.pull_request.presence is not PullRequestPresence.NO_MATCHING_PR
            or not publication_observation.execution.recovery_eligible
            or publication_observation.execution.assistant_turn_id != identity.causal_baseline
            or run.pull_request_numbers
        ):
            return None
        if run.pull_request_numbers:
            if len(run.pull_request_numbers) != 1 or run.pull_request_numbers[0] not in bound.implementation_prs:
                return None
            head_repo, head_ref = authenticated_head(run.pull_request_numbers[0], run.publication_head_ref)
            return _CodexSubject(run.pull_request_numbers[0], head_repo, head_ref)
        return _CodexSubject(None, run.publication_head_repository, run.publication_head_ref)

    def execute(self, identity: CodexWorkIdentity, send: Callable[[], T], force: bool = False, *, send_provenance: Optional[CodexSendProvenance] = None, publication_observation: Optional[CodexRunObservation] = None, final_authority_check: Optional[Callable[[], bool]] = None) -> T:
        if identity.repository != self.accounting.slots.repo_name:
            raise ImplementationSlotUnavailable("Codex work belongs to a different repository")
        owner = ImplementationOwner("issue", identity.issue_number)
        with self.accounting.slots.serialize(owner):
            if send_provenance is not None and send_provenance.strong_source:
                from .strong_correction_scope import validate_request

                if final_authority_check is None:
                    raise CodexWorkAuthorityRefused("Accepted Strong correction requires final authority recheck")
                validate_request(self.accounting, identity, send_provenance)
            if identity.operation_kind in {"follow-up", "publication", "repair"}:
                snapshot = self.accounting.snapshot(owner, identity.incarnation)
                if snapshot.accounting_status is not WorkAccountingStatus.COMPLETE:
                    raise ImplementationSlotUnavailable("Codex send inventory is incomplete")
                subject = self._subject(identity, send_provenance, publication_observation=publication_observation, incoming=True)
                if send_provenance is not None and send_provenance.publication_launch_identity and subject is None:
                    raise CodexWorkOverlap("Current publication subject has no verified observation/binding")
                for operation in snapshot.operations:
                    ambiguous = (operation.send_provenance is not None and operation.send_provenance.outcome in {"reserved", "unknown"}) or (operation.send_provenance is None and operation.phase in {CodexWorkPhase.RESERVED, CodexWorkPhase.DELIVERY_UNKNOWN})
                    if not ambiguous:
                        continue
                    old_identity = CodexWorkIdentity(identity.repository, identity.issue_number, identity.incarnation, operation.logical_operation_id, operation.kind, operation.source_request_id, operation.task_id, operation.causal_baseline)
                    old_subject = self._subject(old_identity, operation.send_provenance)
                    if subject is None or old_subject is None or not subject.separate_from(old_subject):
                        if send_provenance is not None and send_provenance.strong_source:
                            from .strong_correction_scope import validate_send

                            try:
                                validate_send(self.accounting, identity, send_provenance, old_operation=operation)
                            except ValueError:
                                pass
                            else:
                                continue
                        raise CodexWorkOverlap("Ambiguous wildcard Codex work overlaps this request; duplicate send refused before transport")
            if final_authority_check is not None:
                try:
                    current = final_authority_check() is True
                except Exception as exc:
                    raise CodexWorkAuthorityRefused("Final scoped authority read failed before transport") from exc
                if not current:
                    raise CodexWorkAuthorityRefused("Final scoped authority changed before transport")
            registration = self.accounting.register(
                owner,
                identity.incarnation,
                logical_operation_id=identity.operation_id,
                kind=identity.operation_kind,
                source_request_id=identity.source_request_id,
                causal_baseline=identity.causal_baseline,
                task_id=identity.task_id,
                send_provenance=send_provenance,
            )
            if not registration.created and send_provenance is not None:
                raise CodexWorkOverlap("Existing Codex wire reservation cannot be replayed by force; duplicate send refused")
            if not registration.created and not force:
                raise ImplementationSlotUnavailable("Codex logical operation was already accounted; duplicate send refused")
            return send()

    def record_delivery(
        self,
        identity: CodexWorkIdentity,
        *,
        accepted: bool,
        indeterminate: bool,
        evidence_id: str,
        task_id: Optional[str] = None,
    ) -> None:
        owner = ImplementationOwner("issue", identity.issue_number)
        self.accounting.record_send_outcome(owner, identity.incarnation, identity.operation_id, source_request_id=identity.source_request_id, causal_baseline=identity.causal_baseline, task_id=identity.task_id, outcome="accepted" if accepted else "unknown" if indeterminate else "not_delivered")
        phase = CodexWorkPhase.ACCEPTED if accepted else CodexWorkPhase.DELIVERY_UNKNOWN
        if not accepted and not indeterminate:
            phase = CodexWorkPhase.SETTLED
        self.accounting.transition(
            owner,
            identity.incarnation,
            identity.operation_id,
            phase,
            evidence_id=evidence_id,
            evidence_source_request_id=identity.source_request_id if phase is CodexWorkPhase.SETTLED else None,
            evidence_causal_baseline=identity.causal_baseline if phase is CodexWorkPhase.SETTLED else None,
            task_id=task_id or identity.task_id,
            definite_non_delivery=phase is CodexWorkPhase.SETTLED,
        )


def stable_codex_operation_id(kind: str, source_request_id: str) -> str:
    """Derive a replay-stable, non-secret accounting key."""
    digest = hashlib.sha256(f"{kind}\0{source_request_id}".encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def production_codex_fence(repository: str, task_id: str, kind: str, source_request_id: str, causal_baseline: Optional[str] = None, *, reconcile: bool = True) -> tuple[CodexWorkFence, CodexWorkIdentity]:
    """Resolve a task through durable CloudRun and current slot evidence.

    Scoped admission may read existing accounting without enriching old records.
    Actual transport keeps the default reconciliation and registration guards.
    """
    from .cloud_run import CloudRunRepository
    from .codex_work_reconstruction import production_codex_reconstructor
    from .implementation_slots import ImplementationSlotRepository

    if not repository:
        raise CodexWorkOutsideScope("Codex client has no repository ownership context")
    matches = [run for run in CloudRunRepository(repository).list_all() if run.provider == "codex-cloud" and run.task_id == task_id]
    if not matches:
        raise CodexWorkOutsideScope("Codex task is positively outside tracked Issue work")
    if len(matches) != 1:
        raise ImplementationSlotUnavailable("Codex task has no unique durable Issue ownership")
    run = matches[0]
    slots = ImplementationSlotRepository(repository, 1)
    owner = ImplementationOwner("issue", run.issue_number)
    incarnation = slots.owner_incarnation(owner)
    if not incarnation:
        raise ImplementationSlotUnavailable("Codex task owner has no active incarnation")
    reconstructor = production_codex_reconstructor(repository, run.issue_number)
    accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
    if reconcile:
        accounting.reconcile_from_receipt(owner, incarnation, reconstructor.reconstruct())
    identity = CodexWorkIdentity(
        repository,
        run.issue_number,
        incarnation,
        stable_codex_operation_id(kind, source_request_id),
        kind,
        source_request_id,
        task_id,
        causal_baseline,
    )
    return CodexWorkFence(accounting), identity


def production_codex_issue_fence(
    repository: str,
    issue_number: int,
    kind: str,
    source_request_id: str,
    slots: Optional[ImplementationSlotRepository] = None,
) -> tuple[CodexWorkFence, CodexWorkIdentity]:
    """Prepare a pre-task fence for an already-reserved Issue owner."""
    from .codex_work_reconstruction import production_codex_reconstructor

    slots = slots or ImplementationSlotRepository(repository, 1)
    owner = ImplementationOwner("issue", issue_number)
    incarnation = slots.owner_incarnation(owner)
    if not incarnation:
        raise ImplementationSlotUnavailable("Codex Issue owner has no active incarnation")
    reconstructor = production_codex_reconstructor(repository, issue_number)
    accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
    accounting.reconcile_from_receipt(owner, incarnation, reconstructor.reconstruct())
    return CodexWorkFence(accounting), CodexWorkIdentity(
        repository,
        issue_number,
        incarnation,
        stable_codex_operation_id(kind, source_request_id),
        kind,
        source_request_id,
    )
