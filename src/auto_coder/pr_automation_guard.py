"""Durable operator-intervention latch for abnormal pull-request histories."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .logger_config import get_logger

logger = get_logger(__name__)
DEFAULT_COMMIT_LIMIT = 30
_REVERT_SUBJECT = re.compile(r"^Revert(?:\s|:)", re.IGNORECASE)
_REVERT_BODY = re.compile(r"^This reverts commit [0-9a-f]{40,64}\.\s*$", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class PRCommit:
    sha: str
    message: str

    @property
    def is_revert(self) -> bool:
        subject = self.message.splitlines()[0] if self.message else ""
        return bool(_REVERT_SUBJECT.match(subject) or _REVERT_BODY.search(self.message))


@dataclass(frozen=True)
class PRCommitHistory:
    head_sha: str
    commit_count: int
    commits: tuple[PRCommit, ...]
    complete: bool = True

    def validate(self) -> None:
        if not self.head_sha or type(self.commit_count) is not int or self.commit_count < 0:
            raise ValueError("PR history identity or count is unavailable")
        shas = [commit.sha for commit in self.commits]
        if any(not sha for sha in shas) or len(set(shas)) != len(shas):
            raise ValueError("PR history contains missing or duplicate commit identities")
        if len(shas) > self.commit_count or (self.complete and len(shas) != self.commit_count):
            raise ValueError("PR history count disagrees with its commit listing")
        if self.complete and self.commit_count and self.head_sha not in shas:
            raise ValueError("PR history does not contain its observed head")


class HistoryReader(Protocol):
    def get_pr_automation_history_strict(self, repository: str, pr_number: int) -> PRCommitHistory: ...


@dataclass(frozen=True)
class PRGuardDecision:
    disposition: str
    reason: str = ""
    head_sha: str = ""
    observed_commits: int = 0
    commit_limit: int = DEFAULT_COMMIT_LIMIT
    revert_sha: str = ""

    def __post_init__(self) -> None:
        if self.disposition not in {"allowed", "stopped", "deferred", "unobserved"}:
            raise ValueError("unknown PR automation guard state")
        if type(self.observed_commits) is not int or self.observed_commits < 0 or self.commit_limit != DEFAULT_COMMIT_LIMIT:
            raise ValueError("invalid persisted PR commit budget")
        if not all(isinstance(value, str) for value in (self.reason, self.head_sha, self.revert_sha)) or (self.stopped and not self.reason):
            raise ValueError("invalid persisted PR automation stop")

    @property
    def allowed(self) -> bool:
        return self.disposition == "allowed"

    @property
    def stopped(self) -> bool:
        return self.disposition == "stopped"


class PRAutomationRefused(RuntimeError):
    """A new effect was refused before transport by the durable PR guard."""

    def __init__(self, decision: PRGuardDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason)


class PRGuardStore:
    """One transaction serializes observation, latched refusal and manual grants."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".auto-coder" / "pr_automation_guard.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS pr_guard_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO pr_guard_metadata VALUES('schema_version','1')")
            if connection.execute("SELECT value FROM pr_guard_metadata WHERE key='schema_version'").fetchone()[0] != "1":
                raise ValueError("unsupported PR automation guard schema")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS pr_guard (
                    repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                    seen TEXT NOT NULL, baseline TEXT NOT NULL,
                    acknowledged_reverts TEXT NOT NULL, reverts TEXT NOT NULL, decision TEXT NOT NULL,
                    PRIMARY KEY(repository, pr_number));
                CREATE TABLE IF NOT EXISTS pr_guard_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                    event TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL);
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _identity(repository: str, pr_number: int) -> str:
        if not isinstance(repository, str) or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository.strip()) is None or type(pr_number) is not int or pr_number <= 0:
            raise ValueError("repository and positive PR number are required")
        return repository.strip().casefold()

    @staticmethod
    def _ids(encoded: str) -> set[str]:
        values = json.loads(encoded)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
            raise ValueError("persisted PR history identities are malformed")
        return set(values)

    def status(self, repository: str, pr_number: int) -> PRGuardDecision:
        repository = self._identity(repository, pr_number)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT decision FROM pr_guard WHERE repository=? AND pr_number=?", (repository, pr_number)).fetchone()
        return PRGuardDecision(**json.loads(row[0])) if row else PRGuardDecision("unobserved", "PR history has not been observed")

    def stop_generation(self, repository: str, pr_number: int) -> int:
        """Capture the stop being acknowledged before reading its current head."""
        repository = self._identity(repository, pr_number)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT MAX(id) FROM pr_guard_events WHERE repository=? AND pr_number=? AND event='stopped'", (repository, pr_number)).fetchone()
        if row is None or row[0] is None:
            raise ValueError("PR has no retained automation stop")
        return int(row[0])

    def events(self, repository: str, pr_number: int) -> list[dict[str, object]]:
        repository = self._identity(repository, pr_number)
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT event,evidence,created_at FROM pr_guard_events WHERE repository=? AND pr_number=? ORDER BY id", (repository, pr_number)).fetchall()
        return [{"event": row[0], "evidence": json.loads(row[1]), "created_at": row[2]} for row in rows]

    def observe(self, repository: str, pr_number: int, history: PRCommitHistory) -> PRGuardDecision:
        repository = self._identity(repository, pr_number)
        history.validate()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM pr_guard WHERE repository=? AND pr_number=?", (repository, pr_number)).fetchone()
            if row:
                prior = PRGuardDecision(**json.loads(row["decision"]))
                if prior.stopped:
                    return prior
            seen = self._ids(row["seen"]) if row else set()
            baseline = self._ids(row["baseline"]) if row else set()
            acknowledged = self._ids(row["acknowledged_reverts"]) if row else set()
            reverts = self._ids(row["reverts"]) if row else set()
            reverts.update(commit.sha for commit in history.commits if commit.is_revert)
            current = {commit.sha for commit in history.commits}
            seen.update(current)
            count = max(len(seen - baseline), history.commit_count - len(baseline & current))
            revert = next((commit.sha for commit in history.commits if commit.is_revert and commit.sha not in acknowledged), "")
            reason = f"Revert commit {revert} requires external intervention" if revert else ""
            if not reason and count >= DEFAULT_COMMIT_LIMIT:
                reason = f"PR commit budget reached ({count}/{DEFAULT_COMMIT_LIMIT}); external intervention is required"
            if not reason and not history.complete:
                return PRGuardDecision("deferred", "PR commit history is incomplete", history.head_sha, count)
            decision = PRGuardDecision("stopped" if reason else "allowed", reason, history.head_sha, count, DEFAULT_COMMIT_LIMIT, revert)
            encoded = json.dumps(asdict(decision), sort_keys=True)
            connection.execute(
                "INSERT INTO pr_guard VALUES(?,?,?,?,?,?,?) ON CONFLICT(repository,pr_number) DO UPDATE SET seen=excluded.seen,reverts=excluded.reverts,decision=excluded.decision",
                (repository, pr_number, json.dumps(sorted(seen)), json.dumps(sorted(baseline)), json.dumps(sorted(acknowledged)), json.dumps(sorted(reverts)), encoded),
            )
            if decision.stopped:
                connection.execute("INSERT INTO pr_guard_events(repository,pr_number,event,evidence,created_at) VALUES(?,?,?,?,?)", (repository, pr_number, "stopped", encoded, time.time()))
            connection.commit()
        return decision

    def observe_local_reverts(self, repository: str, pr_number: int, commits: tuple[PRCommit, ...], *, local_head: str) -> PRGuardDecision | None:
        """Latch local Revert evidence without claiming it is remote PR history."""
        repository = self._identity(repository, pr_number)
        if not isinstance(local_head, str) or not local_head:
            raise ValueError("local publication head is unavailable")
        if any(not isinstance(commit.sha, str) or not commit.sha or not isinstance(commit.message, str) for commit in commits):
            raise ValueError("local commit identities or messages are malformed")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM pr_guard WHERE repository=? AND pr_number=?", (repository, pr_number)).fetchone()
            prior = PRGuardDecision(**json.loads(row["decision"])) if row else PRGuardDecision("unobserved")
            if prior.stopped:
                return prior
            seen = self._ids(row["seen"]) if row else set()
            baseline = self._ids(row["baseline"]) if row else set()
            acknowledged = self._ids(row["acknowledged_reverts"]) if row else set()
            reverts = self._ids(row["reverts"]) if row else set()
            local_reverts = tuple(commit.sha for commit in commits if commit.is_revert)
            revert = next((sha for sha in local_reverts if sha not in acknowledged), "")
            if not revert:
                return None
            reverts.update(local_reverts)
            decision = PRGuardDecision("stopped", f"Local Revert commit {revert} requires external intervention", prior.head_sha, prior.observed_commits, DEFAULT_COMMIT_LIMIT, revert)
            encoded = json.dumps(asdict(decision), sort_keys=True)
            connection.execute(
                "INSERT INTO pr_guard VALUES(?,?,?,?,?,?,?) ON CONFLICT(repository,pr_number) DO UPDATE SET reverts=excluded.reverts,decision=excluded.decision",
                (repository, pr_number, json.dumps(sorted(seen)), json.dumps(sorted(baseline)), json.dumps(sorted(acknowledged)), json.dumps(sorted(reverts)), encoded),
            )
            evidence = {**asdict(decision), "source": "local_pr_history", "local_head": local_head, "local_revert_shas": list(local_reverts)}
            connection.execute("INSERT INTO pr_guard_events(repository,pr_number,event,evidence,created_at) VALUES(?,?,?,?,?)", (repository, pr_number, "stopped", json.dumps(evidence, sort_keys=True), time.time()))
            connection.commit()
        return decision

    def resume(self, repository: str, pr_number: int, history: PRCommitHistory, *, expected_head: str, reason: str, expected_stop_generation: int) -> PRGuardDecision:
        """Acknowledge existing evidence and grant thirty additional commits."""
        repository = self._identity(repository, pr_number)
        history.validate()
        if not reason.strip() or history.head_sha != expected_head or not history.complete:
            raise ValueError("resume requires a reason and complete history at the expected current head")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM pr_guard WHERE repository=? AND pr_number=?", (repository, pr_number)).fetchone()
            if row is None or not PRGuardDecision(**json.loads(row["decision"])).stopped:
                raise ValueError("PR has no latched automation stop to acknowledge")
            generation = connection.execute("SELECT MAX(id) FROM pr_guard_events WHERE repository=? AND pr_number=? AND event='stopped'", (repository, pr_number)).fetchone()[0]
            if type(expected_stop_generation) is not int or generation != expected_stop_generation:
                raise ValueError("PR automation stop changed during acknowledgement; inspect the new stop before resuming")
            seen = self._ids(row["seen"]) | {commit.sha for commit in history.commits}
            reverts = self._ids(row["reverts"]) | {commit.sha for commit in history.commits if commit.is_revert}
            acknowledged = self._ids(row["acknowledged_reverts"]) | reverts
            prior = PRGuardDecision(**json.loads(row["decision"]))
            if prior.revert_sha:
                acknowledged.add(prior.revert_sha)
            decision = PRGuardDecision("allowed", "Operator acknowledged prior stop", history.head_sha, 0)
            connection.execute(
                "UPDATE pr_guard SET seen=?,baseline=?,acknowledged_reverts=?,reverts=?,decision=? WHERE repository=? AND pr_number=?",
                (json.dumps(sorted(seen)), json.dumps(sorted(seen)), json.dumps(sorted(acknowledged)), json.dumps(sorted(reverts)), json.dumps(asdict(decision), sort_keys=True), repository, pr_number),
            )
            evidence = {"expected_head": expected_head, "stop_generation": generation, "reason": reason.strip(), "previous_stop": asdict(prior), "additional_commit_budget": DEFAULT_COMMIT_LIMIT, "acknowledged_reverts": sorted(acknowledged)}
            connection.execute("INSERT INTO pr_guard_events(repository,pr_number,event,evidence,created_at) VALUES(?,?,?,?,?)", (repository, pr_number, "operator_resumed", json.dumps(evidence, sort_keys=True), time.time()))
            connection.commit()
        return decision


def assess_pr_automation(client: object, repository: str, pr_number: int, *, store: PRGuardStore | None = None) -> PRGuardDecision:
    """Read failures defer work; a known stop never needs another remote read."""
    try:
        ledger = store or PRGuardStore()
        retained = ledger.status(repository, pr_number)
        if retained.stopped:
            return retained
        reader = getattr(client, "get_pr_automation_history_strict", None)
        if not callable(reader):
            raise ValueError("GitHub client has no strict PR history reader")
        history = reader(repository, pr_number)
        if not isinstance(getattr(history, "head_sha", None), str) or type(getattr(history, "commit_count", None)) is not int or not isinstance(getattr(history, "commits", None), tuple):
            raise ValueError("GitHub returned no authoritative PR commit history")
        decision = ledger.observe(repository, pr_number, history)
        if decision.stopped:
            logger.warning("Stopped PR automation for {}#{}: {}", repository, pr_number, decision.reason)
        return decision
    except Exception as exc:
        logger.warning("PR automation history unavailable for {}#{}: {}", repository, pr_number, exc)
        return PRGuardDecision("deferred", f"PR automation history unavailable: {exc}")


def assess_local_pr_automation(repository: str, pr_number: int, workspace: Path, source_head: str, candidate_head: str = "HEAD", *, exclude_base_head: str = "", store: PRGuardStore | None = None) -> PRGuardDecision | None:
    """Inspect outgoing local ancestry before another commit or publication.

    No remote head or commit-count claim is derived from a private checkout.
    Read failures refuse the pending effect; only an actual Revert is latched.
    """
    try:
        ledger = store or PRGuardStore()
        retained = ledger.status(repository, pr_number)
        if retained.stopped:
            return retained

        def git(*args: str) -> str:
            result = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "local PR history is unavailable")
            return result.stdout

        source = git("rev-parse", "--verify", f"{source_head}^{{commit}}").strip()
        candidate = git("rev-parse", "--verify", f"{candidate_head}^{{commit}}").strip()
        git("merge-base", "--is-ancestor", source, candidate)
        excluded = [git("rev-parse", "--verify", f"{exclude_base_head}^{{commit}}").strip()] if exclude_base_head else []
        encoded = git("log", "--reverse", "--format=%H%x00%B%x00", f"{source}..{candidate}", *[f"^{sha}" for sha in excluded], "--")
        fields = encoded.split("\0")
        if len(fields) % 2 != 1 or fields[-1].strip():
            raise ValueError("local PR commit listing is malformed")
        commits = tuple(PRCommit(fields[index].strip(), fields[index + 1]) for index in range(0, len(fields) - 1, 2))
        decision = ledger.observe_local_reverts(repository, pr_number, commits, local_head=candidate)
        if decision is not None:
            logger.warning("Stopped local PR automation for {}#{}: {}", repository, pr_number, decision.reason)
            try:
                from .execution_trace import EventKind, Outcome, get_trace_collector

                get_trace_collector().record_event(
                    EventKind.STAGE_RESULT, stage_id="pr.automation-stop", origin="pr.automation-stop", label=f"pr#{pr_number} automation stop", outcome=Outcome.BLOCKED, facts={"pr_number": pr_number, **asdict(decision), "source": "local_pr_history", "local_head": candidate}
                )
            except Exception:
                logger.debug("Local PR stop diagnostic recording failed for {}#{}", repository, pr_number)
        return decision
    except Exception as exc:
        logger.warning("Local PR history unavailable for {}#{}: {}", repository, pr_number, exc)
        return PRGuardDecision("deferred", f"Local PR history unavailable: {exc}")
