"""Session parsing regressions after Jules PR automation retirement."""

from auto_coder.pr_processor import _extract_session_id_candidates, _extract_session_id_from_pr_body


def test_supported_claude_session_is_retained():
    body = "https://claude.ai/code/session_supported"
    assert _extract_session_id_from_pr_body(body) == "session_supported"


def test_retired_jules_urls_are_not_candidates():
    body = "> https://jules.google.com/session/legacy\n> https://jules.google.com/task/legacy"
    assert _extract_session_id_candidates(body) == []
    assert _extract_session_id_from_pr_body(body) is None
