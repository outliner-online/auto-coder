"""Durable state for one PR's approval-then-merge mutation sequence.

This models merge operations as two independent durable effects (approval,
merge) with per-attempt ownership and correlated delivery receipts, so that a
restart, a duplicate notification, or a stale in-flight result can never
re-post an approval or re-merge, or silently discard evidence that a
mutation already happened. It does not talk to GitHub itself; production
adapters and the scheduler own interpreting real responses and connecting
this to normal PR processing (see issue #1937 for the full contract).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .github_pending_work import PendingWorkOwnershipError, repository_ownership_key
from .logger_config import get_logger

logger = get_logger(__name__)

_LOCK = threading.Lock()
_DEFAULT_STORE: MergeOperationStore | None = None

# Mirrors github_pending_work.MIN_LOCAL_RETRY_INTERVAL_SECONDS: a local
# (pre-send) deferral must never permit an immediate re-attempt.
MIN_LOCAL_RETRY_INTERVAL_SECONDS = 1.0
# One real throttle response plus this many further automatic attempts are
# tolerated before the effect becomes an operational block (REQ-006).
MAX_THROTTLED_RETRIES_AFTER_FIRST = 3


def owns_merge_operation(repository: str, identity: MergeOperationIdentity) -> bool:
    """Compare repository ownership without rewriting the retained identity."""
    owner = repository_ownership_key(repository)
    try:
        return owner == repository_ownership_key(identity.repository)
    except PendingWorkOwnershipError:
        return False


class OperationStatus(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    OPERATIONALLY_BLOCKED = "operationally_blocked"
    SUPERSEDED = "superseded"
    MERGE_CONFIRMED = "merge_confirmed"


class EffectName(str, Enum):
    APPROVAL = "approval"
    MERGE = "merge"


class EffectState(str, Enum):
    NOT_NEEDED = "not_needed"
    NOT_ATTEMPTED = "not_attempted"
    RUNNING = "running"
    CONFIRMED_UNSENT = "confirmed_unsent"
    CONFIRMED_REJECTED = "confirmed_rejected"
    CONFIRMED_COMPLETE = "confirmed_complete"
    DELIVERY_UNKNOWN = "delivery_unknown"


class ConfirmationSource(str, Enum):
    """How a completed effect was confirmed: our own mutation's success
    response, or a later read of authoritative current state."""

    OWN_RESPONSE = "own_response"
    STATE_OBSERVATION = "state_observation"


class BlockReason(str, Enum):
    AUTHENTICATION = "authentication_failure"
    FORBIDDEN = "forbidden"
    RETRIES_EXHAUSTED = "throttle_retries_exhausted"


class ReservationRefusal(str, Enum):
    """Machine-readable reasons why an App-bound reservation was refused."""

    INELIGIBLE = "ineligible"
    BINDING_REQUIRED = "binding_required"
    INVALID_BINDING = "invalid_binding"
    PRINCIPAL_MISMATCH = "principal_mismatch"


@dataclass(frozen=True)
class EffectSenderBinding:
    """Verified, nonsecret identity of the App principal sending an effect."""

    app_role: str
    api_origin: str
    repository: str
    app_id: int
    installation_id: int
    bot_login: str

    def valid_for(self, identity: MergeOperationIdentity, effect_name: EffectName) -> bool:
        required_role = {EffectName.APPROVAL: "reviewer", EffectName.MERGE: "controller"}.get(effect_name)
        return (
            isinstance(self.app_role, str)
            and self.app_role == required_role
            and isinstance(self.api_origin, str)
            and self.api_origin.rstrip("/") == identity.api_origin.rstrip("/")
            and isinstance(self.repository, str)
            and self.repository.casefold() == identity.repository.casefold()
            and type(self.app_id) is int
            and self.app_id > 0
            and type(self.installation_id) is int
            and self.installation_id > 0
            and isinstance(self.bot_login, str)
            and re.fullmatch(r"[A-Za-z0-9-]+\[bot\]", self.bot_login, re.IGNORECASE) is not None
        )

    def canonical(self) -> EffectSenderBinding:
        """Compare the same principal identically before and after persistence."""
        return EffectSenderBinding(self.app_role, self.api_origin.rstrip("/"), self.repository.casefold(), self.app_id, self.installation_id, self.bot_login.casefold())

    def to_json(self) -> str:
        return json.dumps(
            {
                "app_role": self.app_role,
                "api_origin": self.api_origin.rstrip("/"),
                "repository": self.repository,
                "app_id": self.app_id,
                "installation_id": self.installation_id,
                "bot_login": self.bot_login,
            },
            sort_keys=True,
        )

    @staticmethod
    def from_json(raw: str) -> EffectSenderBinding | None:
        if not raw:
            return None
        data = json.loads(raw)
        return EffectSenderBinding(data["app_role"], data["api_origin"], data["repository"], data["app_id"], data["installation_id"], data["bot_login"])


@dataclass(frozen=True)
class MergeOperationIdentity:
    """Target identity: API origin, repository, and PR number."""

    api_origin: str
    repository: str
    pr_number: int

    def key(self) -> str:
        return json.dumps(
            {"api_origin": self.api_origin, "repository": self.repository, "pr_number": self.pr_number},
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class EffectReceipt:
    """A confirmed effect's correlated evidence, never a raw request/response."""

    confirmation_source: ConfirmationSource
    review_id: str = ""
    reviewer_identity: str = ""
    target_head_sha: str = ""
    merge_commit_sha: str = ""
    recorded_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(
            {
                "confirmation_source": self.confirmation_source.value,
                "review_id": self.review_id,
                "reviewer_identity": self.reviewer_identity,
                "target_head_sha": self.target_head_sha,
                "merge_commit_sha": self.merge_commit_sha,
                "recorded_at": self.recorded_at,
            }
        )

    @staticmethod
    def from_json(raw: str) -> EffectReceipt | None:
        if not raw:
            return None
        data = json.loads(raw)
        return EffectReceipt(
            confirmation_source=ConfirmationSource(data["confirmation_source"]),
            review_id=data.get("review_id", ""),
            reviewer_identity=data.get("reviewer_identity", ""),
            target_head_sha=data.get("target_head_sha", ""),
            merge_commit_sha=data.get("merge_commit_sha", ""),
            recorded_at=data.get("recorded_at", 0.0),
        )


@dataclass(frozen=True)
class EffectRecord:
    name: EffectName
    state: EffectState = EffectState.NOT_ATTEMPTED
    attempt_id: str = ""
    generation: int = 0
    receipt: EffectReceipt | None = None
    throttle_attempts: int = 0
    throttled_attempt_ids: tuple[str, ...] = field(default_factory=tuple)
    last_error: str = ""
    sender_binding: EffectSenderBinding | None = None


@dataclass(frozen=True)
class MergeOperation:
    identity: MergeOperationIdentity
    expected_head_sha: str
    merge_method: str
    approval_credential_role: str
    reviewer_identity: str
    generation: int
    status: OperationStatus
    resume_reason: str
    not_before: float
    effects: dict[EffectName, EffectRecord] = field(default_factory=dict)

    def effect(self, name: EffectName) -> EffectRecord:
        return self.effects.get(name, EffectRecord(name))


@dataclass(frozen=True)
class ReservationResult:
    """Outcome of trying to acquire exclusive execution rights for one effect."""

    granted: bool
    operation: MergeOperation
    attempt_id: str = ""
    generation: int = 0
    refusal: ReservationRefusal | None = None


@dataclass(frozen=True)
class EffectAttemptRecord:
    effect_name: EffectName
    generation: int
    attempt_id: str
    sender_binding: EffectSenderBinding | None
    receipt: EffectReceipt | None
    outcome: str
    expected_head_sha: str = ""


class MergeOperationPersistenceError(RuntimeError):
    """Persistence uncertainty; callers must not treat this as success (fail closed)."""


def default_merge_operation_state_path() -> Path:
    return Path.home() / ".auto-coder" / "merge_operation_state.db"


_RETRYABLE_EFFECT_STATES = {EffectState.NOT_ATTEMPTED, EffectState.CONFIRMED_UNSENT}
_TERMINAL_OPERATION_STATUSES = {OperationStatus.SUPERSEDED, OperationStatus.MERGE_CONFIRMED}


class MergeOperationStore:
    """SQLite-backed durable state for approval/merge effects of one PR."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_merge_operation_state_path()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            # DDL is not implicitly transactional in sqlite3's legacy mode.
            # Serialize schema inspection, upgrades and evidence backfill.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS merge_operations (
                op_key TEXT PRIMARY KEY, api_origin TEXT NOT NULL, repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL, expected_head_sha TEXT NOT NULL,
                merge_method TEXT NOT NULL, approval_credential_role TEXT NOT NULL,
                reviewer_identity TEXT NOT NULL, generation INTEGER NOT NULL,
                status TEXT NOT NULL, resume_reason TEXT NOT NULL,
                not_before REAL NOT NULL, updated_at REAL NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS merge_operation_effects (
                op_key TEXT NOT NULL, effect_name TEXT NOT NULL, state TEXT NOT NULL,
                attempt_id TEXT NOT NULL, generation INTEGER NOT NULL,
                receipt_json TEXT NOT NULL, throttle_attempts INTEGER NOT NULL,
                throttled_attempt_ids TEXT NOT NULL, last_error TEXT NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY (op_key, effect_name))""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(merge_operation_effects)")}
            if "sender_binding_json" not in columns:
                connection.execute("ALTER TABLE merge_operation_effects ADD COLUMN sender_binding_json TEXT NOT NULL DEFAULT ''")
            connection.execute("""CREATE TABLE IF NOT EXISTS merge_operation_attempt_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT, op_key TEXT NOT NULL,
                effect_name TEXT NOT NULL, generation INTEGER NOT NULL, attempt_id TEXT NOT NULL,
                outcome TEXT NOT NULL, detail TEXT NOT NULL, recorded_at REAL NOT NULL)""")
            attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(merge_operation_effect_attempts)")}
            connection.execute("""CREATE TABLE IF NOT EXISTS merge_operation_effect_attempts (
                op_key TEXT NOT NULL, effect_name TEXT NOT NULL, generation INTEGER NOT NULL,
                attempt_id TEXT NOT NULL, sender_binding_json TEXT NOT NULL,
                receipt_json TEXT NOT NULL, outcome TEXT NOT NULL, updated_at REAL NOT NULL,
                expected_head_sha TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (op_key, effect_name, generation, attempt_id))""")
            if attempt_columns and "expected_head_sha" not in attempt_columns:
                connection.execute("ALTER TABLE merge_operation_effect_attempts ADD COLUMN expected_head_sha TEXT NOT NULL DEFAULT ''")
            if "expected_head_sha" not in attempt_columns:
                self._backfill_attempts(connection)
            connection.commit()
            return connection
        except BaseException:
            connection.rollback()
            connection.close()
            raise

    def _backfill_attempts(self, connection: sqlite3.Connection) -> None:
        """Retain legacy evidence without inferring a historical App sender."""
        connection.execute("""INSERT OR IGNORE INTO merge_operation_effect_attempts
            (op_key,effect_name,generation,attempt_id,sender_binding_json,receipt_json,outcome,updated_at,expected_head_sha)
            SELECT e.op_key,e.effect_name,e.generation,e.attempt_id,e.sender_binding_json,
                e.receipt_json,e.state,e.updated_at,
                CASE WHEN e.generation=o.generation THEN o.expected_head_sha ELSE '' END
            FROM merge_operation_effects e JOIN merge_operations o ON o.op_key=e.op_key
            WHERE e.attempt_id != ''""")
        connection.execute("""INSERT OR IGNORE INTO merge_operation_effect_attempts
            (op_key,effect_name,generation,attempt_id,sender_binding_json,receipt_json,outcome,updated_at,expected_head_sha)
            SELECT h.op_key,h.effect_name,h.generation,h.attempt_id,'','',h.outcome,h.recorded_at,
                CASE WHEN h.generation=o.generation THEN o.expected_head_sha ELSE '' END
            FROM merge_operation_attempt_history h JOIN merge_operations o ON o.op_key=h.op_key
            WHERE h.attempt_id != '' AND h.history_id=(
                SELECT MAX(latest.history_id) FROM merge_operation_attempt_history latest
                WHERE latest.op_key=h.op_key AND latest.effect_name=h.effect_name
                    AND latest.generation=h.generation AND latest.attempt_id=h.attempt_id)""")
        # The short-lived pre-head-binding schema can still have authoritative
        # current-generation heads or exact receipt targets. Unknown older heads
        # stay unknown rather than borrowing a newer generation's target.
        connection.execute("""UPDATE merge_operation_effect_attempts
            SET expected_head_sha=COALESCE((SELECT expected_head_sha FROM merge_operations o
                WHERE o.op_key=merge_operation_effect_attempts.op_key
                    AND o.generation=merge_operation_effect_attempts.generation),'')
            WHERE expected_head_sha=''""")
        rows = connection.execute("SELECT op_key,effect_name,generation,attempt_id,receipt_json FROM merge_operation_effect_attempts WHERE expected_head_sha='' AND receipt_json!=''").fetchall()
        for op_key, effect_name, generation, attempt_id, raw_receipt in rows:
            receipt = EffectReceipt.from_json(raw_receipt)
            if receipt is not None and receipt.target_head_sha:
                connection.execute(
                    "UPDATE merge_operation_effect_attempts SET expected_head_sha=? WHERE op_key=? AND effect_name=? AND generation=? AND attempt_id=?",
                    (receipt.target_head_sha, op_key, effect_name, generation, attempt_id),
                )

    # -- row <-> dataclass -------------------------------------------------

    def _load_operation(self, connection: sqlite3.Connection, identity: MergeOperationIdentity) -> MergeOperation | None:
        row = connection.execute(
            "SELECT expected_head_sha, merge_method, approval_credential_role, reviewer_identity, " "generation, status, resume_reason, not_before FROM merge_operations WHERE op_key=?",
            (identity.key(),),
        ).fetchone()
        if row is None:
            return None
        effects: dict[EffectName, EffectRecord] = {}
        for erow in connection.execute(
            "SELECT effect_name, state, attempt_id, generation, receipt_json, throttle_attempts, " "throttled_attempt_ids, last_error, sender_binding_json FROM merge_operation_effects WHERE op_key=?",
            (identity.key(),),
        ).fetchall():
            name = EffectName(erow[0])
            effects[name] = EffectRecord(
                name=name,
                state=EffectState(erow[1]),
                attempt_id=erow[2],
                generation=erow[3],
                receipt=EffectReceipt.from_json(erow[4]),
                throttle_attempts=erow[5],
                throttled_attempt_ids=tuple(json.loads(erow[6])),
                last_error=erow[7],
                sender_binding=EffectSenderBinding.from_json(erow[8]),
            )
        return MergeOperation(
            identity=identity,
            expected_head_sha=row[0],
            merge_method=row[1],
            approval_credential_role=row[2],
            reviewer_identity=row[3],
            generation=row[4],
            status=OperationStatus(row[5]),
            resume_reason=row[6],
            not_before=row[7],
            effects=effects,
        )

    def _save_operation(self, connection: sqlite3.Connection, operation: MergeOperation, now: float) -> None:
        key = operation.identity.key()
        connection.execute(
            "INSERT OR REPLACE INTO merge_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                operation.identity.api_origin,
                operation.identity.repository,
                operation.identity.pr_number,
                operation.expected_head_sha,
                operation.merge_method,
                operation.approval_credential_role,
                operation.reviewer_identity,
                operation.generation,
                operation.status.value,
                operation.resume_reason,
                operation.not_before,
                now,
            ),
        )
        for name, effect in operation.effects.items():
            connection.execute(
                "INSERT OR REPLACE INTO merge_operation_effects " "(op_key,effect_name,state,attempt_id,generation,receipt_json,throttle_attempts,throttled_attempt_ids,last_error,updated_at,sender_binding_json) " "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    name.value,
                    effect.state.value,
                    effect.attempt_id,
                    effect.generation,
                    effect.receipt.to_json() if effect.receipt is not None else "",
                    effect.throttle_attempts,
                    json.dumps(effect.throttled_attempt_ids),
                    effect.last_error,
                    now,
                    effect.sender_binding.to_json() if effect.sender_binding is not None else "",
                ),
            )

    def _save_attempt(
        self,
        connection: sqlite3.Connection,
        op_key: str,
        effect: EffectAttemptRecord,
        now: float,
    ) -> None:
        connection.execute(
            "INSERT INTO merge_operation_effect_attempts "
            "(op_key,effect_name,generation,attempt_id,sender_binding_json,receipt_json,outcome,updated_at,expected_head_sha) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(op_key,effect_name,generation,attempt_id) DO UPDATE SET "
            "receipt_json=CASE WHEN merge_operation_effect_attempts.receipt_json='' THEN excluded.receipt_json ELSE merge_operation_effect_attempts.receipt_json END, "
            "outcome=CASE WHEN merge_operation_effect_attempts.receipt_json='' THEN excluded.outcome ELSE merge_operation_effect_attempts.outcome END, "
            "updated_at=excluded.updated_at",
            (
                op_key,
                effect.effect_name.value,
                effect.generation,
                effect.attempt_id,
                effect.sender_binding.to_json() if effect.sender_binding is not None else "",
                effect.receipt.to_json() if effect.receipt is not None else "",
                effect.outcome,
                now,
                effect.expected_head_sha,
            ),
        )

    def _append_history(
        self,
        connection: sqlite3.Connection,
        op_key: str,
        effect_name: EffectName,
        generation: int,
        attempt_id: str,
        outcome: str,
        detail: str,
        now: float,
    ) -> None:
        connection.execute(
            "INSERT INTO merge_operation_attempt_history " "(op_key, effect_name, generation, attempt_id, outcome, detail, recorded_at) VALUES (?,?,?,?,?,?,?)",
            (op_key, effect_name.value, generation, attempt_id, outcome, detail, now),
        )
        connection.execute(
            "UPDATE merge_operation_effect_attempts SET outcome=?, updated_at=? " "WHERE op_key=? AND effect_name=? AND generation=? AND attempt_id=? AND receipt_json=''",
            (outcome, now, op_key, effect_name.value, generation, attempt_id),
        )

    # -- public API ---------------------------------------------------------

    def get(self, identity: MergeOperationIdentity) -> MergeOperation | None:
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN")
                return self._load_operation(connection, identity)
        except Exception as exc:
            logger.error("Could not read merge operation {}: {}", identity.key(), exc)
            raise MergeOperationPersistenceError("Merge operation could not be read") from exc

    def all_operations(self) -> list[MergeOperation]:
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN")
                rows = connection.execute("SELECT api_origin, repository, pr_number FROM merge_operations").fetchall()
                return [op for row in rows if (op := self._load_operation(connection, MergeOperationIdentity(row[0], row[1], row[2]))) is not None]
        except Exception as exc:
            logger.error("Could not list merge operations: {}", exc)
            raise MergeOperationPersistenceError("Merge operations could not be read") from exc

    def _load_attempt(
        self,
        connection: sqlite3.Connection,
        identity: MergeOperationIdentity,
        effect_name: EffectName,
        generation: int,
        attempt_id: str,
    ) -> EffectAttemptRecord | None:
        row = connection.execute(
            "SELECT sender_binding_json, receipt_json, outcome, expected_head_sha FROM merge_operation_effect_attempts " "WHERE op_key=? AND effect_name=? AND generation=? AND attempt_id=?",
            (identity.key(), effect_name.value, generation, attempt_id),
        ).fetchone()
        if row is None:
            return None
        return EffectAttemptRecord(effect_name, generation, attempt_id, EffectSenderBinding.from_json(row[0]), EffectReceipt.from_json(row[1]), row[2], row[3])

    def get_attempt(
        self,
        identity: MergeOperationIdentity,
        effect_name: EffectName,
        generation: int,
        attempt_id: str,
    ) -> EffectAttemptRecord | None:
        """Read durable evidence for an exact historical or current attempt."""
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN")
                return self._load_attempt(connection, identity, effect_name, generation, attempt_id)
        except Exception as exc:
            raise MergeOperationPersistenceError("Merge operation attempt could not be read") from exc

    def get_or_create(
        self,
        identity: MergeOperationIdentity,
        *,
        expected_head_sha: str,
        merge_method: str,
        approval_credential_role: str,
        reviewer_identity: str,
        needs_approval: bool,
        now: float | None = None,
    ) -> MergeOperation:
        """Fetch (creating if absent) the operation for this target.

        A same-head re-entry (duplicate notification, a different entry
        point, a restart, or a mere metadata refresh such as a merge-method
        change) never resets effects, deadlines, generation, or receipts. A
        genuinely new expected head bumps the generation and returns any
        currently-retryable (not confirmed-complete/blocked) effect to
        ``NOT_ATTEMPTED`` while leaving prior-generation history untouched.
        """
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = self._load_operation(connection, identity)
                if existing is None:
                    effects = {
                        EffectName.APPROVAL: EffectRecord(EffectName.APPROVAL, EffectState.NOT_NEEDED if not needs_approval else EffectState.NOT_ATTEMPTED),
                        EffectName.MERGE: EffectRecord(EffectName.MERGE, EffectState.NOT_ATTEMPTED),
                    }
                    operation = MergeOperation(
                        identity=identity,
                        expected_head_sha=expected_head_sha,
                        merge_method=merge_method,
                        approval_credential_role=approval_credential_role,
                        reviewer_identity=reviewer_identity,
                        generation=1,
                        status=OperationStatus.WAITING,
                        resume_reason="",
                        not_before=current_time,
                        effects=effects,
                    )
                    self._save_operation(connection, operation, current_time)
                    return operation
                if existing.expected_head_sha == expected_head_sha:
                    # Same target: refresh only caller-supplied metadata,
                    # never effects/generation/deadline/status.
                    refreshed = MergeOperation(
                        identity=identity,
                        expected_head_sha=existing.expected_head_sha,
                        merge_method=merge_method,
                        approval_credential_role=approval_credential_role,
                        reviewer_identity=reviewer_identity,
                        generation=existing.generation,
                        status=existing.status,
                        resume_reason=existing.resume_reason,
                        not_before=existing.not_before,
                        effects=existing.effects,
                    )
                    self._save_operation(connection, refreshed, current_time)
                    return refreshed
                # A new expected head invalidates current execution
                # permission for retryable effects but never discards
                # receipts or history already recorded under the old
                # generation.
                new_generation = existing.generation + 1
                new_effects: dict[EffectName, EffectRecord] = {}
                for name, prior in existing.effects.items():
                    if prior.state in (EffectState.CONFIRMED_COMPLETE, EffectState.NOT_NEEDED):
                        new_effects[name] = prior
                        continue
                    new_effects[name] = EffectRecord(name, EffectState.NOT_ATTEMPTED)
                operation = MergeOperation(
                    identity=identity,
                    expected_head_sha=expected_head_sha,
                    merge_method=merge_method,
                    approval_credential_role=approval_credential_role,
                    reviewer_identity=reviewer_identity,
                    generation=new_generation,
                    status=OperationStatus.WAITING,
                    resume_reason="",
                    not_before=current_time,
                    effects=new_effects,
                )
                self._save_operation(connection, operation, current_time)
                return operation
        except Exception as exc:
            logger.error("Could not persist merge operation {}: {}", identity.key(), exc)
            raise MergeOperationPersistenceError("Merge operation could not be persisted") from exc

    def reserve_app_attempt(
        self,
        identity: MergeOperationIdentity,
        effect_name: EffectName,
        sender_binding: EffectSenderBinding | None,
        *,
        now: float | None = None,
    ) -> ReservationResult:
        """Reserve an App attempt, atomically persisting its verified sender."""
        return self.reserve_attempt(identity, effect_name, now=now, sender_binding=sender_binding, require_sender_binding=True)

    def reserve_attempt(
        self,
        identity: MergeOperationIdentity,
        effect_name: EffectName,
        *,
        now: float | None = None,
        sender_binding: EffectSenderBinding | None = None,
        require_sender_binding: bool = False,
    ) -> ReservationResult:
        """Acquire exclusive execution rights for one effect before its mutation call.

        Refused when the effect is already running (another entry point/
        thread owns it), already confirmed complete, or currently blocked
        (``DELIVERY_UNKNOWN`` awaiting reconciliation, ``CONFIRMED_REJECTED``,
        or the operation is operationally blocked/superseded/merge-confirmed).
        """
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                # Serialize the eligibility read with both the binding and
                # reservation writes, including across controller processes.
                connection.execute("BEGIN IMMEDIATE")
                operation = self._load_operation(connection, identity)
                if operation is None:
                    raise MergeOperationPersistenceError(f"No merge operation exists for {identity.key()}; call get_or_create first")
                effect = operation.effect(effect_name)
                if require_sender_binding and sender_binding is None:
                    return ReservationResult(False, operation, refusal=ReservationRefusal.BINDING_REQUIRED)
                if sender_binding is not None and not sender_binding.valid_for(identity, effect_name):
                    return ReservationResult(False, operation, refusal=ReservationRefusal.INVALID_BINDING)
                if sender_binding is not None:
                    sender_binding = sender_binding.canonical()
                if effect.sender_binding is not None and (sender_binding is None or sender_binding != effect.sender_binding.canonical()):
                    return ReservationResult(False, operation, refusal=ReservationRefusal.PRINCIPAL_MISMATCH)
                if operation.status in _TERMINAL_OPERATION_STATUSES or operation.status is OperationStatus.OPERATIONALLY_BLOCKED:
                    return ReservationResult(False, operation, refusal=ReservationRefusal.INELIGIBLE)
                if effect.state not in _RETRYABLE_EFFECT_STATES:
                    return ReservationResult(False, operation, refusal=ReservationRefusal.INELIGIBLE)
                attempt_id = uuid.uuid4().hex
                authoritative_binding = effect.sender_binding or sender_binding
                updated_effect = EffectRecord(
                    effect_name,
                    EffectState.RUNNING,
                    attempt_id=attempt_id,
                    generation=operation.generation,
                    receipt=effect.receipt,
                    throttle_attempts=effect.throttle_attempts,
                    throttled_attempt_ids=effect.throttled_attempt_ids,
                    last_error=effect.last_error,
                    sender_binding=authoritative_binding,
                )
                new_effects = dict(operation.effects)
                new_effects[effect_name] = updated_effect
                operation = MergeOperation(
                    identity=operation.identity,
                    expected_head_sha=operation.expected_head_sha,
                    merge_method=operation.merge_method,
                    approval_credential_role=operation.approval_credential_role,
                    reviewer_identity=operation.reviewer_identity,
                    generation=operation.generation,
                    status=OperationStatus.RUNNING,
                    resume_reason=operation.resume_reason,
                    not_before=operation.not_before,
                    effects=new_effects,
                )
                self._save_operation(connection, operation, current_time)
                self._save_attempt(
                    connection,
                    identity.key(),
                    EffectAttemptRecord(effect_name, operation.generation, attempt_id, authoritative_binding, None, "reserved", operation.expected_head_sha),
                    current_time,
                )
                self._append_history(connection, identity.key(), effect_name, operation.generation, attempt_id, "reserved", "", current_time)
                return ReservationResult(True, operation, attempt_id, operation.generation)
        except MergeOperationPersistenceError:
            raise
        except Exception as exc:
            logger.error("Could not reserve merge-operation attempt {}/{}: {}", identity.key(), effect_name.value, exc)
            raise MergeOperationPersistenceError("Merge operation attempt could not be reserved") from exc

    def _owns_attempt(self, operation: MergeOperation, effect_name: EffectName, attempt_id: str, generation: int) -> bool:
        effect = operation.effect(effect_name)
        return bool(attempt_id) and operation.generation == generation and effect.attempt_id == attempt_id and effect.generation == generation

    def _both_effects_settled(self, operation: MergeOperation) -> bool:
        approval = operation.effect(EffectName.APPROVAL)
        merge = operation.effect(EffectName.MERGE)
        approval_done = approval.state in (EffectState.NOT_NEEDED, EffectState.CONFIRMED_COMPLETE)
        return approval_done and merge.state is EffectState.CONFIRMED_COMPLETE

    @staticmethod
    def _valid_receipt(effect_name: EffectName, attempt: EffectAttemptRecord | None, receipt: EffectReceipt | None) -> bool:
        if attempt is None or not isinstance(receipt, EffectReceipt) or not isinstance(receipt.confirmation_source, ConfirmationSource):
            return False
        if receipt.target_head_sha and receipt.target_head_sha != attempt.expected_head_sha:
            return False
        if receipt.confirmation_source is ConfirmationSource.STATE_OBSERVATION or attempt.sender_binding is not None:
            if not attempt.expected_head_sha or receipt.target_head_sha != attempt.expected_head_sha:
                return False
            if effect_name is EffectName.APPROVAL:
                return bool(receipt.review_id and receipt.reviewer_identity)
            return bool(receipt.merge_commit_sha)
        # The existing personal response contract permits compact evidence;
        # correlation still comes from the reserved attempt and generation.
        return True

    def _finish_attempt(
        self,
        identity: MergeOperationIdentity,
        effect_name: EffectName,
        attempt_id: str,
        generation: int,
        outcome_label: str,
        detail: str,
        new_state: EffectState,
        receipt: EffectReceipt | None,
        now: float | None,
        *,
        new_operation_status: OperationStatus | None = None,
    ) -> bool:
        """Apply a terminal (non-error) transition for one attempt.

        Stale results (an attempt whose ``(attempt_id, generation)`` no
        longer matches the effect's current owner, e.g. because a new head
        invalidated it) are recorded only to history and never mutate
        current state, deadlines, or receipts (REQ-004).
        """
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                operation = self._load_operation(connection, identity)
                if operation is None:
                    raise MergeOperationPersistenceError(f"No merge operation exists for {identity.key()}")
                attempt = self._load_attempt(connection, identity, effect_name, generation, attempt_id)
                if new_state is EffectState.CONFIRMED_COMPLETE and not self._valid_receipt(effect_name, attempt, receipt):
                    return False
                if not self._owns_attempt(operation, effect_name, attempt_id, generation):
                    if attempt is not None:
                        self._save_attempt(
                            connection,
                            identity.key(),
                            EffectAttemptRecord(effect_name, generation, attempt_id, attempt.sender_binding, receipt, f"stale_{outcome_label}", attempt.expected_head_sha),
                            current_time,
                        )
                        self._append_history(connection, identity.key(), effect_name, generation, attempt_id, f"stale_{outcome_label}", detail, current_time)
                    return False
                prior_effect = operation.effect(effect_name)
                if prior_effect.state is EffectState.CONFIRMED_COMPLETE:
                    # A delayed failure cannot undo positive correlated evidence.
                    return new_state is EffectState.CONFIRMED_COMPLETE
                updated_effect = EffectRecord(
                    effect_name,
                    new_state,
                    attempt_id=attempt_id,
                    generation=generation,
                    receipt=receipt if receipt is not None else prior_effect.receipt,
                    throttle_attempts=prior_effect.throttle_attempts,
                    throttled_attempt_ids=prior_effect.throttled_attempt_ids,
                    last_error=detail if new_state is EffectState.DELIVERY_UNKNOWN else prior_effect.last_error,
                    sender_binding=prior_effect.sender_binding,
                )
                new_effects = dict(operation.effects)
                new_effects[effect_name] = updated_effect
                status = new_operation_status
                if status is None:
                    status = OperationStatus.MERGE_CONFIRMED if new_state is EffectState.CONFIRMED_COMPLETE and self._both_effects_settled_from(new_effects) else OperationStatus.WAITING
                operation = MergeOperation(
                    identity=operation.identity,
                    expected_head_sha=operation.expected_head_sha,
                    merge_method=operation.merge_method,
                    approval_credential_role=operation.approval_credential_role,
                    reviewer_identity=operation.reviewer_identity,
                    generation=operation.generation,
                    status=status,
                    resume_reason=operation.resume_reason if status is not OperationStatus.WAITING else "",
                    not_before=operation.not_before,
                    effects=new_effects,
                )
                self._save_operation(connection, operation, current_time)
                self._save_attempt(
                    connection,
                    identity.key(),
                    EffectAttemptRecord(effect_name, generation, attempt_id, prior_effect.sender_binding, updated_effect.receipt, outcome_label, operation.expected_head_sha),
                    current_time,
                )
                self._append_history(connection, identity.key(), effect_name, generation, attempt_id, outcome_label, detail, current_time)
                return True
        except MergeOperationPersistenceError:
            raise
        except Exception as exc:
            logger.error("Could not record merge-operation outcome {}/{}: {}", identity.key(), effect_name.value, exc)
            raise MergeOperationPersistenceError("Merge operation outcome could not be persisted") from exc

    def _both_effects_settled_from(self, effects: dict[EffectName, EffectRecord]) -> bool:
        approval = effects.get(EffectName.APPROVAL, EffectRecord(EffectName.APPROVAL))
        merge = effects.get(EffectName.MERGE, EffectRecord(EffectName.MERGE))
        approval_done = approval.state in (EffectState.NOT_NEEDED, EffectState.CONFIRMED_COMPLETE)
        return approval_done and merge.state is EffectState.CONFIRMED_COMPLETE

    def record_confirmed_complete(
        self,
        identity: MergeOperationIdentity,
        effect_name: EffectName,
        attempt_id: str,
        generation: int,
        receipt: EffectReceipt,
        now: float | None = None,
    ) -> bool:
        """Confirm one effect completed, with its correlated receipt (REQ-003)."""
        return self._finish_attempt(identity, effect_name, attempt_id, generation, "confirmed_complete", "", EffectState.CONFIRMED_COMPLETE, receipt, now)

    def record_confirmed_unsent(self, identity: MergeOperationIdentity, effect_name: EffectName, attempt_id: str, generation: int, detail: str = "", now: float | None = None) -> bool:
        """Confirm the mutation definitely was not sent; safe to retry (REQ-005)."""
        return self._finish_attempt(identity, effect_name, attempt_id, generation, "confirmed_unsent", detail, EffectState.CONFIRMED_UNSENT, None, now)

    def record_confirmed_rejected(self, identity: MergeOperationIdentity, effect_name: EffectName, attempt_id: str, generation: int, detail: str = "", now: float | None = None) -> bool:
        """Confirm GitHub definitively refused the mutation (not a transient failure)."""
        return self._finish_attempt(identity, effect_name, attempt_id, generation, "confirmed_rejected", detail, EffectState.CONFIRMED_REJECTED, None, now)

    def record_delivery_unknown(self, identity: MergeOperationIdentity, effect_name: EffectName, attempt_id: str, generation: int, detail: str = "", now: float | None = None) -> bool:
        """Delivery could not be confirmed either way; must not be assumed unsent (REQ-005)."""
        return self._finish_attempt(identity, effect_name, attempt_id, generation, "delivery_unknown", detail, EffectState.DELIVERY_UNKNOWN, None, now)

    def defer_local(
        self,
        identity: MergeOperationIdentity,
        effect_name: EffectName,
        attempt_id: str,
        generation: int,
        *,
        is_real_throttle: bool,
        throttle_attempt_id: str = "",
        retry_after_seconds: float = 0.0,
        governor_deadline: float | None = None,
        detail: str = "",
        now: float | None = None,
    ) -> MergeOperation | None:
        """Return an effect to waiting after a non-terminal (retryable) failure.

        Implements REQ-006's deadline floor: the new deadline is never
        earlier than ``now + MIN_LOCAL_RETRY_INTERVAL_SECONDS``,
        ``retry_after_seconds`` from now, any explicit ``governor_deadline``,
        or the previously retained deadline — whichever is latest. A real
        throttle response only spends one retry per distinct
        ``throttle_attempt_id`` (duplicate redelivery is free); a merely
        local (pre-send) deferral never spends a retry at all. Stale
        attempts (superseded by a new head) are recorded to history only.
        """
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                operation = self._load_operation(connection, identity)
                if operation is None:
                    raise MergeOperationPersistenceError(f"No merge operation exists for {identity.key()}")
                if not self._owns_attempt(operation, effect_name, attempt_id, generation):
                    self._append_history(connection, identity.key(), effect_name, generation, attempt_id, "stale_deferred", detail, current_time)
                    return None
                prior_effect = operation.effect(effect_name)
                if prior_effect.state in (EffectState.CONFIRMED_COMPLETE, EffectState.NOT_NEEDED):
                    return None
                attempts = prior_effect.throttle_attempts
                throttled_ids = prior_effect.throttled_attempt_ids
                blocked = False
                if is_real_throttle:
                    if throttle_attempt_id and throttle_attempt_id in throttled_ids:
                        pass  # duplicate redelivery of the same real throttle: no new spend
                    else:
                        attempts += 1
                        if throttle_attempt_id:
                            throttled_ids = (*throttled_ids, throttle_attempt_id)
                        if attempts > MAX_THROTTLED_RETRIES_AFTER_FIRST:
                            blocked = True
                due = max(current_time + MIN_LOCAL_RETRY_INTERVAL_SECONDS, current_time + retry_after_seconds, governor_deadline if governor_deadline is not None else 0.0, operation.not_before)
                new_effect_state = EffectState.NOT_ATTEMPTED
                updated_effect = EffectRecord(
                    effect_name,
                    new_effect_state,
                    attempt_id="",
                    generation=operation.generation,
                    receipt=prior_effect.receipt,
                    throttle_attempts=attempts,
                    throttled_attempt_ids=throttled_ids,
                    last_error=detail,
                    sender_binding=prior_effect.sender_binding,
                )
                new_effects = dict(operation.effects)
                new_effects[effect_name] = updated_effect
                operation = MergeOperation(
                    identity=operation.identity,
                    expected_head_sha=operation.expected_head_sha,
                    merge_method=operation.merge_method,
                    approval_credential_role=operation.approval_credential_role,
                    reviewer_identity=operation.reviewer_identity,
                    generation=operation.generation,
                    status=OperationStatus.OPERATIONALLY_BLOCKED if blocked else OperationStatus.WAITING,
                    resume_reason=BlockReason.RETRIES_EXHAUSTED.value if blocked else "",
                    not_before=0.0 if blocked else due,
                    effects=new_effects,
                )
                self._save_operation(connection, operation, current_time)
                self._append_history(connection, identity.key(), effect_name, generation, attempt_id, "deferred", detail, current_time)
                return operation
        except MergeOperationPersistenceError:
            raise
        except Exception as exc:
            logger.error("Could not defer merge-operation attempt {}/{}: {}", identity.key(), effect_name.value, exc)
            raise MergeOperationPersistenceError("Merge operation attempt could not be deferred") from exc

    def operationally_block(
        self,
        identity: MergeOperationIdentity,
        effect_name: EffectName,
        attempt_id: str,
        generation: int,
        reason: BlockReason,
        detail: str = "",
        now: float | None = None,
    ) -> bool:
        """Record an authentication/forbidden failure as a durable operational block."""
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                operation = self._load_operation(connection, identity)
                if operation is None:
                    raise MergeOperationPersistenceError(f"No merge operation exists for {identity.key()}")
                if not self._owns_attempt(operation, effect_name, attempt_id, generation):
                    self._append_history(connection, identity.key(), effect_name, generation, attempt_id, "stale_blocked", detail, current_time)
                    return False
                prior_effect = operation.effect(effect_name)
                if prior_effect.state in (EffectState.CONFIRMED_COMPLETE, EffectState.NOT_NEEDED):
                    return False
                updated_effect = EffectRecord(
                    effect_name,
                    EffectState.NOT_ATTEMPTED,
                    attempt_id="",
                    generation=operation.generation,
                    receipt=prior_effect.receipt,
                    throttle_attempts=prior_effect.throttle_attempts,
                    throttled_attempt_ids=prior_effect.throttled_attempt_ids,
                    last_error=detail,
                    sender_binding=prior_effect.sender_binding,
                )
                new_effects = dict(operation.effects)
                new_effects[effect_name] = updated_effect
                operation = MergeOperation(
                    identity=operation.identity,
                    expected_head_sha=operation.expected_head_sha,
                    merge_method=operation.merge_method,
                    approval_credential_role=operation.approval_credential_role,
                    reviewer_identity=operation.reviewer_identity,
                    generation=operation.generation,
                    status=OperationStatus.OPERATIONALLY_BLOCKED,
                    resume_reason=reason.value,
                    not_before=0.0,
                    effects=new_effects,
                )
                self._save_operation(connection, operation, current_time)
                self._append_history(connection, identity.key(), effect_name, generation, attempt_id, "operationally_blocked", detail, current_time)
                return True
        except MergeOperationPersistenceError:
            raise
        except Exception as exc:
            logger.error("Could not record operational block for merge operation {}/{}: {}", identity.key(), effect_name.value, exc)
            raise MergeOperationPersistenceError("Merge operation block could not be persisted") from exc

    def manual_reset_effect(self, identity: MergeOperationIdentity, effect_name: EffectName, now: float | None = None) -> MergeOperation | None:
        """Explicit operator retry: clear a block/exhaustion/rejection for one effect only."""
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                operation = self._load_operation(connection, identity)
                if operation is None:
                    return None
                prior_effect = operation.effect(effect_name)
                if prior_effect.state in (EffectState.CONFIRMED_COMPLETE, EffectState.NOT_NEEDED, EffectState.RUNNING, EffectState.DELIVERY_UNKNOWN):
                    return operation
                updated_effect = EffectRecord(
                    effect_name,
                    EffectState.NOT_ATTEMPTED,
                    attempt_id="",
                    generation=operation.generation,
                    receipt=prior_effect.receipt,
                    throttle_attempts=0,
                    throttled_attempt_ids=(),
                    last_error="",
                    sender_binding=prior_effect.sender_binding,
                )
                new_effects = dict(operation.effects)
                new_effects[effect_name] = updated_effect
                other_blocked = any(e.state in (EffectState.RUNNING, EffectState.DELIVERY_UNKNOWN) for name, e in new_effects.items() if name != effect_name)
                operation = MergeOperation(
                    identity=operation.identity,
                    expected_head_sha=operation.expected_head_sha,
                    merge_method=operation.merge_method,
                    approval_credential_role=operation.approval_credential_role,
                    reviewer_identity=operation.reviewer_identity,
                    generation=operation.generation,
                    status=operation.status if other_blocked else OperationStatus.WAITING,
                    resume_reason=operation.resume_reason if other_blocked else "",
                    not_before=current_time,
                    effects=new_effects,
                )
                self._save_operation(connection, operation, current_time)
                return operation
        except Exception as exc:
            logger.error("Could not manually reset merge-operation effect {}/{}: {}", identity.key(), effect_name.value, exc)
            raise MergeOperationPersistenceError("Merge operation effect could not be reset") from exc

    def recover_after_restart(self, now: float | None = None, *, repository: str | None = None) -> list[MergeOperation]:
        """Reclassify any effect left ``RUNNING`` by a controller that stopped
        before recording a terminal outcome as ``DELIVERY_UNKNOWN`` (REQ-005):
        a crash mid-dispatch must never be assumed to mean "not sent"."""
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                op_keys = {row[0] for row in connection.execute("SELECT op_key FROM merge_operation_effects WHERE state=?", (EffectState.RUNNING.value,)).fetchall()}
                affected: list[MergeOperation] = []
                for row in connection.execute("SELECT api_origin, repository, pr_number FROM merge_operations").fetchall():
                    identity = MergeOperationIdentity(row[0], row[1], row[2])
                    if repository is not None and not owns_merge_operation(repository, identity):
                        continue
                    if identity.key() not in op_keys:
                        continue
                    operation = self._load_operation(connection, identity)
                    if operation is None:
                        continue
                    new_effects = dict(operation.effects)
                    for name, effect in operation.effects.items():
                        if effect.state is EffectState.RUNNING:
                            new_effects[name] = EffectRecord(
                                name,
                                EffectState.DELIVERY_UNKNOWN,
                                attempt_id=effect.attempt_id,
                                generation=effect.generation,
                                receipt=effect.receipt,
                                throttle_attempts=effect.throttle_attempts,
                                throttled_attempt_ids=effect.throttled_attempt_ids,
                                last_error="interrupted before a terminal outcome was recorded",
                                sender_binding=effect.sender_binding,
                            )
                            self._append_history(connection, identity.key(), name, effect.generation, effect.attempt_id, "reopen_delivery_unknown", "", current_time)
                    operation = MergeOperation(
                        identity=operation.identity,
                        expected_head_sha=operation.expected_head_sha,
                        merge_method=operation.merge_method,
                        approval_credential_role=operation.approval_credential_role,
                        reviewer_identity=operation.reviewer_identity,
                        generation=operation.generation,
                        status=OperationStatus.WAITING if operation.status is OperationStatus.RUNNING else operation.status,
                        resume_reason=operation.resume_reason,
                        not_before=operation.not_before,
                        effects=new_effects,
                    )
                    self._save_operation(connection, operation, current_time)
                    affected.append(operation)
                return affected
        except Exception as exc:
            logger.error("Could not recover interrupted merge operations: {}", exc)
            raise MergeOperationPersistenceError("Merge operations could not be recovered") from exc

    def supersede(self, identity: MergeOperationIdentity, now: float | None = None) -> None:
        """Mark an operation stale after an authoritative refresh, retaining receipts/history."""
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                operation = self._load_operation(connection, identity)
                if operation is None:
                    return
                operation = MergeOperation(
                    identity=operation.identity,
                    expected_head_sha=operation.expected_head_sha,
                    merge_method=operation.merge_method,
                    approval_credential_role=operation.approval_credential_role,
                    reviewer_identity=operation.reviewer_identity,
                    generation=operation.generation,
                    status=OperationStatus.SUPERSEDED,
                    resume_reason=operation.resume_reason,
                    not_before=operation.not_before,
                    effects=operation.effects,
                )
                self._save_operation(connection, operation, current_time)
        except Exception as exc:
            logger.error("Could not supersede merge operation {}: {}", identity.key(), exc)
            raise MergeOperationPersistenceError("Merge operation could not be superseded") from exc

    def defer_resumption(self, operation: MergeOperation, *, not_before: float, now: float) -> None:
        """Pace reevaluation without changing effect evidence or newer generations.

        A callback may already have completed, blocked, superseded, or deferred
        the operation. Only a still-waiting identical generation can be delayed;
        an existing future effect/governor deadline is left unchanged.
        """
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._load_operation(connection, operation.identity)
                if current is None or current.generation != operation.generation or current.status is not OperationStatus.WAITING:
                    return
                if current.not_before > now:
                    return
                connection.execute(
                    "UPDATE merge_operations SET not_before=?, updated_at=? WHERE op_key=?",
                    (max(current.not_before, not_before), now, operation.identity.key()),
                )
        except Exception as exc:
            raise MergeOperationPersistenceError("Merge operation resumption could not be deferred") from exc

    def due(self, now: float | None = None, *, repository: str | None = None) -> list[MergeOperation]:
        """Operations that are waiting with at least one retryable effect past its deadline."""
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT api_origin, repository, pr_number FROM merge_operations WHERE status=? AND not_before <= ?",
                    (OperationStatus.WAITING.value, current_time),
                ).fetchall()
                identities = [MergeOperationIdentity(row[0], row[1], row[2]) for row in rows]
                return [op for identity in identities if (repository is None or owns_merge_operation(repository, identity)) and (op := self._load_operation(connection, identity)) is not None]
        except Exception as exc:
            logger.error("Could not read due merge operations: {}", exc)
            raise MergeOperationPersistenceError("Merge operations could not be read") from exc

    def next_due_at(self, *, repository: str | None = None) -> float | None:
        """Earliest deadline among waiting operations, or ``None`` if none are scheduled.

        Used by the resumption scheduler to sleep until the next deadline
        instead of polling; a blocked/superseded/merge-confirmed operation is
        never timer-driven and is therefore excluded.
        """
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT api_origin, repository, pr_number, not_before FROM merge_operations WHERE status=? AND not_before > 0",
                    (OperationStatus.WAITING.value,),
                ).fetchall()
            return min((float(row[3]) for row in rows if repository is None or owns_merge_operation(repository, MergeOperationIdentity(row[0], row[1], row[2]))), default=None)
        except Exception as exc:
            logger.error("Could not read next due merge operation deadline: {}", exc)
            raise MergeOperationPersistenceError("Merge operations could not be read") from exc


def get_merge_operation_store() -> MergeOperationStore:
    """Return the process-wide durable merge-operation store."""
    global _DEFAULT_STORE
    with _LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = MergeOperationStore()
        return _DEFAULT_STORE
