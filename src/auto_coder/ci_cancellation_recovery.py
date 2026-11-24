"""Durable, selective recovery of native cancelled PR CI jobs.

A native cancellation is a recovery candidate, never a successful check. Each
call sends at most one selective request and leaves outcome observation to the
existing webhook/watch reconciliation path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .github_ci_observer import ci_effect_authority, end_ci_read_phase
from .github_request_governor import GitHubRequestGovernor
from .logger_config import get_logger
from .util.gh_cache import get_ghapi_client
from .util.github_request_outcome import DeliveryCertainty, GitHubRequestError, boundary_hooks

logger = get_logger(__name__)
_PAGE_SIZE = 100


@dataclass(frozen=True)
class RecoveryPolicy:
    """Explicit normal-test workflow and unique native root-job names."""

    workflow_path: str
    root_job_names: tuple[str, ...]
    aggregate_job_names: tuple[str, ...] = ()
    multi_root_verified: bool = False

    def __post_init__(self) -> None:
        if not self.workflow_path.startswith(".github/workflows/") or "@" in self.workflow_path or ".." in self.workflow_path.split("/"):
            raise ValueError("Recovery requires an exact workflow path")
        names = self.root_job_names + self.aggregate_job_names
        if type(self.multi_root_verified) is not bool:
            raise ValueError("Multi-root fixture verification must be explicit")
        if not self.root_job_names or any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError("Recovery root and aggregate names must be distinct and nonempty")


@dataclass(frozen=True)
class NativeCIJob:
    job_id: int
    run_id: int
    attempt: int
    name: str
    status: str
    conclusion: str
    check_run_id: int | None = None
    started_at: str = ""
    completed_at: str = ""

    @property
    def execution_fingerprint(self) -> str:
        # GitHub can clone retained jobs with new IDs on a selective rerun.
        # Their native execution timestamps remain unchanged.
        return json.dumps([self.started_at, self.completed_at], separators=(",", ":"))


@dataclass(frozen=True)
class CancellationRun:
    workflow_id: int
    run_id: int
    attempt: int
    workflow_path: str
    status: str
    conclusion: str
    jobs: tuple[NativeCIJob, ...]
    policy: RecoveryPolicy

    @property
    def cancelled_roots(self) -> tuple[NativeCIJob, ...]:
        return tuple(job for job in self.jobs if job.name in self.policy.root_job_names and job.status == "completed" and job.conclusion == "cancelled")


@dataclass(frozen=True)
class CancellationEvidence:
    available: bool
    reason: str
    runs: tuple[CancellationRun, ...] = ()


@dataclass(frozen=True)
class RecoveryResult:
    status: str
    reason: str
    run_id: int = 0
    attempt: int = 0
    job_id: int = 0
    logical_job: str = ""
    claim_id: str = ""


@dataclass(frozen=True)
class RecoveryClaim:
    acquired: bool
    reason: str
    claim_id: str = ""


def _positive(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("missing_native_identity")
    return value


def _mapping(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("ambiguous_native_identity")
    return value


def _pages(api: Any, path: str, key: str, **query: object) -> list[dict]:
    items: list[dict] = []
    seen: set[int] = set()
    for page in range(1, 1001):
        payload = _mapping(api(path, verb="GET", query={**query, "per_page": _PAGE_SIZE, "page": page}))
        entries = payload.get(key)
        total = payload.get("total_count")
        if not isinstance(entries, list) or (total is not None and (type(total) is not int or total < 0)):
            raise ValueError("incomplete_native_inventory")
        for entry in entries:
            item = _mapping(entry)
            identity = _positive(item.get("id"))
            if identity in seen:
                raise ValueError("ambiguous_native_inventory")
            seen.add(identity)
            items.append(item)
        if type(total) is int:
            if len(items) > total:
                raise ValueError("changing_native_inventory")
            if len(items) == total:
                return items
            if not entries:
                raise ValueError("incomplete_native_inventory")
        elif len(entries) < _PAGE_SIZE:
            return items
    raise ValueError("native_inventory_limit")


def _validate_pr(api: Any, repository: str, pr_number: int, expected_head: str) -> dict:
    metadata = _mapping(api(f"/repos/{repository}/pulls/{pr_number}", verb="GET"))
    if metadata.get("number") != pr_number or metadata.get("state") != "open" or metadata.get("merged_at") is not None:
        raise ValueError("pr_not_open")
    head = _mapping(metadata.get("head"))
    base = _mapping(metadata.get("base"))
    if head.get("sha") != expected_head:
        raise ValueError("stale_head")
    if _mapping(base.get("repo")).get("full_name", "").lower() != repository.lower() or not _mapping(head.get("repo")).get("full_name"):
        raise ValueError("ambiguous_pr_repository")
    return metadata


def _validate_run(item: dict, metadata: dict, repository: str, pr_number: int, expected_head: str) -> None:
    associations = item.get("pull_requests")
    if item.get("event") != "pull_request" or item.get("head_sha") != expected_head:
        raise ValueError("not_current_normal_pr_workflow")
    if not isinstance(associations, list) or len(associations) != 1:
        raise ValueError("ambiguous_pr_association")
    association = _mapping(associations[0])
    if association.get("number") != pr_number or _mapping(association.get("head")).get("sha") != expected_head:
        raise ValueError("ambiguous_pr_association")
    if _mapping(item.get("repository")).get("full_name", "").lower() != repository.lower():
        raise ValueError("ambiguous_run_repository")
    expected_repository = _mapping(_mapping(metadata.get("head")).get("repo")).get("full_name")
    if _mapping(item.get("head_repository")).get("full_name") != expected_repository:
        raise ValueError("ambiguous_head_repository")


def _native_jobs(api: Any, repository: str, run_id: int, attempt: int, expected_head: str) -> tuple[NativeCIJob, ...]:
    entries = _pages(api, f"/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    jobs: list[NativeCIJob] = []
    names: set[str] = set()
    for item in entries:
        name = item.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("ambiguous_logical_job")
        names.add(name)
        if item.get("run_id") != run_id or item.get("run_attempt") != attempt or item.get("head_sha") != expected_head:
            raise ValueError("ambiguous_job_association")
        check_url = str(item.get("check_run_url") or "")
        check_match = re.fullmatch(rf"https://api\.github\.com/repos/{re.escape(repository)}/check-runs/([1-9][0-9]*)", check_url)
        jobs.append(NativeCIJob(_positive(item.get("id")), run_id, attempt, name, str(item.get("status") or ""), str(item.get("conclusion") or ""), int(check_match.group(1)) if check_match else None, str(item.get("started_at") or ""), str(item.get("completed_at") or "")))
    if not jobs:
        raise ValueError("empty_job_inventory")
    return tuple(jobs)


def inspect_cancellation_evidence(api: Any, repository: str, pr_number: int, expected_head: str, policies: tuple[RecoveryPolicy, ...]) -> CancellationEvidence:
    """Read complete exact-attempt evidence without performing any mutation."""
    if not policies:
        return CancellationEvidence(True, "disabled")
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not expected_head or pr_number <= 0:
            raise ValueError("invalid_recovery_subject")
        by_path = {policy.workflow_path: policy for policy in policies}
        if len(by_path) != len(policies):
            raise ValueError("ambiguous_workflow_policy")
        metadata = _validate_pr(api, repository, pr_number, expected_head)
        items = _pages(api, f"/repos/{repository}/actions/runs", "workflow_runs", head_sha=expected_head)
        selected: dict[str, dict] = {}
        for item in items:
            path = str(item.get("path") or "").split("@", 1)[0]
            if path not in by_path:
                continue
            # Another event or ambiguous association in an enabled workflow is
            # not evidence that an older PR run remains the effective one.
            _validate_run(item, metadata, repository, pr_number, expected_head)
            number = _positive(item.get("run_number"))
            _positive(item.get("run_attempt"))
            _positive(item.get("workflow_id"))
            previous = selected.get(path)
            if previous is not None and previous.get("run_number") == number:
                raise ValueError("ambiguous_effective_run")
            if previous is None or number > previous["run_number"]:
                selected[path] = item
        runs: list[CancellationRun] = []
        for path in sorted(selected):
            item = selected[path]
            run_id, attempt = _positive(item.get("id")), _positive(item.get("run_attempt"))
            # A collection and detail must agree before any attempt inventory
            # is interpreted as current; a rerun racing the read is deferred.
            detail = _mapping(api(f"/repos/{repository}/actions/runs/{run_id}", verb="GET"))
            _validate_run(detail, metadata, repository, pr_number, expected_head)
            if detail.get("id") != run_id or detail.get("run_attempt") != attempt or detail.get("workflow_id") != item.get("workflow_id") or detail.get("run_number") != item.get("run_number") or str(detail.get("path") or "").split("@", 1)[0] != path:
                raise ValueError("superseded_attempt")
            jobs = _native_jobs(api, repository, run_id, attempt, expected_head)
            runs.append(CancellationRun(_positive(item.get("workflow_id")), run_id, attempt, path, str(detail.get("status") or ""), str(detail.get("conclusion") or ""), jobs, by_path[path]))
        return CancellationEvidence(True, "native_evidence", tuple(runs))
    except GitHubRequestError as exc:
        return CancellationEvidence(False, f"github_{exc.outcome.classification.value}")
    except Exception as exc:
        # Error messages from transports may contain credentials or signed URLs.
        reason = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_]+", str(exc)) else "native_evidence_unavailable"
        return CancellationEvidence(False, reason)


class CancellationRecoveryStore:
    """SQLite effect claims with atomic per-run serialization across workers."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path or os.environ.get("AUTO_CODER_CI_RECOVERY_DB", "~/.auto-coder/ci-cancellation-recovery.sqlite3")).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("""CREATE TABLE IF NOT EXISTS ci_job_recovery_claims (
            claim_key TEXT PRIMARY KEY, claim_id TEXT NOT NULL, repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL, head_sha TEXT NOT NULL, workflow_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL, attempt INTEGER NOT NULL, job_id INTEGER NOT NULL,
            logical_job TEXT NOT NULL, execution_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS ci_job_recovery_run_owner ON ci_job_recovery_claims(repository, run_id) WHERE settled = 0")
        return connection

    @staticmethod
    def _key(repository: str, pr_number: int, head_sha: str, run: CancellationRun, job: NativeCIJob) -> str:
        return hashlib.sha256(json.dumps([repository.lower(), pr_number, head_sha, run.workflow_id, run.run_id, job.name], separators=(",", ":")).encode()).hexdigest()

    def acquire(self, repository: str, pr_number: int, head_sha: str, run: CancellationRun, job: NativeCIJob) -> RecoveryClaim:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            key = self._key(repository, pr_number, head_sha, run, job)
            if connection.execute("SELECT 1 FROM ci_job_recovery_claims WHERE claim_key = ?", (key,)).fetchone():
                connection.execute("COMMIT")
                return RecoveryClaim(False, "logical_job_already_claimed")
            if not run.policy.multi_root_verified and connection.execute("SELECT 1 FROM ci_job_recovery_claims WHERE repository = ? AND run_id = ?", (repository.lower(), run.run_id)).fetchone():
                connection.execute("COMMIT")
                return RecoveryClaim(False, "multi_root_fixture_unverified")
            active = connection.execute("SELECT * FROM ci_job_recovery_claims WHERE repository = ? AND run_id = ? AND settled = 0", (repository.lower(), run.run_id)).fetchone()
            if active is not None:
                observed = next((entry for entry in run.jobs if entry.name == active["logical_job"]), None)
                reconciled = run.status == "completed" and run.attempt > active["attempt"] and observed is not None and observed.status == "completed" and bool(observed.completed_at) and observed.execution_fingerprint != active["execution_fingerprint"]
                if not reconciled:
                    connection.execute("COMMIT")
                    return RecoveryClaim(False, "previous_effect_unreconciled")
                connection.execute("UPDATE ci_job_recovery_claims SET settled = 1, updated_at = ? WHERE claim_id = ?", (time.time(), active["claim_id"]))
            claim_id = str(uuid4())
            now = time.time()
            connection.execute("INSERT INTO ci_job_recovery_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)", (key, claim_id, repository.lower(), pr_number, head_sha, run.workflow_id, run.run_id, run.attempt, job.job_id, job.name, job.execution_fingerprint, now, now))
            connection.execute("COMMIT")
            return RecoveryClaim(True, "claimed", claim_id)
        except Exception:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            return RecoveryClaim(False, "claim_store_unavailable")
        finally:
            if connection is not None:
                connection.close()

    def finish(self, claim_id: str, state: str) -> bool:
        """Retain every possible effect; release only proven unsent claims."""
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            if state == "not_sent":
                cursor = connection.execute("DELETE FROM ci_job_recovery_claims WHERE claim_id = ? AND state = 'pending'", (claim_id,))
            else:
                cursor = connection.execute("UPDATE ci_job_recovery_claims SET state = ?, settled = ?, updated_at = ? WHERE claim_id = ?", (state, int(state == "rejected"), time.time(), claim_id))
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            return False
        finally:
            if connection is not None:
                connection.close()


def _result(status: str, reason: str, run: CancellationRun | None = None, job: NativeCIJob | None = None, claim_id: str = "") -> RecoveryResult:
    result = RecoveryResult(status, reason, run.run_id if run else 0, run.attempt if run else 0, job.job_id if job else 0, job.name if job else "", claim_id)
    logger.info("CI cancellation recovery status={} reason={} run={} attempt={} job={} claim={}", status, reason, result.run_id, result.attempt, result.job_id, claim_id)
    return result


def _final_identity_current(api: Any, repository: str, pr_number: int, expected_head: str, run: CancellationRun) -> bool:
    """Fence changes that arrive during the final paginated job inventory."""
    try:
        metadata = _validate_pr(api, repository, pr_number, expected_head)
        items = _pages(api, f"/repos/{repository}/actions/runs", "workflow_runs", head_sha=expected_head)
        applicable = []
        for item in items:
            if str(item.get("path") or "").split("@", 1)[0] != run.workflow_path:
                continue
            _validate_run(item, metadata, repository, pr_number, expected_head)
            _positive(item.get("run_number"))
            applicable.append(item)
        if not applicable:
            return False
        latest = max(applicable, key=lambda item: item["run_number"])
        if sum(item["run_number"] == latest["run_number"] for item in applicable) != 1:
            return False
        if latest.get("id") != run.run_id or latest.get("run_attempt") != run.attempt or latest.get("status") != "completed":
            return False
        current = _mapping(api(f"/repos/{repository}/actions/runs/{run.run_id}", verb="GET"))
        _validate_run(current, metadata, repository, pr_number, expected_head)
        return current.get("id") == run.run_id and current.get("workflow_id") == run.workflow_id and current.get("run_attempt") == run.attempt and current.get("status") == "completed"
    except Exception:
        return False


def _selective_write_client(token: str, read_api: Any) -> Any:
    """Use the existing governor without waiting after final identity reads."""
    admission, observation = boundary_hooks()
    governor = getattr(admission, "__self__", None)
    if isinstance(governor, GitHubRequestGovernor):
        return get_ghapi_client(token, admission_hook=governor.admit, observation_hook=observation, extra_headers={"Cache-Control": "no-cache"})
    return read_api


def recover_cancelled_jobs(github_client: Any, repository: str, pr_number: int, expected_head: str, policies: tuple[RecoveryPolicy, ...], *, store: CancellationRecoveryStore | None = None) -> RecoveryResult:
    """Recover one current cancelled root, never a whole workflow or a success."""
    if not policies:
        return _result("disabled", "no_enabled_workflow")
    try:
        api = get_ghapi_client(github_client.token, extra_headers={"Cache-Control": "no-cache"})
    except Exception:
        return _result("deferred", "repository_client_unavailable")
    claims = store or CancellationRecoveryStore()
    evidence = inspect_cancellation_evidence(api, repository, pr_number, expected_head, policies)
    if not evidence.available:
        return _result("deferred", evidence.reason)
    last = RecoveryResult("no_candidate", "no_native_cancelled_root")
    for run in evidence.runs:
        roots = run.cancelled_roots
        if not roots:
            continue
        if run.status != "completed" or any(job.status != "completed" for job in run.jobs):
            last = _result("deferred", "active_execution", run)
            continue
        if len(roots) > 1 and not run.policy.multi_root_verified:
            last = _result("deferred", "multi_root_fixture_unverified", run)
            continue
        for job in roots:
            claim = claims.acquire(repository, pr_number, expected_head, run, job)
            if not claim.acquired:
                last = _result("deferred", claim.reason, run, job)
                continue
            with ci_effect_authority():
                fresh = inspect_cancellation_evidence(api, repository, pr_number, expected_head, policies)
                current_run = next((entry for entry in fresh.runs if entry.run_id == run.run_id and entry.attempt == run.attempt), None)
                current_job = next((entry for entry in current_run.cancelled_roots if entry == job), None) if current_run is not None else None
                if not fresh.available or current_run is None or current_run.status != "completed" or any(entry.status != "completed" for entry in current_run.jobs) or current_job is None:
                    claims.finish(claim.claim_id, "not_sent")
                    return _result("deferred", fresh.reason if not fresh.available else "identity_changed_before_send", run, job, claim.claim_id)
                if not _final_identity_current(api, repository, pr_number, expected_head, current_run):
                    claims.finish(claim.claim_id, "not_sent")
                    return _result("deferred", "identity_changed_before_send", run, job, claim.claim_id)
                end_ci_read_phase("cancelled-ci-selective-rerun")
                try:
                    # GhApi's cached client routes this POST through the same
                    # repository credential and shared request governor.
                    write_api = _selective_write_client(github_client.token, api)
                    write_api(f"/repos/{repository}/actions/jobs/{job.job_id}/rerun", verb="POST", data={"enable_debug_logging": False})
                except GitHubRequestError as exc:
                    if exc.outcome.delivery is DeliveryCertainty.DEFINITELY_NOT_SENT:
                        claims.finish(claim.claim_id, "not_sent")
                        return _result("deferred", "github_request_not_sent", run, job, claim.claim_id)
                    state = "rejected" if exc.outcome.delivery is DeliveryCertainty.HTTP_RESPONSE_RECEIVED and exc.outcome.status is not None and 400 <= exc.outcome.status < 500 else "indeterminate"
                    claims.finish(claim.claim_id, state)
                    return _result(state, "github_selective_rerun_" + state, run, job, claim.claim_id)
                except Exception:
                    claims.finish(claim.claim_id, "indeterminate")
                    return _result("indeterminate", "github_selective_rerun_indeterminate", run, job, claim.claim_id)
                if not claims.finish(claim.claim_id, "accepted"):
                    return _result("indeterminate", "accepted_effect_record_unavailable", run, job, claim.claim_id)
                return _result("accepted", "github_selective_rerun_accepted", run, job, claim.claim_id)
    return last
