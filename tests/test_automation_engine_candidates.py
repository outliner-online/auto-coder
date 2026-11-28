from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, StaleJulesPRResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.pr_automation_guard import PRCommit, PRCommitHistory
from tests.ci_observation_fakes import authoritative_ci_status


class TestAutomationEngineCandidates:
    """Test cases for _get_candidates method in AutomationEngine."""

    def test_get_candidates_unknown_jules_draft_pr_is_fenced(self, mock_github_client):
        """An unknown historical candidate must remain draft and unprocessed."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        repo_name = "owner/repo"

        # Mock PR data
        pr_data = {"number": 123, "title": "Jules PR", "draft": True, "head": {"ref": "jules-branch", "sha": "a" * 40}, "labels": [], "body": "Session ID: abc", "created_at": "2023-01-01T00:00:00Z", "node_id": "PR_123"}

        # Mock get_open_prs_json to return the PR data
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_automation_history_strict.return_value = PRCommitHistory(pr_data["head"]["sha"], 1, (PRCommit(pr_data["head"]["sha"], "Existing candidate"),))

        # Mock _is_jules_pr to return True

        # Mock other dependencies to avoid errors
        with (
            patch("auto_coder.automation_engine.LabelManager") as mock_label_manager,
            patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress") as mock_check_actions,
            patch("auto_coder.util.github_action._check_github_actions_status") as mock_check_status,
        ):

            mock_label_manager.return_value.__enter__.return_value = True
            mock_check_actions.return_value = True
            mock_check_status.side_effect = authoritative_ci_status(success=True, ids=[1])

            # Execute
            candidates = engine._get_candidates(repo_name)

        mock_github_client.graphql_query.assert_not_called()
        assert pr_data["draft"] is True
        assert candidates == []

    def test_get_candidates_unknown_jules_ready_pr_is_fenced(self, mock_github_client):
        """Ready status does not prove historical selection or unrelated origin."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        repo_name = "owner/repo"

        # Mock PR data
        pr_data = {"number": 123, "title": "Jules PR", "draft": False, "head": {"ref": "jules-branch", "sha": "a" * 40}, "labels": [], "body": "Session ID: abc", "created_at": "2023-01-01T00:00:00Z"}  # Already ready

        # Mock get_open_prs_json to return the PR data
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_automation_history_strict.return_value = PRCommitHistory(pr_data["head"]["sha"], 1, (PRCommit(pr_data["head"]["sha"], "Existing candidate"),))

        # Mock _is_jules_pr to return True

        # Mock other dependencies
        with (
            patch("auto_coder.automation_engine.LabelManager") as mock_label_manager,
            patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress") as mock_check_actions,
            patch("auto_coder.util.github_action._check_github_actions_status") as mock_check_status,
        ):

            mock_label_manager.return_value.__enter__.return_value = True
            mock_check_actions.return_value = True
            mock_check_status.side_effect = authoritative_ci_status(success=True, ids=[1])

            # Execute
            candidates = engine._get_candidates(repo_name)

        # Assert
        # Verify gh pr ready was NOT called
        mock_github_client.graphql_query.assert_not_called()

        # Verify candidate was created
        assert candidates == []

    def test_get_candidates_non_jules_draft_pr(self, mock_github_client):
        """Test that non-Jules draft PRs are NOT marked as ready."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        repo_name = "owner/repo"

        # Mock PR data
        pr_data = {"number": 123, "title": "Regular PR", "draft": True, "head": {"ref": "feature-branch", "sha": "a" * 40}, "labels": [], "body": "<!-- auto-coder:local-llm -->\n\nDescription", "created_at": "2023-01-01T00:00:00Z"}

        # Mock get_open_prs_json to return the PR data
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_automation_history_strict.return_value = PRCommitHistory(pr_data["head"]["sha"], 1, (PRCommit(pr_data["head"]["sha"], "Existing candidate"),))

        # Mock _is_jules_pr to return False

        # Mock other dependencies
        with (
            patch("auto_coder.automation_engine.LabelManager") as mock_label_manager,
            patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress") as mock_check_actions,
            patch("auto_coder.util.github_action._check_github_actions_status") as mock_check_status,
        ):

            mock_label_manager.return_value.__enter__.return_value = True
            mock_check_actions.return_value = True
            mock_check_status.side_effect = authoritative_ci_status(success=True, ids=[1])

            # Execute
            candidates = engine._get_candidates(repo_name)

        # Assert
        # Verify gh pr ready was NOT called
        mock_github_client.graphql_query.assert_not_called()

        # Verify pr_data was NOT updated
        assert pr_data["draft"] is True

        # Verify candidate was created
        assert len(candidates) == 1

    def test_get_candidates_sub_issues_with_self_referencing_parent(self, mock_github_client):
        """Test that sub-issue #5032 is chosen when parent #5031 has self-mentioning body."""
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        repo_name = "owner/repo"

        mock_github_client.get_open_prs_json.return_value = []
        mock_github_client.get_open_issues_json.return_value = [
            {
                "number": 5031,
                "title": "Parent Issue",
                "body": "Each child issue declares Parent-Issue: #5031",
                "state": "open",
                "labels": ["difficult"],
                "created_at": "2023-01-01T00:00:00Z",
                "author": "owner",
                "has_open_sub_issues": True,
                "open_sub_issue_numbers": [5032, 5033],
                "parent_issue_number": None,
                "linked_pr_numbers": [],
            },
            {
                "number": 5032,
                "title": "First Child Issue",
                "body": "Parent-Issue: #5031",
                "state": "open",
                "labels": ["difficult"],
                "created_at": "2023-01-01T00:01:00Z",
                "author": "owner",
                "has_open_sub_issues": False,
                "open_sub_issue_numbers": [],
                "parent_issue_number": 5031,
                "linked_pr_numbers": [],
            },
            {
                "number": 5033,
                "title": "Second Child Issue",
                "body": "Parent-Issue: #5031",
                "state": "open",
                "labels": ["difficult"],
                "created_at": "2023-01-01T00:02:00Z",
                "author": "owner",
                "has_open_sub_issues": False,
                "open_sub_issue_numbers": [],
                "parent_issue_number": 5031,
                "linked_pr_numbers": [],
            },
        ]

        with patch("auto_coder.automation_engine.LabelManager") as mock_label_manager:
            mock_label_manager.return_value.__enter__.return_value = True
            candidates = engine._get_candidates(repo_name)

        # 5031 has open sub issues -> skipped
        # 5032 has parent 5031, elder siblings [] -> chosen
        # 5033 has parent 5031, elder siblings [5032] -> skipped
        assert [candidate.data["number"] for candidate in candidates] == [5032, 5033]
