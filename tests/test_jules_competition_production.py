from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_coder.issue_processor import _dispatch_jules_competition, _process_issue_jules_mode
from auto_coder.jules_candidate_submission import CandidateSubmissionOutcome, CandidateSubmissionResult
from auto_coder.jules_competition_ledger import GenerationRetirementSource, JulesCompetitionLedger


def config(width: object = 3, validation: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        JULES_SPECULATIVE_PARALLELISM=width,
        JULES_ISSUE_PR_TIMEOUT_HOURS=12,
        JULES_PR_CI_TIMEOUT_HOURS=12,
        MAIN_BRANCH="main",
        pr_adversarial_validation=validation,
    )


@patch("auto_coder.issue_processor.JulesClient")
def test_invalid_runtime_width_is_reported_before_provider_dispatch(client_class: Mock) -> None:
    actions = _process_issue_jules_mode("owner/repo", {"number": 7, "title": "T"}, config(True), Mock())  # type: ignore[arg-type]
    assert actions == ["Error processing issue #7 in Jules mode: [jules].speculative_parallelism must be a positive integer (booleans are not valid)"]
    client_class.assert_not_called()


def test_competition_dispatch_is_retired_without_ledger_or_provider_effects(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    client = Mock()
    actions = _dispatch_jules_competition("owner/repo", {"number": 7}, config(), "task", client, None, None)  # type: ignore[arg-type]
    assert actions == ["Deferred retired Jules competition for issue #7: speculative execution is disabled"]
    client.start_session.assert_not_called()
    assert JulesCompetitionLedger().get_namespace_snapshot("owner/repo", 7).generations == ()
