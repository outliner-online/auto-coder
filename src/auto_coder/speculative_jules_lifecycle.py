"""Fail-closed lifecycle fence for PRs owned by Jules competitions.

The observation adapter is the authority for membership and winner identity. This
module turns that read-only decision into ingress and merge decisions. Historical
state is never refreshed from Jules and never authorizes cleanup mutations.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol, cast

from .jules_candidate_observation import ArtifactClassification, ClassificationResult


class SpeculativeClassifier(Protocol):
    def classify(self, repository: str, issue_number: int, pr_repository: str, pr_number: int) -> ClassificationResult: ...

    def classify_pr(
        self,
        repository: str,
        pr_repository: str,
        pr_number: int,
        hinted_issue_numbers: tuple[int, ...] = (),
        independently_unrelated: bool = False,
    ) -> tuple[Optional[int], ClassificationResult]: ...

    def selected_pr_number(self, repository: str, issue_number: int, generation_id: str) -> Optional[int]: ...

    def selected_merge_authorized(
        self,
        repository: str,
        issue_number: int,
        result: ClassificationResult,
        pr_data: Mapping[str, object],
        issue_data: Mapping[str, object],
    ) -> bool: ...

    def selected_ingress_authorized(
        self,
        repository: str,
        issue_number: int,
        result: ClassificationResult,
        pr_data: Mapping[str, object],
    ) -> bool: ...


class CleanupGitHubClient(Protocol):
    def get_pull_request_metadata_strict(self, repository: str, pr_number: int) -> Mapping[str, object]: ...

    def close_pr(self, repository: str, pr_number: int, comment: str) -> None: ...


class RetiredJulesReader:
    """Non-transport placeholder satisfying the observation adapter contract."""

    def get_session(self, session_id: str) -> Mapping[str, object]:
        raise RuntimeError("retired Jules competitions cannot read provider sessions")


@dataclass(frozen=True)
class CleanupObligation:
    repository: str
    issue_number: int
    generation_id: str
    candidate_id: str
    pr_number: int
    selected_pr: Optional[int]
    reason: str


@dataclass(frozen=True)
class LifecycleDecision:
    classification: ArtifactClassification = ArtifactClassification.LEGACY
    allow_ordinary_processing: bool = True
    cleanup_pending: bool = False
    reason: str = ""


class SpeculativeCleanupStore:
    """Durable close obligations; retirement is recorded before GitHub mutation."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS cleanup (
                repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
                generation_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                pr_number INTEGER NOT NULL, selected_pr INTEGER,
                status TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(repository, generation_id, candidate_id, pr_number))""")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def retain(self, repository: str, issue: int, result: ClassificationResult, pr_number: int, selected_pr: Optional[int]) -> None:
        reason = f"retired Jules competitor in generation {result.generation_id}"
        if selected_pr is not None:
            reason += f"; selected PR is #{selected_pr}"
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO cleanup VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)",
                (repository, issue, result.generation_id or "", result.candidate_id or "", pr_number, selected_pr, reason),
            )

    def complete(self, repository: str, generation: str, candidate: str, pr_number: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE cleanup SET status='COMPLETE' WHERE repository=? AND generation_id=? AND candidate_id=? AND pr_number=?",
                (repository, generation, candidate, pr_number),
            )

    def pending(self) -> tuple[CleanupObligation, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT repository, issue_number, generation_id, candidate_id, pr_number, selected_pr, reason FROM cleanup WHERE status='PENDING'").fetchall()
        return tuple(CleanupObligation(str(row[0]), int(row[1]), str(row[2]), str(row[3]), int(row[4]), cast(Optional[int], row[5]), str(row[6])) for row in rows)


class SpeculativeJulesLifecycle:
    def __init__(self, classifier: SpeculativeClassifier, cleanup_store: SpeculativeCleanupStore):
        self.classifier = classifier
        self.cleanup_store = cleanup_store

    def evaluate_pr(
        self,
        repository: str,
        pr_number: int,
        hinted_issue_numbers: tuple[int, ...] = (),
        pr_data: Optional[Mapping[str, object]] = None,
    ) -> LifecycleDecision:
        independently_unrelated = bool(pr_data is not None and isinstance(pr_data.get("_verified_non_jules_pr_origin"), str) and pr_data.get("_verified_non_jules_pr_origin"))
        issue_number, result = self.classifier.classify_pr(
            repository,
            repository,
            pr_number,
            hinted_issue_numbers,
            independently_unrelated,
        )
        if result.classification is ArtifactClassification.LEGACY:
            return LifecycleDecision()
        if result.classification is ArtifactClassification.SELECTED:
            if issue_number is None or pr_data is None or not self.classifier.selected_ingress_authorized(repository, issue_number, result, pr_data):
                return LifecycleDecision(ArtifactClassification.BLOCKED, False, False, "selected acceptance is stale or invalidated")
            return LifecycleDecision(result.classification, True)
        if result.classification is ArtifactClassification.RETIRED:
            return LifecycleDecision(result.classification, False, False, "verified retired Jules competitor")
        # Active candidates use the competition evaluator; suspected/conflicting
        # artifacts must be retried after authoritative evidence becomes available.
        return LifecycleDecision(result.classification, False, False, "; ".join(result.diagnostics) or "candidate-aware evaluation required")

    def evaluate_merge_authority(
        self,
        repository: str,
        pr_number: int,
        pr_data: Mapping[str, object],
        issue_data: Mapping[str, object],
        hinted_issue_numbers: tuple[int, ...] = (),
    ) -> LifecycleDecision:
        """Reestablish fresh selected-pair authority at the merge mutation boundary."""
        independently_unrelated = bool(isinstance(pr_data.get("_verified_non_jules_pr_origin"), str) and pr_data.get("_verified_non_jules_pr_origin"))
        issue_number, result = self.classifier.classify_pr(
            repository,
            repository,
            pr_number,
            hinted_issue_numbers,
            independently_unrelated,
        )
        if result.classification is ArtifactClassification.LEGACY:
            return LifecycleDecision()
        if issue_number is None or result.classification is not ArtifactClassification.SELECTED:
            return LifecycleDecision(result.classification, False, False, "artifact is not the selected Jules result")
        if not self.classifier.selected_merge_authorized(repository, issue_number, result, pr_data, issue_data):
            return LifecycleDecision(ArtifactClassification.BLOCKED, False, False, "selected acceptance is stale or invalidated")
        return LifecycleDecision(ArtifactClassification.SELECTED, True)

    def consume_cleanup(self, github_client: CleanupGitHubClient) -> int:
        """Leave pre-retirement cleanup obligations inspectable and inert."""
        return 0


class RefreshingSpeculativeClassifier:
    """Read the retired ledger and retained observations without Jules traffic.

    The historical name remains for import compatibility.
    """

    def __init__(self, adapter: object, ledger: object):
        self.adapter = adapter
        self.ledger = ledger

    def classify(self, repository: str, issue_number: int, pr_repository: str, pr_number: int, independently_unrelated: bool = False) -> ClassificationResult:
        try:
            result = self.adapter.classify(repository, issue_number, pr_repository, pr_number)  # type: ignore[attr-defined]
            if result.classification is ArtifactClassification.LEGACY:
                snapshot = self.ledger.get_namespace_snapshot(repository, issue_number)  # type: ignore[attr-defined]
                if any(binding.pr_repository.lower() == pr_repository.lower() and binding.pr_number == pr_number for generation in snapshot.generations for candidate in generation.candidates for binding in candidate.bindings):
                    return ClassificationResult(ArtifactClassification.BLOCKED, diagnostics=("ledger binding lacks matching observation authority",))
                if snapshot.generations and not independently_unrelated and not self.adapter.store.has_legacy_membership(repository, pr_repository, pr_number):  # type: ignore[attr-defined]
                    return ClassificationResult(ArtifactClassification.BLOCKED, diagnostics=("unresolved retired Jules ownership",))
            return result  # type: ignore[no-any-return]
        except Exception as exc:
            return ClassificationResult(
                ArtifactClassification.BLOCKED,
                diagnostics=(f"retired Jules ledger unavailable: {type(exc).__name__}",),
            )

    def classify_pr(
        self,
        repository: str,
        pr_repository: str,
        pr_number: int,
        hinted_issue_numbers: tuple[int, ...] = (),
        independently_unrelated: bool = False,
    ) -> tuple[Optional[int], ClassificationResult]:
        try:
            retained_association = self.adapter.store.has_retired_pr_association(repository, pr_repository, pr_number)  # type: ignore[attr-defined]
        except Exception as exc:
            return None, ClassificationResult(
                ArtifactClassification.BLOCKED,
                diagnostics=(f"retired Jules observations unavailable: {type(exc).__name__}",),
            )
        try:
            ledger_issues = self.ledger.list_issue_numbers(repository)  # type: ignore[attr-defined]
        except Exception as exc:
            if independently_unrelated and not retained_association:
                return None, ClassificationResult(ArtifactClassification.LEGACY, mutation_allowed=True)
            return None, ClassificationResult(
                ArtifactClassification.BLOCKED,
                diagnostics=(f"retired Jules ledger unavailable: {type(exc).__name__}",),
            )
        if not ledger_issues:
            if independently_unrelated and not retained_association:
                return None, ClassificationResult(ArtifactClassification.LEGACY, mutation_allowed=True)
            return None, ClassificationResult(
                ArtifactClassification.BLOCKED,
                diagnostics=("retired Jules ownership cannot be reconciled from empty history",),
            )
        issue_numbers = tuple(dict.fromkeys((*hinted_issue_numbers, *ledger_issues)))
        classified = tuple((issue, self.classify(repository, issue, pr_repository, pr_number, independently_unrelated)) for issue in issue_numbers)
        relevant = tuple(item for item in classified if item[1].classification is not ArtifactClassification.LEGACY)
        if not relevant:
            if retained_association:
                return None, ClassificationResult(ArtifactClassification.BLOCKED, diagnostics=("retained association lacks readable ledger authority",))
            return None, ClassificationResult(ArtifactClassification.LEGACY, mutation_allowed=True)
        definitive = tuple(item for item in relevant if item[1].classification in (ArtifactClassification.ACTIVE_UNSELECTED, ArtifactClassification.SELECTED, ArtifactClassification.RETIRED))
        if len(definitive) == 1 and len(relevant) == 1:
            return definitive[0]
        if len(definitive) > 1 or len(relevant) > 1:
            return None, ClassificationResult(ArtifactClassification.BLOCKED, diagnostics=("PR has multiple speculative Issue associations",))
        return relevant[0]

    def selected_pr_number(self, repository: str, issue_number: int, generation_id: str) -> Optional[int]:
        generation = self.ledger.get_namespace_snapshot(repository, issue_number).get_generation(generation_id)  # type: ignore[attr-defined]
        return generation.winner_pr_number if generation is not None else None

    def selected_merge_authorized(
        self,
        repository: str,
        issue_number: int,
        result: ClassificationResult,
        pr_data: Mapping[str, object],
        issue_data: Mapping[str, object],
    ) -> bool:
        from .jules_candidate_selection import CandidateTarget, selected_pair_authorized

        if result.generation_id is None or result.candidate_id is None:
            return False
        generation = self.ledger.get_namespace_snapshot(repository, issue_number).get_generation(result.generation_id)  # type: ignore[attr-defined]
        if generation is None or issue_data.get("state") != "open" or issue_data.get("body") != generation.issue_oracle_snapshot:
            return False
        head = pr_data.get("head")
        base = pr_data.get("base")
        if not isinstance(head, Mapping) or not isinstance(base, Mapping):
            return False
        raw_pr_number = pr_data.get("number")
        if not isinstance(raw_pr_number, int) or isinstance(raw_pr_number, bool):
            return False
        target = CandidateTarget(
            repository=repository,
            issue_number=issue_number,
            generation_id=result.generation_id,
            candidate_id=result.candidate_id,
            pr_repository=repository,
            pr_number=raw_pr_number,
            head_sha=str(head.get("sha") or ""),
            base_sha=str(base.get("sha") or ""),
            issue_oracle_fingerprint=generation.issue_oracle_fingerprint,
        )
        invalidation_revision = self.adapter.store.current_invalidation_revision(repository, issue_number, result.generation_id, result.candidate_id)  # type: ignore[attr-defined]
        return selected_pair_authorized(self.ledger, target, current_invalidation_revision=invalidation_revision)  # type: ignore[arg-type]

    def selected_ingress_authorized(
        self,
        repository: str,
        issue_number: int,
        result: ClassificationResult,
        pr_data: Mapping[str, object],
    ) -> bool:
        """Validate the durable selected pair before ordinary PR effects run."""
        if result.generation_id is None:
            return False
        try:
            generation = self.ledger.get_namespace_snapshot(repository, issue_number).get_generation(result.generation_id)  # type: ignore[attr-defined]
            if generation is None:
                return False
            return self.selected_merge_authorized(
                repository,
                issue_number,
                result,
                pr_data,
                {"state": "open", "body": generation.issue_oracle_snapshot},
            )
        except Exception:
            return False


_lifecycle: Optional[SpeculativeJulesLifecycle] = None


def configure_speculative_jules_lifecycle(lifecycle: Optional[SpeculativeJulesLifecycle]) -> None:
    """Install the process lifecycle authority (also used by isolated tests)."""
    global _lifecycle
    _lifecycle = lifecycle


def get_speculative_jules_lifecycle(github_client: Optional[object] = None) -> Optional[SpeculativeJulesLifecycle]:
    """Return configured authority, lazily creating the production adapter."""
    global _lifecycle
    if _lifecycle is None and github_client is not None:
        from .jules_candidate_observation import CandidateObservationStore, JulesCandidateObservationAdapter
        from .jules_competition_ledger import JulesCompetitionLedger, default_jules_competition_db_path, retired_jules_history_marker_path

        root = Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", Path.home() / ".auto-coder")) / "state"
        ledger_path = default_jules_competition_db_path()
        observation_path = root / "jules_candidate_observations.db"
        history_marker = retired_jules_history_marker_path(ledger_path)
        # Migrate pre-marker installations while at least one historical store
        # is still observable.  The marker then keeps the fence active if both
        # SQLite files are lost later. Absence of both stores and the marker
        # cannot distinguish a fresh installation from pre-migration loss.
        if ledger_path.exists() or observation_path.exists():
            history_marker.parent.mkdir(parents=True, exist_ok=True)
            history_marker.touch(exist_ok=True)
        ledger = JulesCompetitionLedger(ledger_path)
        adapter = JulesCandidateObservationAdapter(
            ledger,
            CandidateObservationStore(observation_path),
            RetiredJulesReader(),
            github_client,  # type: ignore[arg-type]
        )
        _lifecycle = SpeculativeJulesLifecycle(
            RefreshingSpeculativeClassifier(adapter, ledger),
            SpeculativeCleanupStore(default_cleanup_path()),
        )
    return _lifecycle


def default_cleanup_path() -> Path:
    root = Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", Path.home() / ".auto-coder"))
    return root / "state" / "jules_speculative_cleanup.db"


def consume_due_speculative_cleanup(github_client: object) -> int:
    """Run restart-recovered cleanup from a production maintenance origin."""
    lifecycle = get_speculative_jules_lifecycle(github_client)
    if lifecycle is None:
        return 0
    return lifecycle.consume_cleanup(github_client)  # type: ignore[arg-type]


def consume_due_speculative_work(
    repository: str,
    github_client: object,
    enqueue_issue: Optional[Callable[[int], None]] = None,
    enqueue_pr: Optional[Callable[[int], None]] = None,
) -> tuple[int, int]:
    """Retired compatibility entry point; competition effects remain inert."""
    return (0, 0)
