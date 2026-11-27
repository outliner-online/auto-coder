from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_coder.automation_config import AutomationConfig, PRProcessingOutcome
from auto_coder.jules_candidate_observation import ArtifactClassification, ClassificationResult
from auto_coder.jules_competition_ledger import ObligationKind
from auto_coder.pr_processor import process_pull_request
from auto_coder.speculative_jules_lifecycle import (
    CleanupObligation,
    RefreshingSpeculativeClassifier,
    SpeculativeCleanupStore,
    SpeculativeJulesLifecycle,
    configure_speculative_jules_lifecycle,
    consume_due_speculative_work,
)


class Classifier:
    def __init__(self, result: ClassificationResult, issue_number: int = 7, selected_pr: int | None = None):
        self.result = result
        self.issue_number = issue_number
        self.selected_pr = selected_pr
        self.calls = 0

    def classify(self, repository: str, issue_number: int, pr_repository: str, pr_number: int) -> ClassificationResult:
        self.calls += 1
        return self.result

    def classify_pr(
        self,
        repository: str,
        pr_repository: str,
        pr_number: int,
        hinted_issue_numbers: tuple[int, ...] = (),
        independently_unrelated: bool = False,
    ) -> tuple[int, ClassificationResult]:
        self.calls += 1
        return self.issue_number, self.result

    def selected_pr_number(self, repository: str, issue_number: int, generation_id: str) -> int | None:
        return self.selected_pr

    def selected_ingress_authorized(self, repository: str, issue_number: int, result: ClassificationResult, pr_data: object) -> bool:
        return True


def lifecycle(tmp_path: Path, result: ClassificationResult) -> tuple[SpeculativeJulesLifecycle, Classifier]:
    classifier = Classifier(result)
    return SpeculativeJulesLifecycle(classifier, SpeculativeCleanupStore(tmp_path / "cleanup.db")), classifier


def test_targeted_maintenance_refreshes_known_sessions_without_account_listing(tmp_path: Path) -> None:
    adapter = Mock()
    candidate = SimpleNamespace(candidate_id="c1", session_id="session-1")
    generation = SimpleNamespace(generation_id="g1", candidates=(candidate,))
    ledger = Mock()
    ledger.list_issue_numbers.return_value = (7,)
    ledger.get_namespace_snapshot.return_value = SimpleNamespace(generations=(generation,))
    ledger.list_pending_obligations.return_value = ()
    classifier = RefreshingSpeculativeClassifier(adapter, ledger)
    service = SpeculativeJulesLifecycle(classifier, SpeculativeCleanupStore(tmp_path / "cleanup.db"))
    configure_speculative_jules_lifecycle(service)
    try:
        assert consume_due_speculative_work("owner/repo", Mock()) == (0, 0)
    finally:
        configure_speculative_jules_lifecycle(None)
    adapter.observe_candidate.assert_not_called()
    adapter.list_sessions.assert_not_called()


def test_targeted_maintenance_delivers_durable_aggregate_failure(tmp_path: Path) -> None:
    adapter = Mock()
    ledger = Mock()
    ledger.list_issue_numbers.return_value = (7,)
    ledger.get_namespace_snapshot.return_value = SimpleNamespace(generations=())
    obligation = SimpleNamespace(kind=ObligationKind.AGGREGATE_FAILURE, obligation_id="obl-1")
    ledger.list_pending_obligations.return_value = (obligation,)
    classifier = RefreshingSpeculativeClassifier(adapter, ledger)
    service = SpeculativeJulesLifecycle(classifier, SpeculativeCleanupStore(tmp_path / "cleanup.db"))
    enqueue_issue = Mock()
    configure_speculative_jules_lifecycle(service)
    try:
        assert consume_due_speculative_work("owner/repo", Mock(), enqueue_issue=enqueue_issue) == (0, 0)
    finally:
        configure_speculative_jules_lifecycle(None)
    enqueue_issue.assert_not_called()
    ledger.mark_obligation_delivered.assert_not_called()
    ledger.acknowledge_obligation.assert_not_called()


def test_active_candidate_is_deferred_without_cleanup(tmp_path: Path) -> None:
    service, _ = lifecycle(tmp_path, ClassificationResult(ArtifactClassification.ACTIVE_UNSELECTED, "c1", "g1"))
    decision = service.evaluate_pr("owner/repo", 41)
    assert decision.allow_ordinary_processing is False
    assert decision.cleanup_pending is False
    assert service.cleanup_store.pending() == ()


def test_retirement_is_durable_before_cleanup_and_survives_restart(tmp_path: Path) -> None:
    result = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, _ = lifecycle(tmp_path, result)
    assert service.evaluate_pr("owner/repo", 42).cleanup_pending is False
    restarted = SpeculativeCleanupStore(tmp_path / "cleanup.db")
    assert restarted.pending() == ()


def test_cleanup_rechecks_authority_and_confirms_close(tmp_path: Path) -> None:
    result = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, classifier = lifecycle(tmp_path, result)
    service.cleanup_store.retain("owner/repo", 7, result, 42, None)
    github = Mock()
    assert service.consume_cleanup(github) == 0
    github.get_pull_request_metadata_strict.assert_not_called()
    github.close_pr.assert_not_called()
    assert len(service.cleanup_store.pending()) == 1


def test_cleanup_conflict_at_mutation_boundary_keeps_obligation(tmp_path: Path) -> None:
    retired = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, classifier = lifecycle(tmp_path, retired)
    service.cleanup_store.retain("owner/repo", 7, retired, 42, None)
    classifier.result = retired
    github = Mock()
    github.get_pull_request_metadata_strict.return_value = {"number": 42, "state": "open"}

    original = classifier.classify

    def changing(*args: object) -> ClassificationResult:
        if classifier.calls >= 2:
            return ClassificationResult(ArtifactClassification.BLOCKED, "c2", "g1")
        return original(*args)

    classifier.classify = changing  # type: ignore[method-assign]

    assert service.consume_cleanup(github) == 0
    github.close_pr.assert_not_called()
    assert len(service.cleanup_store.pending()) == 1


def test_top_level_processor_fences_retired_empty_pr_before_lifecycle_effects(tmp_path: Path) -> None:
    retired = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, _ = lifecycle(tmp_path, retired)
    github = Mock()
    pr = {
        "number": 42,
        "state": "open",
        "changed_files": 0,
        "body": "Closes #7",
        "head": {"sha": "abc", "ref": "issue-7"},
    }
    configure_speculative_jules_lifecycle(service)
    try:
        result = process_pull_request(github, AutomationConfig(), "owner/repo", pr, force_adversarial_validation=True)
    finally:
        configure_speculative_jules_lifecycle(None)

    assert result.outcome is PRProcessingOutcome.DEFERRED
    assert result.priority == "defer"
    assert result.actions_taken == ["Speculative Jules retired artifact fenced: verified retired Jules competitor"]
    github.close_pr.assert_not_called()


def test_top_level_processor_fences_unlinked_retired_pr(tmp_path: Path) -> None:
    retired = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, _ = lifecycle(tmp_path, retired)
    github = Mock()
    pr = {"number": 42, "state": "open", "changed_files": 0, "body": "", "head": {"sha": "abc", "ref": "jules-output"}}
    configure_speculative_jules_lifecycle(service)
    try:
        result = process_pull_request(github, AutomationConfig(), "owner/repo", pr)
    finally:
        configure_speculative_jules_lifecycle(None)

    assert result.outcome is PRProcessingOutcome.DEFERRED
    assert result.priority == "defer"
    github.close_pr.assert_not_called()


def test_cleanup_reason_includes_selected_pr_when_known(tmp_path: Path) -> None:
    result = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    classifier = Classifier(result, selected_pr=41)
    service = SpeculativeJulesLifecycle(classifier, SpeculativeCleanupStore(tmp_path / "cleanup.db"))
    service.cleanup_store.retain("owner/repo", 7, result, 42, 41)
    assert service.cleanup_store.pending()[0].reason == "retired Jules competitor in generation g1; selected PR is #41"


def test_jules_maintenance_drives_due_cleanup_without_new_issue_event() -> None:
    github = Mock()
    cleanup = Mock(return_value=1)
    with (
        patch("auto_coder.jules_engine.JulesClient") as jules_client,
        patch("auto_coder.jules_engine.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.speculative_jules_lifecycle.consume_due_speculative_cleanup", cleanup),
    ):
        jules_client.return_value.list_sessions.return_value = []
        from auto_coder.jules_engine import check_and_resume_or_archive_sessions

        check_and_resume_or_archive_sessions("owner/repo")

    cleanup.assert_called_once_with(github)


def test_unavailable_ledger_blocks_human_and_bot_prs(tmp_path: Path) -> None:
    ledger = Mock()
    ledger.list_issue_numbers.side_effect = OSError("offline")
    adapter = Mock()
    adapter.store.has_retired_pr_association.return_value = True
    adapter.github.get_pull_request_metadata_strict.side_effect = [
        {"number": 50, "user": {"login": "octocat", "type": "User"}},
        {"number": 51, "user": {"login": "automation[bot]", "type": "Bot"}},
    ]
    classifier = RefreshingSpeculativeClassifier(adapter, ledger)

    _, ordinary = classifier.classify_pr("owner/repo", "owner/repo", 50)
    _, ambiguous = classifier.classify_pr("owner/repo", "owner/repo", 51)

    assert ordinary.classification is ArtifactClassification.BLOCKED
    assert ordinary.mutation_allowed is False
    assert ambiguous.classification is ArtifactClassification.BLOCKED
    assert ambiguous.mutation_allowed is False
    adapter.observe_candidate.assert_not_called()


def test_empty_ledger_does_not_infer_unrelated_ownership_from_empty_history() -> None:
    adapter = Mock()
    adapter.store.has_retired_pr_association.return_value = False
    ledger = Mock(list_issue_numbers=Mock(return_value=()))

    _, result = RefreshingSpeculativeClassifier(adapter, ledger).classify_pr("owner/repo", "owner/repo", 50)
    assert result.classification is ArtifactClassification.BLOCKED
    assert result.mutation_allowed is False


def test_recreated_empty_history_fences_ambiguous_pr_at_production_boundaries(tmp_path: Path, monkeypatch) -> None:
    """REQ-002/006: two empty stores cannot authorize ingress or merge."""
    from auto_coder.jules_candidate_observation import CandidateObservationStore, JulesCandidateObservationAdapter
    from auto_coder.jules_competition_ledger import JulesCompetitionLedger
    from auto_coder.merge_operation_state import MergeOperationStore
    from auto_coder.pr_processor import _merge_pr
    from auto_coder.speculative_jules_lifecycle import RetiredJulesReader

    monkeypatch.setenv("HOME", str(tmp_path))
    repository = "owner/repo"
    pr = {
        "number": 59,
        "state": "open",
        "body": "",
        "changed_files": 1,
        "user": {"login": "octocat", "type": "User"},
        "head": {"repo": {"full_name": repository}, "ref": "ambiguous", "sha": "head-59"},
        "base": {"repo": {"full_name": repository}, "ref": "main", "sha": "base-sha"},
    }
    ledger = JulesCompetitionLedger(tmp_path / "empty-ledger.db")
    store = CandidateObservationStore(tmp_path / "empty-observations.db")
    github = Mock(token="token")
    service = SpeculativeJulesLifecycle(
        RefreshingSpeculativeClassifier(
            JulesCandidateObservationAdapter(ledger, store, RetiredJulesReader(), github),
            ledger,
        ),
        SpeculativeCleanupStore(tmp_path / "cleanup-empty.db"),
    )
    configure_speculative_jules_lifecycle(service)
    try:
        ingress = process_pull_request(github, AutomationConfig(), repository, dict(pr))
        assert ingress.outcome is PRProcessingOutcome.DEFERRED
        assert "cannot be reconciled from empty history" in ingress.actions_taken[0]

        metadata_api = Mock()
        metadata_api.pulls.get.return_value = dict(pr)
        outbound_api = Mock()
        with (
            patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=metadata_api),
            patch("auto_coder.merge_operation_adapter.get_ghapi_client", return_value=outbound_api),
            patch(
                "auto_coder.merge_operation_state.get_merge_operation_store",
                return_value=MergeOperationStore(db_path=tmp_path / "merge-operations-empty.db"),
            ),
            patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
            patch("auto_coder.pr_processor._resolve_pr_issue_numbers", return_value=[]),
        ):
            assert _merge_pr(repository, 59, {}, AutomationConfig(), github_client=github) is False
        outbound_api.pulls.merge.assert_not_called()
    finally:
        configure_speculative_jules_lifecycle(None)


def test_unavailable_ledger_never_infers_unrelated_ownership_from_absence() -> None:
    adapter = Mock()
    adapter.store.has_retired_pr_association.return_value = False
    ledger = Mock(list_issue_numbers=Mock(side_effect=OSError("offline")))

    _, result = RefreshingSpeculativeClassifier(adapter, ledger).classify_pr("owner/repo", "owner/repo", 50)
    assert result.classification is ArtifactClassification.BLOCKED
    assert result.mutation_allowed is False


def test_unavailable_ledger_allows_durably_attributed_codex_pr_at_production_boundaries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """REQ-005/006: durable Codex proof survives ingress and final merge fences."""
    from auto_coder.automation_config import EmptyPRResult
    from auto_coder.cloud_run import CloudRun, CloudRunRepository
    from auto_coder.codex_pr_attribution import AttributionDisposition, CodexPrAttributionRepository
    from auto_coder.jules_candidate_observation import CandidateObservationStore, JulesCandidateObservationAdapter
    from auto_coder.merge_operation_state import MergeOperationStore
    from auto_coder.pr_processor import _merge_pr
    from auto_coder.speculative_jules_lifecycle import RetiredJulesReader

    monkeypatch.setenv("HOME", str(tmp_path))
    repository = "owner/repo"
    pr_number = 60
    issue_number = 605
    CloudRunRepository(repository).save(
        CloudRun(
            repository,
            issue_number,
            0,
            "codex-cloud",
            task_id="task_e_verified60",
            backend_name="codex-cloud",
            submission_outcome="accepted",
            launch_identity="launch-60",
            publication_head_repository=repository,
            publication_head_ref="codex-feature",
        )
    )
    pr = {
        "number": pr_number,
        "state": "open",
        "body": "Closes #605\nhttps://chatgpt.com/codex/tasks/task_e_verified60",
        "changed_files": 0,
        "user": {"login": "octocat", "type": "User"},
        "head": {"repo": {"full_name": repository}, "ref": "codex-feature", "sha": "head-60"},
        "base": {"repo": {"full_name": repository}, "ref": "main", "sha": "base-sha"},
    }

    class UnavailableLedger:
        def list_issue_numbers(self, _repository: str) -> tuple[int, ...]:
            raise OSError("offline")

    github = Mock(token="token")
    adapter = JulesCandidateObservationAdapter(
        UnavailableLedger(),
        CandidateObservationStore(tmp_path / "observations.db"),
        RetiredJulesReader(),
        github,
    )
    service = SpeculativeJulesLifecycle(
        RefreshingSpeculativeClassifier(adapter, UnavailableLedger()),
        SpeculativeCleanupStore(tmp_path / "cleanup.db"),
    )
    configure_speculative_jules_lifecycle(service)
    try:
        with (
            patch(
                "auto_coder.pr_processor._reject_unsafe_codex_cloud_pr",
                return_value=SimpleNamespace(closed=False, metadata_error=None, authoritative_pr_data=None),
            ),
            patch("auto_coder.pr_processor._link_codex_cloud_pr_to_issue", return_value=None),
            patch(
                "auto_coder.pr_processor._close_empty_pr",
                return_value=EmptyPRResult(closed=True, actions=["ordinary empty-PR effect"]),
            ) as empty_effect,
        ):
            ingress = process_pull_request(github, AutomationConfig(), repository, dict(pr))
        assert ingress.priority == "close"
        assert ingress.actions_taken == ["ordinary empty-PR effect"]
        empty_effect.assert_called_once()
        assert CodexPrAttributionRepository(repository).get(pr_number).disposition is AttributionDisposition.VERIFIED

        metadata_api = Mock()
        metadata_api.pulls.get.return_value = dict(pr)
        outbound_api = Mock()
        outbound_api.pulls.merge.return_value = {"merged": True, "sha": "merged-sha"}
        with (
            patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=metadata_api),
            patch("auto_coder.merge_operation_adapter.get_ghapi_client", return_value=outbound_api),
            patch(
                "auto_coder.merge_operation_state.get_merge_operation_store",
                return_value=MergeOperationStore(db_path=tmp_path / "merge-operations-codex.db"),
            ),
            patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
            patch("auto_coder.pr_processor._resolve_pr_issue_numbers", return_value=[issue_number]),
            patch("auto_coder.pr_processor._close_linked_issues"),
            patch("auto_coder.pr_processor._archive_jules_session"),
        ):
            github.get_issue.return_value = {"state": "open", "body": "ordinary codex issue"}
            assert _merge_pr(repository, pr_number, {}, AutomationConfig(), github_client=github) is True
        outbound_api.pulls.merge.assert_called_once_with("owner", "repo", pr_number, merge_method="squash", sha="head-60")
    finally:
        configure_speculative_jules_lifecycle(None)


def test_selected_association_does_not_override_an_unreadable_namespace() -> None:
    ledger = Mock()
    ledger.list_issue_numbers.return_value = (7, 8)
    adapter = Mock()
    adapter.classify.side_effect = [
        ClassificationResult(ArtifactClassification.SELECTED, "c1", "g1"),
        OSError("unreadable namespace"),
    ]
    classifier = RefreshingSpeculativeClassifier(adapter, ledger)

    _, result = classifier.classify_pr("owner/repo", "owner/repo", 41)

    assert result.classification is ArtifactClassification.BLOCKED


def test_restarted_persisted_generation_fences_ingress_and_final_merge_transport(tmp_path: Path) -> None:
    """REQ-006: exercise both production boundaries from durable retired state."""
    from auto_coder.jules_candidate_observation import (
        CandidateObservation,
        CandidateObservationStore,
        EvidenceStatus,
        JulesCandidateObservationAdapter,
        PullRequestIdentity,
        VerifiedPullRequest,
    )
    from auto_coder.jules_competition_ledger import (
        AcceptanceRecord,
        CandidateBindingObservation,
        JulesCompetitionLedger,
        SpeculativeGenerationBundle,
    )
    from auto_coder.pr_processor import _merge_pr
    from auto_coder.speculative_jules_lifecycle import RetiredJulesReader

    repository = "owner/repo"
    issue_number = 7
    ledger_path = tmp_path / "competition.db"
    observation_path = tmp_path / "observations.db"
    ledger = JulesCompetitionLedger(ledger_path)
    created = ledger.create_generation(
        repository,
        issue_number,
        "create",
        0,
        SpeculativeGenerationBundle(
            candidate_ids=("selected", "loser"),
            issue_oracle_snapshot="requirements-v1",
            issue_oracle_fingerprint="oracle-v1",
            source_branch="main",
        ),
    )
    generation_id = created.generation_id
    assert generation_id is not None
    epoch = created.snapshot.epoch
    store = CandidateObservationStore(observation_path)
    for candidate_id, pr_number in (("selected", 41), ("loser", 42)):
        claimed = ledger.claim_candidate_submission(repository, issue_number, generation_id, candidate_id, f"claim-{candidate_id}", epoch)
        accepted = ledger.record_candidate_accepted(
            repository,
            issue_number,
            generation_id,
            candidate_id,
            f"accept-{candidate_id}",
            claimed.snapshot.epoch,
            "jules",
            f"session-{candidate_id}",
        )
        epoch = accepted.snapshot.epoch
        scope = store.scope(repository, issue_number, generation_id, candidate_id)
        read_id = store.begin_read(scope)
        artifact = VerifiedPullRequest(
            PullRequestIdentity(repository, pr_number),
            repository,
            f"candidate-{candidate_id}",
            f"head-{pr_number}",
            repository,
            "main",
            "base-sha",
        )
        assert store.publish(
            scope,
            CandidateObservation(
                repository,
                issue_number,
                generation_id,
                candidate_id,
                "jules",
                f"session-{candidate_id}",
                read_id,
                evidence_status=EvidenceStatus.KNOWN,
                artifacts=(artifact,),
            ),
        )
        bound = ledger.record_candidate_binding(
            repository,
            issue_number,
            generation_id,
            candidate_id,
            f"bind-{candidate_id}",
            epoch,
            CandidateBindingObservation(repository, pr_number, f"head-{pr_number}", "base-sha"),
        )
        epoch = bound.snapshot.epoch

    selected = ledger.select_winner(
        repository,
        issue_number,
        generation_id,
        "selected",
        "select",
        epoch,
        AcceptanceRecord(
            repository,
            generation_id,
            "selected",
            repository,
            41,
            "head-41",
            "base-sha",
            "oracle-v1",
            1,
            1,
            0,
        ),
    )
    assert selected.selected

    # Reopen both durable stores to model daemon restart. The retired reader is
    # deliberately incapable of Jules GET/list traffic.
    strict_prs = {
        41: {
            "number": 41,
            "body": "Closes #7",
            "user": {"login": "google-labs-jules[bot]", "type": "Bot"},
            "head": {"repo": {"full_name": repository}, "ref": "candidate-selected", "sha": "changed-head"},
            "base": {"repo": {"full_name": repository}, "ref": "main", "sha": "base-sha"},
        },
        42: {
            "number": 42,
            "body": "Closes #7",
            "changed_files": 1,
            "user": {"login": "google-labs-jules[bot]", "type": "Bot"},
            "head": {"repo": {"full_name": repository}, "ref": "candidate-loser", "sha": "head-42"},
            "base": {"repo": {"full_name": repository}, "ref": "main", "sha": "base-sha"},
        },
        50: {
            "number": 50,
            "state": "open",
            "body": "",
            "changed_files": 1,
            "user": {"login": "octocat", "type": "User"},
            "head": {"repo": {"full_name": repository}, "ref": "ordinary", "sha": "head-50"},
            "base": {"repo": {"full_name": repository}, "ref": "main", "sha": "base-sha"},
        },
        51: {
            "number": 51,
            "body": "",
            "changed_files": 1,
            "user": {"login": "other-automation[bot]", "type": "Bot"},
            "head": {"repo": {"full_name": repository}, "ref": "unknown", "sha": "head-51"},
            "base": {"repo": {"full_name": repository}, "ref": "main", "sha": "base-sha"},
        },
    }
    github = Mock(token="token")
    github.get_pull_request_metadata_strict.side_effect = lambda _repo, number: strict_prs[number]
    restarted_ledger = JulesCompetitionLedger(ledger_path)
    retired_reader = Mock(wraps=RetiredJulesReader())
    restarted_adapter = JulesCandidateObservationAdapter(
        restarted_ledger,
        CandidateObservationStore(observation_path),
        retired_reader,
        github,
    )
    restarted = SpeculativeJulesLifecycle(
        RefreshingSpeculativeClassifier(restarted_adapter, restarted_ledger),
        SpeculativeCleanupStore(tmp_path / "cleanup.db"),
    )

    configure_speculative_jules_lifecycle(restarted)
    try:
        enqueue_issue = Mock()
        enqueue_pr = Mock()
        assert consume_due_speculative_work(repository, github, enqueue_issue, enqueue_pr) == (0, 0)
        enqueue_issue.assert_not_called()
        enqueue_pr.assert_not_called()
        github.close_pr.assert_not_called()
        green_ci = Mock(success=True, in_progress=False)
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=green_ci) as check_ci:
            loser = process_pull_request(github, AutomationConfig(), repository, strict_prs[42])
        assert loser.outcome is PRProcessingOutcome.DEFERRED
        assert "retired artifact fenced" in loser.actions_taken[0]
        # Even a green retired loser is fenced before CI can grant authority.
        check_ci.assert_not_called()
        github.close_pr.assert_not_called()

        # A durable winner whose accepted head no longer matches is fenced at
        # ingress, before CI, review, or any other ordinary PR effect.
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=green_ci) as check_ci:
            stale_selected = process_pull_request(github, AutomationConfig(), repository, strict_prs[41])
        assert stale_selected.outcome is PRProcessingOutcome.DEFERRED
        assert "stale or invalidated" in stale_selected.actions_taken[0]
        check_ci.assert_not_called()

        # Mixed input: strict human identity proves this PR unrelated while the
        # candidate remains fenced by the same restarted lifecycle.
        _, ordinary = restarted.classifier.classify_pr(repository, repository, 50)
        assert ordinary.classification is ArtifactClassification.LEGACY
        ordinary_result = process_pull_request(github, AutomationConfig(), repository, strict_prs[50])
        assert all("Speculative Jules" not in action for action in ordinary_result.actions_taken)
        assert process_pull_request(github, AutomationConfig(), repository, strict_prs[51]).outcome is PRProcessingOutcome.DEFERRED

        from auto_coder.merge_operation_state import MergeOperationStore

        metadata_api = Mock()
        metadata_api.pulls.get.side_effect = lambda _owner, _repo, number: strict_prs[number]
        outbound_api = Mock()
        outbound_api.pulls.merge.return_value = {"merged": True, "sha": "merged-sha"}
        merge_store = MergeOperationStore(db_path=tmp_path / "merge-operations.db")
        with (
            patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=metadata_api),
            patch("auto_coder.merge_operation_adapter.get_ghapi_client", return_value=outbound_api),
            patch("auto_coder.merge_operation_state.get_merge_operation_store", return_value=merge_store),
            patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
            patch("auto_coder.pr_processor._resolve_pr_issue_numbers", return_value=[issue_number]),
            patch("auto_coder.pr_processor._close_linked_issues"),
            patch("auto_coder.pr_processor._archive_jules_session"),
        ):
            github.get_issue.return_value = {"state": "open", "body": "requirements-v1"}
            # Both historical artifacts reach the real final entry and are
            # refused before the durable adapter can dispatch a mutation.
            assert _merge_pr(repository, 42, {}, AutomationConfig(), github_client=github) is False
            assert _merge_pr(repository, 41, {}, AutomationConfig(), github_client=github) is False
            assert _merge_pr(repository, 51, {}, AutomationConfig(), github_client=github) is False
            outbound_api.pulls.merge.assert_not_called()

            # The proven-unrelated control traverses the same final gate and
            # reaches the actual durable merge adapter transport exactly once.
            assert _merge_pr(repository, 50, {}, AutomationConfig(), github_client=github) is True

        outbound_api.pulls.merge.assert_called_once_with("owner", "repo", 50, merge_method="squash", sha="head-50")

        # A surviving ledger binding must remain authoritative even if the
        # separate observation database has been lost after restart.
        missing_observations = JulesCandidateObservationAdapter(restarted_ledger, CandidateObservationStore(tmp_path / "missing.db"), RetiredJulesReader(), github)
        configure_speculative_jules_lifecycle(
            SpeculativeJulesLifecycle(
                RefreshingSpeculativeClassifier(missing_observations, restarted_ledger),
                SpeculativeCleanupStore(tmp_path / "cleanup.db"),
            )
        )
        strict_prs[42]["user"] = {"login": "octocat", "type": "User"}
        assert process_pull_request(github, AutomationConfig(), repository, strict_prs[42]).outcome is PRProcessingOutcome.DEFERRED
        with (
            patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=metadata_api),
            patch("auto_coder.merge_operation_adapter.get_ghapi_client", return_value=outbound_api),
            patch("auto_coder.merge_operation_state.get_merge_operation_store", return_value=merge_store),
            patch("auto_coder.pr_processor._resolve_pr_issue_numbers", return_value=[issue_number]),
        ):
            assert _merge_pr(repository, 42, {}, AutomationConfig(), github_client=github) is False
        outbound_api.pulls.merge.assert_called_once()

        # Neither a strict human author nor an empty replacement ledger proves
        # that the retained candidate is unrelated. Remove its Issue hint to
        # ensure the persisted observation, rather than request metadata, is
        # what an empty replacement ledger would otherwise lose.
        strict_prs[42]["body"] = ""

        class UnavailableLedger:
            def list_issue_numbers(self, _repository: str) -> tuple[int, ...]:
                raise OSError("offline")

        assert restarted_adapter.store.has_retired_pr_association(repository, repository, 42) is True
        replacement_empty_ledger = JulesCompetitionLedger(tmp_path / "replacement-empty-ledger.db")
        assert replacement_empty_ledger.list_issue_numbers(repository) == ()
        for uncertain_ledger in (UnavailableLedger(), replacement_empty_ledger):
            configure_speculative_jules_lifecycle(
                SpeculativeJulesLifecycle(
                    RefreshingSpeculativeClassifier(restarted_adapter, uncertain_ledger),
                    SpeculativeCleanupStore(tmp_path / "cleanup.db"),
                )
            )
            assert process_pull_request(github, AutomationConfig(), repository, strict_prs[42]).outcome is PRProcessingOutcome.DEFERRED
            with (
                patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=metadata_api),
                patch("auto_coder.merge_operation_adapter.get_ghapi_client", return_value=outbound_api),
                patch("auto_coder.merge_operation_state.get_merge_operation_store", return_value=merge_store),
                patch("auto_coder.pr_processor._resolve_pr_issue_numbers", return_value=[]),
            ):
                assert _merge_pr(repository, 42, {}, AutomationConfig(), github_client=github) is False
        outbound_api.pulls.merge.assert_called_once()
    finally:
        configure_speculative_jules_lifecycle(None)

    github.close_pr.assert_not_called()
    retired_reader.get_session.assert_not_called()
