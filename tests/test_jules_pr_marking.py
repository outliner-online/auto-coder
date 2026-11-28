from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.automation_engine import AutomationConfig, AutomationEngine
from auto_coder.util.gh_cache import GitHubClient


class TestJulesPRMarking:

    @patch("auto_coder.automation_engine.get_ghapi_client")
    def test_unknown_jules_pr_is_not_marked_ready_or_fetched_for_mutation(self, mock_get_ghapi, mock_github_client):
        """Missing history must stop before draft-ready fallback or mutation."""

        # Setup mocks
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        # Mock GhApi client for fallback fetch
        mock_ghapi_instance = Mock()
        mock_get_ghapi.return_value = mock_ghapi_instance
        mock_github_client.api_for.return_value = mock_ghapi_instance

        # Mock PR data - initially Jules PR, Draft, missing node_id
        pr_data = {
            "number": 123,
            "title": "Jules PR",
            "body": "I started a Jules session to work on this issue. Session ID: 123",  # Required for _is_jules_pr check
            "draft": True,
            "labels": [],
            "created_at": "2023-01-01T00:00:00Z",
            "head": {"ref": "jules-branch"},
            # "node_id": missing
        }

        # Prepare get_open_prs_json to return our PR
        mock_github_client.get_open_prs_json.return_value = [pr_data]

        # Mock fallback fetch response
        mock_ghapi_instance.pulls.get.return_value = {"node_id": "PR_NODE_ID_123"}

        # Execute the method that triggers the logic
        # _get_candidates is called internally. We need to call a method that calls it.
        # But _get_candidates is private. We can test it directly if we access it,
        # or we mock essential parts and call `engine.run` or similar?
        # Actually _get_candidates logic is what we want to test.
        # Let's verify if we can invoke it via public API or just call it directly for this unit test.
        # Python allows calling private methods.

        # patch the source modules directly since they are imported inside the function
        with patch("auto_coder.util.github_action.preload_github_actions_status"), patch("auto_coder.pr_processor._is_jules_pr", return_value=True):

            candidates = engine._get_candidates("owner/repo")

        assert not candidates
        mock_ghapi_instance.pulls.get.assert_not_called()
        mock_github_client.graphql_query.assert_not_called()
        assert "node_id" not in pr_data
        assert pr_data["draft"] is True

    @patch("auto_coder.automation_engine.get_ghapi_client")
    def test_unknown_jules_pr_keeps_draft_even_with_node_id(self, mock_get_ghapi, mock_github_client):
        """A complete GitHub identity is not historical selection authority."""

        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        pr_data = {"number": 456, "title": "Jules PR", "body": "I started a Jules session...", "draft": True, "node_id": "EXISTING_NODE_ID", "head": {"ref": "jules/456"}, "labels": [], "created_at": "2023-01-01T00:00:00Z"}

        mock_github_client.get_open_prs_json.return_value = [pr_data]

        with patch("auto_coder.util.github_action.preload_github_actions_status"), patch("auto_coder.pr_processor._is_jules_pr", return_value=True):

            engine._get_candidates("owner/repo")

        # 1. Verify Fallback Fetch was NOT called
        mock_get_ghapi.return_value.pulls.get.assert_not_called()

        mock_github_client.graphql_query.assert_not_called()
        assert pr_data["draft"] is True
