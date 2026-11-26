"""Tests for the per-loop caching of the Jules session listing."""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.jules_client import JulesClient, invalidate_jules_sessions_cache


def _mock_llm_config(mock_get_config):
    mock_config = Mock()
    mock_backend_config = Mock()
    mock_backend_config.options = []
    mock_backend_config.options_for_noedit = []
    mock_backend_config.api_key = None
    mock_config.get_backend_config.return_value = mock_backend_config
    mock_get_config.return_value = mock_config


def _sessions_response():
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "sessions": [
            {"name": "sessions/s1", "state": "IN_PROGRESS", "sourceContext": {"source": "sources/github/owner/repo"}},
            {"name": "sessions/s2", "state": "COMPLETED", "sourceContext": {"source": "sources/github/owner/other"}},
        ]
    }
    return response


class TestJulesSessionCache:
    """The listing is fetched once and reused until explicitly invalidated."""

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.get")
    def test_repeated_list_sessions_performs_single_fetch(self, mock_get, mock_get_config):
        _mock_llm_config(mock_get_config)
        mock_get.side_effect = [_sessions_response()]

        client = JulesClient()
        first = client.list_sessions(repo_name="owner/repo")
        second = client.list_sessions(repo_name="owner/repo")

        assert mock_get.call_count == 1
        assert [s["name"] for s in first] == ["sessions/s1"]
        assert first == second

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.get")
    def test_cache_is_shared_between_client_instances(self, mock_get, mock_get_config):
        _mock_llm_config(mock_get_config)
        mock_get.side_effect = [_sessions_response()]

        JulesClient().list_sessions(repo_name="owner/repo")
        JulesClient().list_sessions(repo_name="owner/repo")

        assert mock_get.call_count == 1

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.get")
    def test_different_repo_filters_reuse_the_same_fetch(self, mock_get, mock_get_config):
        _mock_llm_config(mock_get_config)
        mock_get.side_effect = [_sessions_response()]

        client = JulesClient()
        unfiltered = client.list_sessions()
        filtered = client.list_sessions(repo_name="owner/other")

        assert mock_get.call_count == 1
        assert [s["name"] for s in unfiltered] == ["sessions/s1", "sessions/s2"]
        assert [s["name"] for s in filtered] == ["sessions/s2"]

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.get")
    def test_invalidation_triggers_a_new_fetch(self, mock_get, mock_get_config):
        _mock_llm_config(mock_get_config)
        mock_get.side_effect = [_sessions_response(), _sessions_response()]

        client = JulesClient()
        client.list_sessions(repo_name="owner/repo")
        invalidate_jules_sessions_cache()
        client.list_sessions(repo_name="owner/repo")

        assert mock_get.call_count == 2

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.get")
    def test_failed_fetch_is_cached_until_next_maintenance_cycle(self, mock_get, mock_get_config):
        _mock_llm_config(mock_get_config)
        error_response = Mock()
        error_response.status_code = 500
        error_response.text = "boom"
        mock_get.side_effect = [error_response, _sessions_response()]

        client = JulesClient()
        try:
            client.list_sessions(repo_name="owner/repo")
            raise AssertionError("Expected RuntimeError")
        except RuntimeError:
            pass

        with pytest.raises(RuntimeError, match="boom"):
            client.list_sessions(repo_name="owner/repo")

        assert mock_get.call_count == 1

        invalidate_jules_sessions_cache()
        sessions = client.list_sessions(repo_name="owner/repo")

        assert mock_get.call_count == 2
        assert [s["name"] for s in sessions] == ["sessions/s1"]
