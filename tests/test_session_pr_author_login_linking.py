"""Author parsing and supported-provider linking regressions."""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.cloud_manager import CloudTaskBinding
from auto_coder.pr_processor import _link_supported_provider_pr_to_issue
from auto_coder.utils import get_pr_author_login


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"author": "plain", "user": {"login": "fallback"}}, "plain"),
        ({"author": {"login": "nested"}, "user": {"login": "fallback"}}, "nested"),
        ({"author": None, "user": {"login": "fallback"}}, "fallback"),
        ({"author": None, "user": None}, None),
    ],
)
def test_get_pr_author_login_handles_supported_shapes(metadata, expected):
    assert get_pr_author_login(metadata) == expected


def test_nullable_author_does_not_block_claude_binding():
    github = MagicMock()
    pr = {"number": 22, "author": None, "user": None, "body": "https://claude.ai/code/session_abc"}
    with patch("auto_coder.pr_processor.CloudManager") as manager:
        manager.return_value.get_issues_by_session.return_value = [42]
        manager.return_value.get_binding.return_value = CloudTaskBinding(provider="claude-routine", task_id="session_abc")
        assert _link_supported_provider_pr_to_issue("owner/repo", pr, github)
    assert "close #42" in pr["body"]
