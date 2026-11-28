"""Durable execution of explicit-local pull-request review corrections."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from .durable_repair_allowance import (
    CompletionAvailability,
    CorrectiveGenerationBundle,
    DeliveryOutcome,
    GenerationLifecycleState,
    RepairAllowanceLedger,
    ValidationObservation,
)
from .execution_trace import EventKind, Outcome, get_trace_collector
from .git_branch import git_commit_with_retry
from .git_commit import git_push
from .git_info import parse_github_repo_from_url
from .llm_backend_config import TASK_ONLY_BACKEND_TYPES, active_repo_context, get_llm_config
from .logger_config import get_logger
from .pr_automation_guard import assess_local_pr_automation
from .process_identity import ProcessIdentity, process_is_dead, read_process_identity
from .review_correction_policy import validate_review_correction_basis_json
from .utils import bind_command_execution_cwd, reset_command_execution_cwd

if TYPE_CHECKING:
    from .controller_git_identity import ControllerGitIdentity

logger = get_logger(__name__)


@dataclass(frozen=True)
class LocalReviewRepairRequest:
    repository: str
    pr_number: int
    head_repository: str
    head_ref: str
    head_sha: str
    feedback_identities: tuple[str, ...]
    prompt: str
    expected_push_url: str = ""
    review_correction_basis_json: str = ""

    @property
    def attempt_id(self) -> str:
        material = "\n".join((self.repository, str(self.pr_number), self.head_sha, *sorted(self.feedback_identities)))
        return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class LocalReviewRepairOutcome:
    phase: str
    reason: str
    executed: bool = False
    published: bool = False


@dataclass(frozen=True)
class LocalReviewRepairRecord:
    attempt_id: str = ""
    head_sha: str = ""
    head_ref: str = ""
    phase: str = ""
    result_sha: str = ""
    reason: str = ""
    incarnation: int = 0
    workspace_path: str = ""
    invocation_entered: bool = False
    local_job_id: str = ""
    feedback_identities: tuple[str, ...] = ()
    expected_push_url: str = ""
    updated_at: float = 0.0
    owner_pid: Optional[int] = None
    owner_boot_id: str = ""
    owner_start_ticks: Optional[int] = None
    pre_push_evidence_json: str = ""
    publication_started: bool = False
    publication_send_state: str = "unknown"
    review_correction_basis_json: str = ""


def review_correction_basis_refusal(value: LocalReviewRepairRequest | LocalReviewRepairRecord) -> str:
    """Require retained pre-edit evidence only for captured reviewer output."""
    if not any(identity.startswith("review-output:") for identity in value.feedback_identities):
        return ""
    return validate_review_correction_basis_json(value.review_correction_basis_json, value.head_sha)


@dataclass(frozen=True)
class LocalReviewRepairSupersession:
    """One immutable evidence-only disposition of a retained correction."""

    attempt_id: str
    incarnation: int
    replacement_head: str
    evidence_json: str
    settled_at: float


@dataclass(frozen=True)
class ReviewCommitRecovery:
    """Immutable source and identity evidence for one controller commit entry."""

    attempt_id: str
    incarnation: int
    original_record_json: str
    evidence_json: str
    identity_json: str
    phase: str = "started"
    result_sha: str = ""
    reason: str = ""


@dataclass(frozen=True)
class LocalReviewRepairClaim:
    admitted: bool
    phase: str
    attempt_id: str
    incarnation: int = 0


class LocalBackendUnavailableError(RuntimeError):
    """Raised before invocation when no configured local candidate can run."""


class LocalRepairValidationRequired(RuntimeError):
    """A completed generation needs independent validation before another repair."""


def settle_local_review_repair_validation(
    repository: str,
    pr_number: int,
    head_sha: str,
    observations: tuple[ValidationObservation, ...],
    *,
    ledger: Optional[RepairAllowanceLedger] = None,
    store: Optional[LocalReviewRepairStore] = None,
    expected_generation_id: Optional[str] = None,
) -> None:
    """Settle only independently adjudicated feedback after the published result."""
    ledger = ledger or RepairAllowanceLedger()
    snapshot = ledger.get_snapshot("https://api.github.com", repository, pr_number)
    generation = snapshot.get_outstanding_generation()
    if generation is None or generation.owning_identity != "local-review-repair" or generation.lifecycle_state != GenerationLifecycleState.PENDING_REVALIDATION:
        return
    if expected_generation_id is not None and generation.generation_id != expected_generation_id:
        raise RuntimeError("pending local repair generation changed before verification settlement")
    store = store or LocalReviewRepairStore(local_review_repair_db_path(repository))
    request = LocalReviewRepairRequest(repository, pr_number, repository, "", head_sha, (), "")
    record = store.get(request, generation.bundle_reference)
    if record is None or not record.result_sha:
        return
    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", record.result_sha, head_sha], capture_output=True)
    if ancestry.returncode != 0:
        return
    covered = tuple(observation for observation in observations if observation.blocker_id in generation.covered_blocker_ids)
    if covered:
        ledger.record_validation_results(
            "https://api.github.com",
            repository,
            pr_number,
            f"local-validation-{generation.generation_id}-{time.time_ns()}",
            snapshot.epoch,
            generation.generation_id,
            covered,
        )


@dataclass
class LocalRepairAllowanceAuthority:
    ledger: RepairAllowanceLedger
    request: LocalReviewRepairRequest
    generation_id: str
    epoch: int

    def mark_invocation(self) -> None:
        snapshot = self.ledger.record_delivery_outcome(
            "https://api.github.com",
            self.request.repository,
            self.request.pr_number,
            f"local-delivery-{self.request.attempt_id}",
            self.epoch,
            self.generation_id,
            DeliveryOutcome.CONFIRMED,
            self.request.attempt_id,
            evidence="local mutating invocation entered",
        )
        self.epoch = snapshot.epoch

    def mark_completion(self, *, code_changed: bool, evidence: str) -> None:
        snapshot = self.ledger.record_completion_observation(
            "https://api.github.com",
            self.request.repository,
            self.request.pr_number,
            f"local-completion-{self.request.attempt_id}",
            self.epoch,
            self.generation_id,
            CompletionAvailability.KNOWN,
            completion_seq=time.time_ns(),
            code_changed=code_changed,
            evidence=evidence,
        )
        self.epoch = snapshot.epoch


def admit_local_repair_allowance(
    request: LocalReviewRepairRequest,
    ledger: Optional[RepairAllowanceLedger] = None,
) -> tuple[Optional[LocalRepairAllowanceAuthority], str]:
    """Acquire or recover the production corrective-generation authority."""
    ledger = ledger or RepairAllowanceLedger()
    snapshot = ledger.initialize_namespace("https://api.github.com", request.repository, request.pr_number)
    outstanding = snapshot.get_outstanding_generation()
    if outstanding is not None:
        if outstanding.owning_identity == "local-review-repair":
            if outstanding.lifecycle_state == GenerationLifecycleState.PENDING_REVALIDATION:
                raise LocalRepairValidationRequired(f"local correction {outstanding.bundle_reference} awaits independent validation")
            if outstanding.bundle_reference != request.attempt_id:
                return None, f"another local corrective attempt is outstanding: {outstanding.bundle_reference}"
            return LocalRepairAllowanceAuthority(ledger, request, outstanding.generation_id, snapshot.epoch), ""
        return None, f"another corrective generation is outstanding: {outstanding.generation_id}"
    bundle = CorrectiveGenerationBundle(
        bundle_reference=request.attempt_id,
        covered_blocker_ids=request.feedback_identities,
        owning_identity="local-review-repair",
        observed_baseline=request.head_sha,
    )
    admission = ledger.admit_generation(
        "https://api.github.com",
        request.repository,
        request.pr_number,
        f"local-admit-{request.attempt_id}",
        snapshot.epoch,
        bundle,
        open_blocker_ids=request.feedback_identities,
    )
    if not admission.admitted or not admission.generation_id or admission.snapshot is None:
        return None, admission.denial_reason or "repair allowance admission was refused"
    return LocalRepairAllowanceAuthority(ledger, request, admission.generation_id, admission.snapshot.epoch), ""


def local_review_repair_db_path(repository: str) -> Path:
    return Path.home() / ".auto-coder" / repository / "local_review_repairs.sqlite3"


class LocalReviewRepairStore:
    """SQLite claim store that serializes all corrective work for one PR."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS local_review_repair_attempts (
                repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                attempt_id TEXT NOT NULL, head_sha TEXT NOT NULL, head_ref TEXT NOT NULL,
                feedback_json TEXT NOT NULL, phase TEXT NOT NULL,
                result_sha TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL, incarnation INTEGER NOT NULL,
                workspace_path TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(repository, pr_number, attempt_id))""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(local_review_repair_attempts)")}
            for name, declaration in (("owner_pid", "INTEGER"), ("owner_boot_id", "TEXT"), ("owner_start_ticks", "INTEGER")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE local_review_repair_attempts ADD COLUMN {name} {declaration}")
            if "invocation_entered" not in columns:
                connection.execute("ALTER TABLE local_review_repair_attempts ADD COLUMN invocation_entered INTEGER NOT NULL DEFAULT 0")
            if "local_job_id" not in columns:
                connection.execute("ALTER TABLE local_review_repair_attempts ADD COLUMN local_job_id TEXT NOT NULL DEFAULT ''")
            if "expected_push_url" not in columns:
                connection.execute("ALTER TABLE local_review_repair_attempts ADD COLUMN expected_push_url TEXT NOT NULL DEFAULT ''")
            if "pre_push_evidence_json" not in columns:
                connection.execute("ALTER TABLE local_review_repair_attempts ADD COLUMN pre_push_evidence_json TEXT NOT NULL DEFAULT ''")
            if "review_correction_basis_json" not in columns:
                # Existing output keeps its provenance, but gains no new
                # correction authority merely by upgrading the store.
                connection.execute("ALTER TABLE local_review_repair_attempts ADD COLUMN review_correction_basis_json TEXT NOT NULL DEFAULT ''")
            if "publication_started" not in columns:
                connection.execute("ALTER TABLE local_review_repair_attempts ADD COLUMN publication_started INTEGER NOT NULL DEFAULT 0")
            if "publication_send_state" not in columns:
                # Legacy candidate checkpoints do not establish whether a push
                # was entered. Unknown records may reconcile, but never resend.
                connection.execute("ALTER TABLE local_review_repair_attempts ADD COLUMN publication_send_state TEXT NOT NULL DEFAULT 'unknown'")
            connection.execute("""CREATE TABLE IF NOT EXISTS local_review_repair_supersessions (
                repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                attempt_id TEXT NOT NULL, incarnation INTEGER NOT NULL,
                replacement_head TEXT NOT NULL, evidence_json TEXT NOT NULL,
                settled_at REAL NOT NULL,
                PRIMARY KEY(repository, pr_number, attempt_id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS local_review_commit_recoveries (
                repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                attempt_id TEXT NOT NULL, incarnation INTEGER NOT NULL,
                original_record_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                identity_json TEXT NOT NULL, phase TEXT NOT NULL,
                result_sha TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(repository, pr_number, attempt_id))""")
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def admit(self, request: LocalReviewRepairRequest) -> LocalReviewRepairClaim:
        """Atomically claim the PR, or describe the retained recovery phase."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_dead_owners(connection, request)
            exact = connection.execute(
                "SELECT phase, incarnation FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? AND attempt_id=?",
                (request.repository, request.pr_number, request.attempt_id),
            ).fetchone()
            if exact is not None:
                phase, previous_incarnation = str(exact[0]), int(exact[1])
                if phase == "not_started":
                    active = connection.execute(
                        "SELECT attempt_id, phase FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? AND attempt_id<>? " "AND phase IN ('executing', 'indeterminate', 'publication_pending') " "ORDER BY updated_at DESC LIMIT 1",
                        (request.repository, request.pr_number, request.attempt_id),
                    ).fetchone()
                    if active is not None:
                        connection.commit()
                        return LocalReviewRepairClaim(False, str(active[1]), str(active[0]))
                    incarnation = time.time_ns()
                    connection.execute(
                        "UPDATE local_review_repair_attempts SET phase='executing', reason='', invocation_entered=0, local_job_id='', publication_send_state='not_entered', review_correction_basis_json=?, updated_at=?, incarnation=? " "WHERE repository=? AND pr_number=? AND attempt_id=?",
                        (request.review_correction_basis_json, time.time(), incarnation, request.repository, request.pr_number, request.attempt_id),
                    )
                    self._set_owner(connection, request)
                    connection.commit()
                    return LocalReviewRepairClaim(True, "executing", request.attempt_id, incarnation)
                connection.commit()
                return LocalReviewRepairClaim(False, phase, request.attempt_id, previous_incarnation)
            active = connection.execute(
                "SELECT attempt_id, phase, incarnation FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? " "AND phase IN ('executing', 'indeterminate', 'publication_pending') " "ORDER BY updated_at DESC LIMIT 1",
                (request.repository, request.pr_number),
            ).fetchone()
            if active is not None:
                connection.commit()
                return LocalReviewRepairClaim(False, str(active[1]), str(active[0]), int(active[2]))
            incarnation = int(time.time_ns())
            connection.execute(
                "INSERT INTO local_review_repair_attempts "
                "(repository, pr_number, attempt_id, head_sha, head_ref, feedback_json, phase, result_sha, reason, updated_at, incarnation, workspace_path, expected_push_url, publication_send_state, review_correction_basis_json) "
                "VALUES (?, ?, ?, ?, ?, ?, 'executing', '', '', ?, ?, '', ?, 'not_entered', ?)",
                (
                    request.repository,
                    request.pr_number,
                    request.attempt_id,
                    request.head_sha,
                    request.head_ref,
                    json.dumps(request.feedback_identities),
                    time.time(),
                    incarnation,
                    request.expected_push_url,
                    request.review_correction_basis_json,
                ),
            )
            self._set_owner(connection, request)
            connection.commit()
            return LocalReviewRepairClaim(True, "executing", request.attempt_id, incarnation)

    def _set_owner(self, connection: sqlite3.Connection, request: LocalReviewRepairRequest) -> None:
        owner = read_process_identity(os.getpid())
        connection.execute(
            "UPDATE local_review_repair_attempts SET owner_pid=?, owner_boot_id=?, owner_start_ticks=? " "WHERE repository=? AND pr_number=? AND attempt_id=?",
            (owner.pid if owner else None, owner.boot_id if owner else None, owner.start_ticks if owner else None, request.repository, request.pr_number, request.attempt_id),
        )

    def _recover_dead_owners(self, connection: sqlite3.Connection, request: LocalReviewRepairRequest) -> None:
        rows = connection.execute(
            "SELECT attempt_id, owner_pid, owner_boot_id, owner_start_ticks, workspace_path, head_sha, pre_push_evidence_json " "FROM local_review_repair_attempts WHERE repository=? AND pr_number=? AND phase='executing'",
            (request.repository, request.pr_number),
        ).fetchall()
        for attempt_id, pid, boot_id, start_ticks, workspace, head_sha, pre_push_evidence_json in rows:
            # Legacy or unavailable ownership evidence cannot prove an exit.
            if pid is None or not boot_id or start_ticks is None:
                continue
            if not process_is_dead(ProcessIdentity(pid, boot_id, start_ticks), read_process_identity(pid)):
                continue
            # Captured settled output remains recovery work even when the owner
            # exited before recording its final local failure disposition.
            phase, result_sha = ("indeterminate" if pre_push_evidence_json else "not_started"), ""
            if workspace and Path(workspace).exists():
                result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True)
                if result.returncode != 0:
                    continue
                if result.stdout.strip() != head_sha:
                    # A controller commit may precede the publication checkpoint.
                    phase, result_sha = "publication_pending", result.stdout.strip()
            reason = f"Recovered interrupted local correction: owner process {pid} exited"
            connection.execute(
                "UPDATE local_review_repair_attempts SET phase=?, result_sha=?, reason=?, updated_at=?, " "publication_started=CASE WHEN ?='publication_pending' THEN 1 ELSE publication_started END " "WHERE repository=? AND pr_number=? AND attempt_id=? AND phase='executing'",
                (phase, result_sha, reason, time.time(), phase, request.repository, request.pr_number, attempt_id),
            )
            logger.warning(f"{request.repository} PR #{request.pr_number}: {reason}; next phase={phase}")
            try:
                get_trace_collector().record_event(
                    EventKind.STAGE_RESULT,
                    "pr.local-repair-recovery",
                    "local-review-repair",
                    label="Interrupted local correction recovered",
                    outcome=Outcome.DEFERRED,
                    facts={"pr_number": request.pr_number, "attempt_id": attempt_id, "owner_pid": pid, "local_phase": phase, "reason": reason, "workspace_path": workspace},
                )
            except Exception:
                logger.debug("Could not record local correction recovery trace", exc_info=True)

    def transition(
        self,
        request: LocalReviewRepairRequest,
        claim: LocalReviewRepairClaim,
        phase: str,
        *,
        result_sha: str = "",
        reason: str = "",
        workspace_path: str = "",
    ) -> bool:
        if phase == "superseded":
            return False  # Only evidence settlement may create this disposition.
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE local_review_repair_attempts SET phase=?, result_sha=?, reason=?, workspace_path=?, "
                "invocation_entered=CASE WHEN ?='not_started' THEN 0 ELSE invocation_entered END, updated_at=?, "
                "publication_started=CASE WHEN ?='publication_pending' THEN 1 ELSE publication_started END "
                "WHERE repository=? AND pr_number=? AND attempt_id=? AND incarnation=? AND phase<>'superseded'",
                (phase, result_sha, reason, workspace_path, phase, time.time(), phase, request.repository, request.pr_number, claim.attempt_id, claim.incarnation),
            )
            return cursor.rowcount == 1

    def mark_invocation_entered(self, request: LocalReviewRepairRequest, claim: LocalReviewRepairClaim, *, local_job_id: str = "") -> bool:
        """Fence backend entry against transfer to a durable local job."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE local_review_repair_attempts SET invocation_entered=1, updated_at=? " "WHERE repository=? AND pr_number=? AND attempt_id=? AND incarnation=? " "AND phase='executing' AND invocation_entered=0 AND local_job_id=?",
                (time.time(), request.repository, request.pr_number, claim.attempt_id, claim.incarnation, local_job_id),
            )
            return cursor.rowcount == 1

    def checkpoint_pre_push(self, request: LocalReviewRepairRequest, claim: LocalReviewRepairClaim, evidence_json: str) -> bool:
        """Retain one immutable checkpoint of settled pre-push output."""
        if not evidence_json or claim.attempt_id != request.attempt_id:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE local_review_repair_attempts SET pre_push_evidence_json=?, "
                "updated_at=CASE WHEN pre_push_evidence_json='' THEN ? ELSE updated_at END "
                "WHERE repository=? AND pr_number=? AND attempt_id=? AND incarnation=? "
                "AND phase='executing' AND invocation_entered=1 AND result_sha='' AND publication_started=0 AND publication_send_state<>'entered' "
                "AND head_sha=? AND head_ref=? AND expected_push_url=? "
                "AND (pre_push_evidence_json='' OR pre_push_evidence_json=?)",
                (evidence_json, time.time(), request.repository, request.pr_number, claim.attempt_id, claim.incarnation, request.head_sha, request.head_ref, request.expected_push_url, evidence_json),
            )
            return cursor.rowcount == 1

    def _get_record(self, connection: sqlite3.Connection, request: LocalReviewRepairRequest, attempt_id: str) -> Optional[LocalReviewRepairRecord]:
        row = connection.execute(
            "SELECT attempt_id, head_sha, head_ref, phase, result_sha, reason, incarnation, workspace_path, "
            "invocation_entered, local_job_id, feedback_json, expected_push_url, updated_at, "
            "owner_pid, owner_boot_id, owner_start_ticks, pre_push_evidence_json, publication_started, publication_send_state, review_correction_basis_json "
            "FROM local_review_repair_attempts WHERE repository=? AND pr_number=? AND attempt_id=?",
            (request.repository, request.pr_number, attempt_id),
        ).fetchone()
        if row is None:
            return None
        return LocalReviewRepairRecord(
            attempt_id=str(row[0]),
            head_sha=str(row[1]),
            head_ref=str(row[2]),
            phase=str(row[3]),
            result_sha=str(row[4]),
            reason=str(row[5]),
            incarnation=int(row[6]),
            workspace_path=str(row[7]),
            invocation_entered=bool(row[8]),
            local_job_id=str(row[9]),
            feedback_identities=tuple(json.loads(row[10])),
            expected_push_url=str(row[11]),
            updated_at=float(row[12]),
            owner_pid=int(row[13]) if row[13] is not None else None,
            owner_boot_id=str(row[14] or ""),
            owner_start_ticks=int(row[15]) if row[15] is not None else None,
            pre_push_evidence_json=str(row[16]),
            publication_started=bool(row[17]),
            publication_send_state=str(row[18]),
            review_correction_basis_json=str(row[19]),
        )

    def get(self, request: LocalReviewRepairRequest, attempt_id: Optional[str] = None) -> Optional[LocalReviewRepairRecord]:
        with self._connect() as connection:
            return self._get_record(connection, request, attempt_id or request.attempt_id)

    def _get_supersession(self, connection: sqlite3.Connection, request: LocalReviewRepairRequest, attempt_id: str) -> Optional[LocalReviewRepairSupersession]:
        row = connection.execute(
            "SELECT attempt_id, incarnation, replacement_head, evidence_json, settled_at " "FROM local_review_repair_supersessions WHERE repository=? AND pr_number=? AND attempt_id=?",
            (request.repository, request.pr_number, attempt_id),
        ).fetchone()
        if row is None:
            return None
        return LocalReviewRepairSupersession(str(row[0]), int(row[1]), str(row[2]), str(row[3]), float(row[4]))

    def get_supersession(self, request: LocalReviewRepairRequest, attempt_id: Optional[str] = None) -> Optional[LocalReviewRepairSupersession]:
        """Read provenance without reclassifying or changing retained work."""
        with self._connect() as connection:
            return self._get_supersession(connection, request, attempt_id or request.attempt_id)

    def settle_superseded(
        self,
        request: LocalReviewRepairRequest,
        expected_record: LocalReviewRepairRecord,
        replacement_head: str,
        evidence_json: str,
        confirm: Callable[[], bool],
    ) -> bool:
        """Atomically release exactly the verified old blocker, retaining evidence.

        The caller supplies the read-only Git/remote evidence confirmation. It
        runs under the same database write transaction as the final comparison
        so another evaluator or stale execution cannot replace this attempt.
        """
        if (
            not replacement_head
            or request.head_repository != request.repository
            or replacement_head == expected_record.head_sha
            or not evidence_json
            or request.attempt_id != expected_record.attempt_id
            or (request.head_sha, request.head_ref, request.feedback_identities, request.expected_push_url) != (expected_record.head_sha, expected_record.head_ref, expected_record.feedback_identities, expected_record.expected_push_url)
            or expected_record.phase != "indeterminate"
            or expected_record.result_sha
            or expected_record.publication_started
        ):
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_record(connection, request, expected_record.attempt_id)
            prior = self._get_supersession(connection, request, expected_record.attempt_id)
            if prior is not None:
                matches = current == replace(expected_record, phase="superseded", updated_at=prior.settled_at) and expected_record.incarnation == prior.incarnation and prior.replacement_head == replacement_head and prior.evidence_json == evidence_json
                connection.commit()
                return matches
            if current != expected_record or not confirm():
                connection.rollback()
                return False
            settled_at = time.time()
            connection.execute(
                "INSERT INTO local_review_repair_supersessions " "(repository, pr_number, attempt_id, incarnation, replacement_head, evidence_json, settled_at) " "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (request.repository, request.pr_number, expected_record.attempt_id, expected_record.incarnation, replacement_head, evidence_json, settled_at),
            )
            connection.execute(
                "UPDATE local_review_repair_attempts SET phase='superseded', updated_at=? " "WHERE repository=? AND pr_number=? AND attempt_id=? AND incarnation=? AND phase='indeterminate'",
                (settled_at, request.repository, request.pr_number, expected_record.attempt_id, expected_record.incarnation),
            )
            connection.commit()
            return True

    def _get_commit_recovery(self, connection: sqlite3.Connection, request: LocalReviewRepairRequest) -> Optional[ReviewCommitRecovery]:
        row = connection.execute(
            "SELECT attempt_id, incarnation, original_record_json, evidence_json, identity_json, phase, result_sha, reason " "FROM local_review_commit_recoveries WHERE repository=? AND pr_number=? AND attempt_id=?",
            (request.repository, request.pr_number, request.attempt_id),
        ).fetchone()
        return ReviewCommitRecovery(*row) if row is not None else None

    def get_commit_recovery(self, request: LocalReviewRepairRequest) -> Optional[ReviewCommitRecovery]:
        """Read the original recovery proof without changing attempt ownership."""
        with self._connect() as connection:
            return self._get_commit_recovery(connection, request)

    def begin_commit_recovery(
        self,
        request: LocalReviewRepairRequest,
        expected: LocalReviewRepairRecord,
        evidence_json: str,
        identity_json: str,
        confirm: Callable[[], bool],
    ) -> Optional[ReviewCommitRecovery]:
        """Persist one commit-entry intent after an exact-attempt comparison."""
        if expected.phase != "indeterminate" or expected.result_sha or expected.publication_started or expected.publication_send_state == "entered" or request.attempt_id != expected.attempt_id or not evidence_json or not identity_json:
            return None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._get_record(connection, request, request.attempt_id) != expected or self._get_commit_recovery(connection, request) is not None or not confirm():
                connection.rollback()
                return None
            recovery = ReviewCommitRecovery(
                request.attempt_id,
                expected.incarnation,
                json.dumps(asdict(expected), sort_keys=True, separators=(",", ":")),
                evidence_json,
                identity_json,
            )
            connection.execute(
                "INSERT INTO local_review_commit_recoveries " "(repository, pr_number, attempt_id, incarnation, original_record_json, evidence_json, identity_json, phase) " "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (request.repository, request.pr_number, recovery.attempt_id, recovery.incarnation, recovery.original_record_json, recovery.evidence_json, recovery.identity_json, recovery.phase),
            )
            connection.commit()
            return recovery

    def finish_commit_recovery(
        self,
        request: LocalReviewRepairRequest,
        expected: LocalReviewRepairRecord,
        recovery: ReviewCommitRecovery,
        *,
        result_sha: str = "",
        reason: str = "",
        confirm: Callable[[], bool],
    ) -> bool:
        """Checkpoint a candidate before push, preserving original failure evidence."""
        if expected.publication_send_state == "entered":
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._get_record(connection, request, request.attempt_id) != expected or self._get_commit_recovery(connection, request) != recovery or not confirm():
                connection.rollback()
                return False
            phase = "candidate" if result_sha else "failed"
            connection.execute(
                "UPDATE local_review_commit_recoveries SET phase=?, result_sha=?, reason=? " "WHERE repository=? AND pr_number=? AND attempt_id=?",
                (phase, result_sha, reason, request.repository, request.pr_number, request.attempt_id),
            )
            if result_sha:
                connection.execute(
                    "UPDATE local_review_repair_attempts SET phase='publication_pending', result_sha=?, publication_started=1, publication_send_state='not_entered', updated_at=? " "WHERE repository=? AND pr_number=? AND attempt_id=? AND incarnation=?",
                    (result_sha, time.time(), request.repository, request.pr_number, request.attempt_id, expected.incarnation),
                )
            connection.commit()
            return True

    def mark_publication_send_entered(self, request: LocalReviewRepairRequest, expected: LocalReviewRepairRecord) -> bool:
        """Durably consume the sole send entry before invoking Git transport."""
        if expected.phase != "publication_pending" or not expected.result_sha or expected.publication_send_state != "not_entered":
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._get_record(connection, request, expected.attempt_id) != expected:
                connection.rollback()
                return False
            connection.execute(
                "UPDATE local_review_repair_attempts SET publication_send_state='entered', updated_at=? " "WHERE repository=? AND pr_number=? AND attempt_id=? AND incarnation=?",
                (time.time(), request.repository, request.pr_number, expected.attempt_id, expected.incarnation),
            )
            connection.commit()
            return True

    def get_unfinished(self, request: LocalReviewRepairRequest, *, feedback_prefix: str) -> Optional[LocalReviewRepairRecord]:
        """Observe owned unfinished output, recovering only proven dead owners."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_dead_owners(connection, request)
            rows = connection.execute(
                "SELECT attempt_id, feedback_json FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? " "AND phase IN ('executing', 'indeterminate', 'publication_pending') " "ORDER BY updated_at DESC",
                (request.repository, request.pr_number),
            ).fetchall()
            connection.commit()
        for attempt_id, feedback_json in rows:
            identities = json.loads(feedback_json)
            if identities and all(isinstance(value, str) and value.startswith(feedback_prefix) for value in identities):
                record = self.get(request, str(attempt_id))
                if record is not None and record.phase in {"executing", "indeterminate", "publication_pending"}:
                    return record
        return None

    def get_publication_pending(self, request: LocalReviewRepairRequest) -> Optional[LocalReviewRepairRecord]:
        """Return retained publication work for this PR without admitting a new claim."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_id FROM local_review_repair_attempts WHERE repository=? AND pr_number=? " "AND phase='publication_pending' ORDER BY updated_at DESC LIMIT 1",
                (request.repository, request.pr_number),
            ).fetchone()
        return self.get(request, str(row[0])) if row is not None else None


def select_local_review_repair_candidates(repository: str) -> list[str]:
    """Return quota-ranked synchronous candidates from the ordinary policy."""
    config = get_llm_config(repo_name=repository)
    groups = config.get_ordinary_priority_groups()
    local_groups = [[name for name in group if config.resolve_backend_type(name) not in TASK_ONLY_BACKEND_TYPES] for group in groups]
    local_groups = [group for group in local_groups if group]
    if not local_groups:
        return []
    from .quota_selector import rank_high_score_backends_by_quota

    return rank_high_score_backends_by_quota(local_groups, config)


def _prepare_default_executor(request: LocalReviewRepairRequest) -> Callable[[LocalReviewRepairRequest, str], str]:
    from .cli_helpers import build_backend_manager

    with active_repo_context(request.repository):
        candidates = select_local_review_repair_candidates(request.repository)
        if not candidates:
            raise LocalBackendUnavailableError("no configured synchronous local backend is available")
        config = get_llm_config(repo_name=request.repository)
        manager = build_backend_manager(
            selected_backends=candidates,
            primary_backend=candidates[0],
            models={name: config.get_model_for_backend(name) or name for name in candidates},
            automatic_session_resume=False,
        )

    def invoke(_request: LocalReviewRepairRequest, worktree: str) -> str:
        with active_repo_context(request.repository):
            token = bind_command_execution_cwd(worktree)
            try:
                return manager.run_prompt(request.prompt)
            finally:
                reset_command_execution_cwd(token)

    return invoke


def _default_executor(request: LocalReviewRepairRequest, worktree: str) -> str:
    invoke = _prepare_default_executor(request)
    return invoke(request, worktree)


def _remote_head(root: Path, head_ref: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", "ls-remote", "origin", f"refs/heads/{head_ref}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    fields = result.stdout.split()
    return True, fields[0] if fields else ""


def _has_commit(root: Path, sha: str) -> bool:
    if not sha:
        return False
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=root,
            capture_output=True,
        )
        return result.returncode == 0
    except (OSError, ValueError):
        return False


def _ensure_head_commit_available(root: Path, request: LocalReviewRepairRequest) -> None:
    if _has_commit(root, request.head_sha):
        return
    try:
        has_remote = (
            subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=root,
                capture_output=True,
            ).returncode
            == 0
        )
        if not has_remote:
            return
        if request.head_ref:
            subprocess.run(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    "origin",
                    f"+refs/heads/{request.head_ref}:refs/remotes/origin/{request.head_ref}",
                ],
                cwd=root,
                capture_output=True,
                timeout=60,
            )
        if not _has_commit(root, request.head_sha):
            subprocess.run(
                ["git", "fetch", "--no-tags", "origin", request.head_sha],
                cwd=root,
                capture_output=True,
                timeout=60,
            )
        if not _has_commit(root, request.head_sha) and request.pr_number:
            subprocess.run(
                ["git", "fetch", "--no-tags", "origin", f"pull/{request.pr_number}/head"],
                cwd=root,
                capture_output=True,
                timeout=60,
            )
    except (OSError, subprocess.SubprocessError):
        pass


def get_origin_push_url(root: Path) -> str:
    """Require one push destination belonging to the admitted fetch repository."""
    result = subprocess.run(["git", "remote", "get-url", "--push", "--all", "origin"], cwd=root, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("origin push destination is unavailable")
    fetch = subprocess.run(["git", "remote", "get-url", "--all", "origin"], cwd=root, capture_output=True, text=True)
    push_urls = result.stdout.strip().splitlines()
    fetch_urls = fetch.stdout.strip().splitlines()
    if fetch.returncode != 0 or len(fetch_urls) != 1 or len(push_urls) != 1:
        raise RuntimeError("review publication requires one unambiguous origin destination")
    push_url, fetch_url = push_urls[0], fetch_urls[0]
    if push_url != fetch_url:
        push_repo = parse_github_repo_from_url(push_url)
        fetch_repo = parse_github_repo_from_url(fetch_url)
        if not push_repo or not fetch_repo or push_repo.casefold() != fetch_repo.casefold():
            raise RuntimeError("origin push destination does not match the admitted fetch repository")
    return push_url


def _resume_publication(
    request: LocalReviewRepairRequest,
    store: LocalReviewRepairStore,
    record: LocalReviewRepairRecord,
    root: Path,
    *,
    publication_authorizer: Optional[Callable[[], bool]] = None,
) -> LocalReviewRepairOutcome:
    """Reconcile or publish retained output without rerunning implementation."""
    if not record.result_sha:
        return LocalReviewRepairOutcome("indeterminate", "retained correction has no recoverable commit", executed=True)
    if record.expected_push_url:
        try:
            matches_destination = get_origin_push_url(root) == record.expected_push_url
        except Exception:
            matches_destination = False
        if not matches_destination:
            return LocalReviewRepairOutcome("publication_pending", "origin push destination changed or is unavailable; retained output was not pushed", executed=True)
    readable, remote_head = _remote_head(root, request.head_ref)
    if not readable:
        return LocalReviewRepairOutcome("publication_pending", f"remote branch could not be reconciled: {remote_head}", executed=True)
    if remote_head == record.result_sha:
        claim = LocalReviewRepairClaim(False, record.phase, record.attempt_id, record.incarnation)
        if not store.transition(request, claim, "awaiting_validation", result_sha=record.result_sha):
            return LocalReviewRepairOutcome("publication_pending", "candidate readback lost attempt ownership before settlement", executed=True)
        return LocalReviewRepairOutcome(
            "awaiting_validation",
            f"reconciled published {record.result_sha} on {request.head_ref}; independent validation is required",
            executed=True,
            published=True,
        )
    if remote_head != record.head_sha:
        return LocalReviewRepairOutcome(
            "publication_pending",
            f"publication conflict: {request.head_ref} advanced to {remote_head or 'an unavailable ref'}",
            executed=True,
        )
    if record.publication_send_state != "not_entered":
        return LocalReviewRepairOutcome("publication_pending", f"publication send disposition is {record.publication_send_state}; original-head readback does not authorize another push", executed=True)
    if publication_authorizer is not None:
        try:
            authorized = publication_authorizer()
        except Exception as exc:
            return LocalReviewRepairOutcome("publication_pending", f"publication authority is unavailable: {exc}", executed=True)
        if not authorized:
            return LocalReviewRepairOutcome("publication_pending", "publication authority changed; retained output was not pushed", executed=True)
    return _send_pending_publication(request, store, record, root, publication_authorizer=publication_authorizer)


def _send_pending_publication(
    request: LocalReviewRepairRequest,
    store: LocalReviewRepairStore,
    record: LocalReviewRepairRecord,
    root: Path,
    *,
    publication_authorizer: Optional[Callable[[], bool]] = None,
) -> LocalReviewRepairOutcome:
    """Enter one send, retaining ambiguity independently of candidate creation."""
    if publication_authorizer is not None:
        try:
            if not publication_authorizer():
                return LocalReviewRepairOutcome("publication_pending", "PR automation is stopped or its authority is unavailable; retained output was not pushed", executed=True)
        except Exception as exc:
            return LocalReviewRepairOutcome("publication_pending", f"publication authority unavailable: {exc}", executed=True)
    if record.publication_send_state != "not_entered":
        return LocalReviewRepairOutcome("publication_pending", f"publication send disposition is {record.publication_send_state}; awaiting authoritative candidate readback without another push", executed=True)
    basis_refusal = review_correction_basis_refusal(record)
    if basis_refusal:
        return LocalReviewRepairOutcome("publication_pending", f"reviewer correction publication refused: {basis_refusal}", executed=True)
    local_guard = assess_local_pr_automation(request.repository, request.pr_number, root, record.head_sha, record.result_sha)
    if local_guard is not None:
        return LocalReviewRepairOutcome("publication_pending", local_guard.reason, executed=True)
    if not store.mark_publication_send_entered(request, record):
        return LocalReviewRepairOutcome("publication_pending", "publication send checkpoint lost attempt ownership; no push was started", executed=True)
    claim = LocalReviewRepairClaim(False, record.phase, record.attempt_id, record.incarnation)
    try:
        push = git_push(
            cwd=str(root),
            remote="origin",
            branch=f"{record.result_sha}:{record.head_ref}",
            expected_remote_sha=record.head_sha,
        )
    except Exception as exc:
        reason = f"publication send outcome is unresolved: {exc}"
        store.transition(request, claim, "publication_pending", result_sha=record.result_sha, reason=reason, workspace_path=record.workspace_path)
        return LocalReviewRepairOutcome("publication_pending", reason, executed=True)
    if not push.success:
        store.transition(request, claim, "publication_pending", result_sha=record.result_sha, reason=push.stderr, workspace_path=record.workspace_path)
        return LocalReviewRepairOutcome("publication_pending", f"publication send outcome is unresolved: {push.stderr}", executed=True)
    if not store.transition(request, claim, "awaiting_validation", result_sha=record.result_sha):
        return LocalReviewRepairOutcome("publication_pending", "confirmed push could not checkpoint completion; candidate readback is required", executed=True)
    return LocalReviewRepairOutcome(
        "awaiting_validation",
        f"published retained {record.result_sha} to {record.head_ref}; independent validation is required",
        executed=True,
        published=True,
    )


def execute_local_review_repair(
    request: LocalReviewRepairRequest,
    *,
    store: Optional[LocalReviewRepairStore] = None,
    executor: Optional[Callable[[LocalReviewRepairRequest, str], str]] = None,
    allowance_authority: Optional[LocalRepairAllowanceAuthority] = None,
    accepted_claim: Optional[LocalReviewRepairClaim] = None,
    local_job_id: str = "",
    provider_entry_authorizer: Optional[Callable[[], bool]] = None,
    provider_entry_checkpoint: Optional[Callable[[], bool]] = None,
    root: Optional[Path] = None,
    allow_commit_repair: bool = True,
    resume_other_attempts: bool = True,
    publication_authorizer: Optional[Callable[[], bool]] = None,
    commit_identity_provider: Optional[Callable[[], ControllerGitIdentity]] = None,
) -> LocalReviewRepairOutcome:
    """Run one fenced correction in a detached exact-head checkout and publish it."""
    if request.head_repository != request.repository:
        return LocalReviewRepairOutcome("not_admitted", "foreign-head pull requests are not eligible")
    repo_root = (root or Path.cwd()).resolve()
    store = store or LocalReviewRepairStore(local_review_repair_db_path(request.repository))
    basis_refusal = review_correction_basis_refusal(request)
    if basis_refusal:
        retained = store.get_publication_pending(request)
        if retained is not None and (resume_other_attempts or retained.attempt_id == request.attempt_id):
            # Legacy attempts can still observe an already published candidate;
            # the send boundary independently requires the stored basis.
            return _resume_publication(request, store, retained, repo_root, publication_authorizer=publication_authorizer)
        return LocalReviewRepairOutcome("not_admitted", f"reviewer correction refused: {basis_refusal}")
    if executor is None and accepted_claim is None:
        retained = store.get_publication_pending(request)
        if retained is not None:
            outcome = _resume_publication(request, store, retained, repo_root, publication_authorizer=publication_authorizer)
            if outcome.published and allowance_authority is not None:
                allowance_authority.mark_completion(code_changed=True, evidence=f"local correction published as {retained.result_sha}")
            return outcome
        try:
            executor = _prepare_default_executor(request)
        except LocalBackendUnavailableError as exc:
            return LocalReviewRepairOutcome("backend_unavailable", str(exc))
    claim = accepted_claim or store.admit(request)
    if not claim.admitted and accepted_claim is None:
        if not resume_other_attempts and claim.attempt_id != request.attempt_id:
            return LocalReviewRepairOutcome(claim.phase, "another corrective attempt owns this PR; reviewer output was not published")
        if claim.phase == "publication_pending":
            record = store.get(request, claim.attempt_id)
            if record is not None:
                outcome = _resume_publication(request, store, record, repo_root, publication_authorizer=publication_authorizer)
                if outcome.published and allowance_authority is not None:
                    allowance_authority.mark_completion(code_changed=True, evidence=f"local correction published as {record.result_sha}")
                return outcome
        return LocalReviewRepairOutcome(claim.phase, f"retained local correction phase: {claim.phase}")
    assert executor is not None
    if any(identity.startswith("review-output:") for identity in request.feedback_identities):
        claimed = store.get(request)
        basis_refusal = "the retained correction attempt is unavailable" if claimed is None else review_correction_basis_refusal(claimed)
        if basis_refusal:
            return LocalReviewRepairOutcome("not_admitted", f"reviewer correction refused: {basis_refusal}")

    _ensure_head_commit_available(repo_root, request)
    worktree = tempfile.mkdtemp(prefix=f"auto_coder_review_{request.pr_number}_")
    preserve_worktree = False
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", worktree, request.head_sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if added.returncode != 0:
            store.transition(request, claim, "not_started", reason=added.stderr.strip())
            return LocalReviewRepairOutcome("not_started", f"protected checkout failed: {added.stderr.strip()}")
        store.transition(request, claim, "executing", workspace_path=worktree)
        if provider_entry_authorizer is not None:
            try:
                authorized = provider_entry_authorizer()
            except Exception as exc:
                reason = f"provider-entry authority is unavailable after preparation: {exc}"
                store.transition(request, claim, "not_started", reason=reason)
                return LocalReviewRepairOutcome("not_started", reason)
            if not authorized:
                store.transition(request, claim, "not_started", reason="provider-entry authority changed during preparation")
                return LocalReviewRepairOutcome("not_started", "provider-entry authority changed during preparation")
        if provider_entry_checkpoint is not None and not provider_entry_checkpoint():
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", "durable provider-entry checkpoint failed")
        if not store.mark_invocation_entered(request, claim, local_job_id=local_job_id):
            preserve_worktree = True
            return LocalReviewRepairOutcome("deferred", "local execution ownership changed before backend entry")
        if allowance_authority is not None:
            try:
                allowance_authority.mark_invocation()
            except Exception as exc:
                store.transition(request, claim, "not_started", reason=str(exc))
                return LocalReviewRepairOutcome("not_started", f"local invocation was not admitted: {exc}")
        try:
            response = executor(request, worktree)
        except Exception as exc:
            store.transition(request, claim, "indeterminate", reason=str(exc), workspace_path=worktree)
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", f"local backend outcome is indeterminate: {exc}", executed=True)

        local_guard = assess_local_pr_automation(request.repository, request.pr_number, Path(worktree), request.head_sha)
        if local_guard is not None:
            store.transition(request, claim, "indeterminate", reason=local_guard.reason, workspace_path=worktree)
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", local_guard.reason, executed=True)

        status = subprocess.run(["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True)
        if status.returncode != 0:
            store.transition(request, claim, "indeterminate", reason=status.stderr.strip(), workspace_path=worktree)
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", "could not inspect corrective output", executed=True)
        if response.strip() == "CANNOT_FIX":
            store.transition(request, claim, "terminal_failure", reason="local backend returned CANNOT_FIX")
            if allowance_authority is not None:
                allowance_authority.mark_completion(code_changed=False, evidence="local backend returned CANNOT_FIX")
            return LocalReviewRepairOutcome("terminal_failure", "local backend could not correct the finding", executed=True)
        if not status.stdout.strip():
            store.transition(request, claim, "completed_no_change", result_sha=request.head_sha)
            if allowance_authority is not None:
                allowance_authority.mark_completion(code_changed=False, evidence="local correction completed with no change")
            return LocalReviewRepairOutcome("completed_no_change", "local correction completed with no change; independent validation is required", executed=True)

        if publication_authorizer is not None:
            try:
                authorized = publication_authorizer()
            except Exception as exc:
                authorized = False
                reason = f"publication authority is unavailable: {exc}"
            else:
                reason = "publication authority changed before commit"
            if not authorized:
                store.transition(request, claim, "indeterminate", reason=reason, workspace_path=worktree)
                preserve_worktree = True
                return LocalReviewRepairOutcome("indeterminate", reason, executed=True)
        stage = subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True, text=True)
        if stage.returncode != 0:
            store.transition(request, claim, "indeterminate", reason=stage.stderr, workspace_path=worktree)
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", f"corrective output could not be staged: {stage.stderr}", executed=True)
        commit_message = f"Fix unresolved review findings for PR #{request.pr_number}"
        commit_env = None
        if commit_identity_provider is not None:
            from .review_publication_recovery import review_commit_message, verified_commit_identity

            commit_message = review_commit_message(request)
            try:
                identity = verified_commit_identity(request, commit_identity_provider)
                commit_env = identity.env()
            except Exception as exc:
                from .review_supersession import capture_pre_push_evidence

                reason = f"Controller Git identity unavailable: {exc}"
                try:
                    retained = store.get(request)
                    if retained is not None:
                        evidence = capture_pre_push_evidence(request, retained, repo_root)
                        store.checkpoint_pre_push(request, claim, evidence)
                except Exception as evidence_error:
                    logger.warning("PR #{} pre-push evidence unavailable: {}", request.pr_number, evidence_error)
                store.transition(request, claim, "indeterminate", reason=reason, workspace_path=worktree)
                preserve_worktree = True
                return LocalReviewRepairOutcome("indeterminate", reason, executed=True)
        if any(identity.startswith("review-output:") for identity in request.feedback_identities):
            retained = store.get(request)
            basis_refusal = "the retained correction attempt is unavailable" if retained is None else review_correction_basis_refusal(retained)
            if basis_refusal:
                reason = f"reviewer correction commit refused: {basis_refusal}"
                store.transition(request, claim, "indeterminate", reason=reason, workspace_path=worktree)
                preserve_worktree = True
                return LocalReviewRepairOutcome("indeterminate", reason, executed=True)
        if commit_env is not None:
            commit = git_commit_with_retry(commit_message, cwd=worktree, allow_repair=False, commit_env=commit_env)
        else:
            commit = git_commit_with_retry(commit_message, cwd=worktree) if allow_commit_repair else git_commit_with_retry(commit_message, cwd=worktree, allow_repair=False)
        if not commit.success:
            if not allow_commit_repair and all(identity.startswith("review-output:") for identity in request.feedback_identities):
                # The captured-output copier has returned and Git staging has
                # completed. Retain structured evidence only when the failed
                # commit left this same owned worktree at its original head.
                from .review_supersession import capture_pre_push_evidence

                try:
                    retained = store.get(request)
                    if retained is not None:
                        evidence = capture_pre_push_evidence(request, retained, repo_root)
                        if not store.checkpoint_pre_push(request, claim, evidence):
                            logger.warning("PR #{} pre-push evidence checkpoint lost ownership", request.pr_number)
                except Exception as exc:
                    logger.warning("PR #{} pre-push evidence unavailable: {}", request.pr_number, exc)
            store.transition(request, claim, "indeterminate", reason=commit.stderr, workspace_path=worktree)
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", f"corrective commit failed after execution: {commit.stderr}", executed=True)
        sha_result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True, text=True)
        result_sha = sha_result.stdout.strip()
        if not store.transition(request, claim, "publication_pending", result_sha=result_sha):
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", "publication checkpoint ownership changed; committed output was not pushed", executed=True)
        if publication_authorizer is not None:
            try:
                authorized = publication_authorizer()
            except Exception as exc:
                return LocalReviewRepairOutcome("publication_pending", f"publication authority is unavailable: {exc}", executed=True)
            if not authorized:
                return LocalReviewRepairOutcome("publication_pending", "publication authority changed; retained output was not pushed", executed=True)
        pending = store.get(request)
        if pending is None:
            return LocalReviewRepairOutcome("publication_pending", "candidate checkpoint is unavailable; no push was started", executed=True)
        outcome = _send_pending_publication(request, store, pending, Path(worktree), publication_authorizer=publication_authorizer)
        if outcome.published and allowance_authority is not None:
            allowance_authority.mark_completion(code_changed=True, evidence=f"local correction published as {result_sha}")
        return outcome
    finally:
        if not preserve_worktree:
            subprocess.run(["git", "worktree", "remove", "--force", worktree], cwd=repo_root, capture_output=True)
            try:
                os.rmdir(worktree)
            except OSError:
                pass


def recover_confirmed_local_review_repair(
    request: LocalReviewRepairRequest,
    response: str,
    *,
    store: LocalReviewRepairStore,
    allowance_authority: LocalRepairAllowanceAuthority,
    publication_authorizer: Optional[Callable[[], bool]] = None,
) -> LocalReviewRepairOutcome:
    """Finish an entered correction from its settled Muse result and worktree."""
    if publication_authorizer is not None:
        try:
            if not publication_authorizer():
                return LocalReviewRepairOutcome("publication_pending", "PR automation is stopped or its authority is unavailable; retained output was not committed", executed=True)
        except Exception as exc:
            return LocalReviewRepairOutcome("publication_pending", f"publication authority unavailable: {exc}", executed=True)
    record = store.get(request)
    if record is None or record.phase != "executing" or not record.invocation_entered or not record.workspace_path:
        return LocalReviewRepairOutcome("indeterminate", "retained correction execution is unavailable")
    worktree = Path(record.workspace_path)
    if not worktree.is_dir():
        return LocalReviewRepairOutcome("indeterminate", "retained correction workspace is unavailable")
    local_guard = assess_local_pr_automation(request.repository, request.pr_number, worktree, record.head_sha)
    if local_guard is not None:
        return LocalReviewRepairOutcome("publication_pending", local_guard.reason, executed=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return LocalReviewRepairOutcome("indeterminate", "could not inspect retained corrective output")
    claim = LocalReviewRepairClaim(True, record.phase, record.attempt_id, record.incarnation)
    if response.strip() == "CANNOT_FIX":
        store.transition(request, claim, "terminal_failure", reason="local backend returned CANNOT_FIX")
        allowance_authority.mark_completion(code_changed=False, evidence="local backend returned CANNOT_FIX")
        return LocalReviewRepairOutcome("terminal_failure", "local backend could not correct the finding", executed=True)
    if not status.stdout.strip():
        store.transition(request, claim, "completed_no_change", result_sha=request.head_sha)
        allowance_authority.mark_completion(code_changed=False, evidence="local correction completed with no change")
        return LocalReviewRepairOutcome(
            "completed_no_change",
            "local correction completed with no change; independent validation is required",
            executed=True,
        )
    basis_refusal = review_correction_basis_refusal(record)
    if basis_refusal:
        return LocalReviewRepairOutcome("indeterminate", f"reviewer correction commit refused: {basis_refusal}", executed=True)
    stage = subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True, text=True)
    if stage.returncode != 0:
        return LocalReviewRepairOutcome("indeterminate", f"corrective output could not be staged: {stage.stderr}")
    commit = git_commit_with_retry(f"Fix unresolved review findings for PR #{request.pr_number}", cwd=str(worktree))
    if not commit.success:
        return LocalReviewRepairOutcome("indeterminate", f"corrective commit failed after recovery: {commit.stderr}")
    result_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not store.transition(request, claim, "publication_pending", result_sha=result_sha):
        return LocalReviewRepairOutcome("publication_pending", "candidate checkpoint lost ownership; no push was started", executed=True)
    pending = store.get(request)
    if pending is None:
        return LocalReviewRepairOutcome("publication_pending", "candidate checkpoint is unavailable; no push was started", executed=True)
    outcome = _send_pending_publication(request, store, pending, worktree, publication_authorizer=publication_authorizer)
    if outcome.published:
        allowance_authority.mark_completion(code_changed=True, evidence=f"local correction published as {result_sha}")
    return outcome
