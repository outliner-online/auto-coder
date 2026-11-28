"""Production regressions for retired Jules wait and timeout policy."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_processor import process_pull_request
from auto_coder.speculative_jules_lifecycle import LifecycleDecision
from tests.ci_observation_fakes import authoritative_ci_status
from tests.pr_automation_fakes import install_pr_history


@pytest.mark.parametrize("ci_success", [True, False], ids=["green-control", "completed-failure"])
def test_old_wait_and_timeout_inputs_have_no_provider_or_retry_effects(ci_success):
    github = MagicMock()
    wait_comment = {
        "body": "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates.",
        "created_at": "2020-01-02T00:00:00Z",
    }
    github.get_pr_comments.return_value = [wait_comment]
    github.get_pr_commits.return_value = [{"commit": {"committer": {"date": "2020-01-01T12:00:00Z"}}}]
    pr = {
        "number": 4643,
        "title": "Historical provider output",
        "body": "https://jules.google.com/session/legacy\n\nCloses #4636",
        "created_at": "2020-01-01T00:00:00Z",
        "state": "open",
        "labels": [],
        "user": {"login": "google-labs-jules[bot]"},
        "head": {"ref": "issue-4636", "sha": "head"},
        "base": {"ref": "main", "sha": "base"},
        "changed_files": 1,
    }
    install_pr_history(github, "head")
    with (
        patch("auto_coder.pr_processor.evaluate_retired_jules_ingress", return_value=LifecycleDecision()),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr", return_value=SimpleNamespace(closed=False, metadata_error=None, actions=(), authoritative_pr_data=pr)),
        patch("auto_coder.pr_processor._link_codex_cloud_pr_to_issue", return_value=None),
        patch("auto_coder.pr_processor._close_empty_pr", return_value=SimpleNamespace(closed=False)),
        patch("auto_coder.pr_processor.LabelManager") as labels,
        patch(
            "auto_coder.pr_processor._check_github_actions_status",
            side_effect=authoritative_ci_status(success=ci_success, ids=[771]),
        ),
        patch("auto_coder.pr_processor._handle_pr_merge", return_value=[]) as ordinary,
        patch("auto_coder.pr_processor.increment_attempt") as increment,
    ):
        labels.return_value.__enter__.return_value = True
        result = process_pull_request(github, AutomationConfig(), "owner/repo", pr)
    ordinary.assert_called_once()
    github.close_pr.assert_not_called()
    github.add_comment_to_pr.assert_not_called()
    increment.assert_not_called()
    assert result.actions_taken == []
