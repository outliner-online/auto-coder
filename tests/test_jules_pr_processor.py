"""Supported provider PR-to-Issue linking after Jules PR retirement."""

from unittest.mock import MagicMock, patch

from auto_coder.pr_processor import _link_supported_provider_pr_to_issue


def test_claude_binding_adds_issue_reference():
    github = MagicMock()
    github.get_repository.return_value.get_pull.return_value = MagicMock()
    pr = {"number": 17, "body": "https://claude.ai/code/session_supported", "user": {"login": "claude[bot]"}}
    with patch("auto_coder.pr_processor.CloudManager") as manager:
        manager.return_value.get_issues_by_session.return_value = [2898]
        manager.return_value.get_issue_by_session.return_value = 2898
        assert _link_supported_provider_pr_to_issue("owner/repo", pr, github)
    assert "close #2898" in pr["body"]


def test_retired_url_does_not_enter_supported_linker():
    github = MagicMock()
    original = "Quoted: https://jules.google.com/session/legacy"
    pr = {"number": 18, "body": original, "user": {"login": "human"}}
    assert _link_supported_provider_pr_to_issue("owner/repo", pr, github)
    assert pr["body"] == original
    github.get_repository.assert_not_called()
