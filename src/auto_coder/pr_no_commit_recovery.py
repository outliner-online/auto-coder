"""Bounded checkpoints for accepted, exact-PR cloud correction requests.

This extends the existing review-delivery document, not provider execution state.
An entered send with no confirmed result stays reserved across process restart.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator, Protocol

from .controller_identity_history import assert_active_controller_authority
from .pr_repair import ExistingPrRepairTarget
from .runtime_locks import file_lock

INTERVAL_SECONDS = 3600
MAX_CONTINUATIONS = 3


@dataclass(frozen=True)
class ContinuationCapability:
    """Native call shape; all providers retain the shared durable reservation."""

    native_logical_identity: bool = False

    def identities(self, logical_identity: str) -> tuple[str, ...]:
        return (logical_identity,) if self.native_logical_identity else ()


SUPPORTED_PROVIDERS = {
    "codex-cloud": ContinuationCapability(native_logical_identity=True),
    "claude-routine": ContinuationCapability(),
}


@contextmanager
def review_repair_state_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_suffix(".lock")):
        yield


@contextmanager
def pr_correction_lock(path: Path, pr_number: int) -> Iterator[None]:
    """A slow provider send owns only its PR, not every PR in the document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_name(f"{path.name}.pr-{pr_number}.lock")):
        yield


@dataclass
class NoCommitSeries:
    request_id: str
    provider: str
    task_id: str
    pr_number: int
    head_branch: str
    base_branch: str
    head_sha: str
    accepted_at: float
    ordinal: int = 0
    status: str = "accepted"
    outcome: str = ""


@dataclass(frozen=True)
class CheckpointResult:
    reason: str
    ordinal: int = 0
    next_check_at: float | None = None


class ContinuationAdapter(Protocol):
    """Same-owner authenticated sender; False means unknown, never rejection."""

    def __call__(self, logical_identity: str, target: ExistingPrRepairTarget, /) -> bool: ...


class NoCommitRecovery:
    """Use the existing delivery document for guarded admission and send receipts."""

    def __init__(self, path: Path):
        self.path = path

    def _document(self) -> dict[str, object]:
        value = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(value, dict):
            raise ValueError("review delivery state must be an object")
        return value

    def _records(self) -> dict[str, NoCommitSeries]:
        raw = self._document().get("no_commit_series", {})
        if not isinstance(raw, dict):
            raise ValueError("no_commit_series must be an object")
        records = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError("invalid no-commit request")
            record = NoCommitSeries(**value)
            if (
                record.request_id != key
                or record.provider not in SUPPORTED_PROVIDERS
                or not record.task_id
                or type(record.pr_number) is not int
                or record.pr_number <= 0
                or not all(isinstance(item, str) and item for item in (record.head_branch, record.base_branch, record.head_sha))
                or type(record.ordinal) is not int
                or not 0 <= record.ordinal <= MAX_CONTINUATIONS
                or type(record.accepted_at) not in (int, float)
                or not math.isfinite(record.accepted_at)
                or record.status not in {"accepted", "reserved", "ended"}
            ):
                raise ValueError("invalid no-commit series evidence")
            records[key] = record
        return records

    def _save(self, records: dict[str, NoCommitSeries], pr_number: int, accepted_identities: tuple[str, ...] = ()) -> None:
        # Other PRs may accept or finish work while this PR's transport is in
        # flight. Merge only this guarded target, preserving their latest rows.
        with review_repair_state_lock(self.path):
            current = self._records()
            settles_acceptance = bool(accepted_identities) or any(value.pr_number == pr_number and value.status == "accepted" and current.get(key) != value for key, value in records.items())
            if settles_acceptance:
                assert_active_controller_authority()
            current.update({key: value for key, value in records.items() if value.pr_number == pr_number})
            document = self._document()
            document["no_commit_series"] = {key: asdict(value) for key, value in current.items()}
            if accepted_identities:
                delivered = document.get("delivered_feedback", [])
                pending = document.get("pending_feedback", [])
                if not isinstance(delivered, list) or not isinstance(pending, list) or not all(isinstance(identity, str) for identity in (*delivered, *pending)):
                    raise ValueError("review delivery receipts must be string lists")
                # This is the confirmed original POST's exact source receipt,
                # committed atomically with its recovery clock. Other pending
                # sources, including unknown POSTs, are never promoted.
                document["delivered_feedback"] = sorted(set(delivered) | set(accepted_identities))
                document["pending_feedback"] = sorted(set(pending) - set(accepted_identities))
            temporary = self.path.with_suffix(f".json.{os.getpid()}.tmp")
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(document, output, indent=2, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            if settles_acceptance:
                # This writer also promotes shared feedback receipts. Preserve
                # the accepted observation if authority changed during I/O.
                assert_active_controller_authority()
            os.replace(temporary, self.path)

    @staticmethod
    def request_identity(provider: str, task_id: str, target: ExistingPrRepairTarget, identities: tuple[str, ...], message: str) -> str:
        # Stable source identity excludes observation time and continuation ordinal.
        source = json.dumps((target.repo_name, target.pr_number, provider, task_id, sorted(identities) if identities else message), sort_keys=True)
        return hashlib.sha256(source.encode()).hexdigest()

    def record_acceptance(self, provider: str, task_id: str, target: ExistingPrRepairTarget, identities: tuple[str, ...], message: str, now: float) -> None:
        if provider not in SUPPORTED_PROVIDERS:
            return
        if not task_id or type(target.pr_number) is not int or target.pr_number <= 0 or not all((target.repo_name, target.head_sha, target.head_branch, target.base_branch)) or not math.isfinite(now):
            raise ValueError("accepted checkpoint requests require an observed PR baseline and clock")
        identity = self.request_identity(provider, task_id, target, identities, message)
        with pr_correction_lock(self.path, target.pr_number):
            records = self._records()
            if identity in records:
                self._save(records, target.pr_number, accepted_identities=identities)
                return
            for record in records.values():
                if record.pr_number == target.pr_number and record.status != "ended":
                    record.status, record.outcome = "ended", "superseded_request"
            records[identity] = NoCommitSeries(identity, provider, task_id, target.pr_number, target.head_branch, target.base_branch, target.head_sha, now)
            self._save(records, target.pr_number, accepted_identities=identities)

    def supersede_prior_request(self, provider: str, task_id: str, target: ExistingPrRepairTarget, identities: tuple[str, ...], message: str) -> None:
        """A competing send attempt also holds old recovery if its result is lost."""
        identity = self.request_identity(provider, task_id, target, identities, message)
        with pr_correction_lock(self.path, target.pr_number):
            records = self._records()
            changed = False
            for record in records.values():
                if record.pr_number == target.pr_number and record.request_id != identity and record.status != "ended":
                    record.status, record.outcome = "ended", "superseded_request"
                    changed = True
            if changed:
                self._save(records, target.pr_number)

    def has_due_request(self, pr_number: int, now: float) -> bool:
        with review_repair_state_lock(self.path):
            return any(record.pr_number == pr_number and record.status == "accepted" and record.ordinal < MAX_CONTINUATIONS and now >= record.accepted_at + INTERVAL_SECONDS for record in self._records().values())

    def end_series(self, pr_number: int, outcome: str = "superseded_request") -> None:
        """Retire known terminal/superseded work without allocating a budget."""
        with pr_correction_lock(self.path, pr_number):
            records = self._records()
            changed = False
            for record in records.values():
                if record.pr_number == pr_number and record.status != "ended":
                    record.status, record.outcome = "ended", outcome
                    changed = True
            if changed:
                self._save(records, pr_number)

    def checkpoint(
        self,
        provider: str,
        task_id: str,
        target: ExistingPrRepairTarget,
        now: Callable[[], float],
        current: Callable[[], str],
        send: ContinuationAdapter,
    ) -> CheckpointResult:
        """Current returns eligible, a terminal outcome, or a transient hold reason.

        The callback uses the ordinary PR admission/ownership context. It does not
        inspect provider execution status. It runs inside the same guard as new
        request acceptance and the exact logical outbound reservation.
        """
        if provider not in SUPPORTED_PROVIDERS:
            return CheckpointResult("unsupported_provider")
        with pr_correction_lock(self.path, target.pr_number):
            records = self._records()
            candidates = [record for record in records.values() if record.pr_number == target.pr_number and record.status != "ended"]
            if not candidates:
                return CheckpointResult("no_accepted_request")
            if len(candidates) != 1:
                return CheckpointResult("ambiguous_request")
            record = candidates[0]
            if (provider, task_id) != (record.provider, record.task_id):
                record.status, record.outcome = "ended", "owner_changed"
            elif (target.head_sha, target.head_branch, target.base_branch) != (record.head_sha, record.head_branch, record.base_branch):
                # This is a baseline invalidation, not a claim that an arbitrary
                # changed head (bot, merge, empty or unrelated) implemented work.
                record.status, record.outcome = "ended", "baseline_changed"
            if record.status == "ended":
                self._save(records, target.pr_number)
                return CheckpointResult(record.outcome, record.ordinal)
            if record.status == "reserved":
                return CheckpointResult("delivery_unknown", record.ordinal)
            if record.ordinal >= MAX_CONTINUATIONS:
                return CheckpointResult("limit_reached", record.ordinal)
            due = record.accepted_at + INTERVAL_SECONDS
            if now() < due:
                return CheckpointResult("not_due", record.ordinal, due)
            reason = current()
            if reason != "eligible":
                if reason in {"closed", "owner_changed", "baseline_changed", "no_change", "input_blocked", "acknowledged_outcome"}:
                    record.status, record.outcome = "ended", reason
                    self._save(records, target.pr_number)
                return CheckpointResult(reason, record.ordinal)
            record.ordinal += 1
            record.status = "reserved"
            self._save(records, target.pr_number)  # Persist exact ordinal BEFORE entering the sender.
            logical_id = f"no-commit:{record.request_id}:{record.ordinal}"
            try:
                accepted = send(logical_id, target)
            except Exception as exc:
                # Only an explicit pre-send refusal can release this ordinal.
                from .claude_followup_waits import ClaudeFollowupHoldActive
                from .exceptions import DeliveryCertainty

                if isinstance(exc, ClaudeFollowupHoldActive) or getattr(exc, "delivery_certainty", None) is DeliveryCertainty.NOT_SENT:
                    record.ordinal -= 1
                    record.status = "accepted"
                    self._save(records, target.pr_number)
                raise
            if not accepted:
                return CheckpointResult("delivery_unknown", record.ordinal)
            record.status = "accepted"
            record.accepted_at = now()  # Fresh interval from confirmed acceptance.
            self._save(records, target.pr_number)
            return CheckpointResult("accepted", record.ordinal, record.accepted_at + INTERVAL_SECONDS if record.ordinal < MAX_CONTINUATIONS else None)
