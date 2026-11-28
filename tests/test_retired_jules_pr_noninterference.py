"""Regression coverage for retired Jules PR-specific automation (Issue #2898)."""

from unittest.mock import MagicMock

from auto_coder import pr_processor


def test_retired_provider_bot_cannot_gain_local_ownership_from_text():
    pr = {
        "user": {"login": "google-labs-jules[bot]"},
        "head": {"ref": "issue-2898_attempt-0"},
        "title": "Auto-Coder implementation",
        "body": "<!-- auto-coder:local-llm -->\nThis PR addresses issue #2898",
    }

    assert pr_processor._is_dependabot_pr(pr) is True
    assert pr_processor._is_local_llm_pr(pr) is False


def test_quoted_retired_provider_urls_are_not_session_attribution():
    quoted = "> Historical reference: https://jules.google.com/session/not-an-owner"
    candidates = pr_processor._extract_session_id_candidates(quoted)

    assert candidates == []


def test_supported_provider_session_parsing_remains_available():
    candidates = pr_processor._extract_session_id_candidates("Implementation session: https://claude.ai/code/session_supported")

    assert ("Pattern 3a (Claude Session URL)", "session_supported") in candidates


def test_retired_pr_entry_points_are_removed():
    for name in (
        "_is_jules_pr",
        "_should_skip_waiting_for_jules",
        "_close_stale_jules_pr",
        "_send_jules_error_feedback",
        "_process_pr_jules_mode",
        "_link_jules_pr_to_issue",
        "_archive_jules_session",
    ):
        assert not hasattr(pr_processor, name)


def test_retired_bot_origin_is_only_negative_speculative_fence_evidence():
    lifecycle = MagicMock()
    lifecycle.evaluate_pr.return_value.allow_ordinary_processing = False
    github = MagicMock()
    pr = {"number": 42, "user": {"login": "google-labs-jules[bot]"}, "body": ""}

    original = pr_processor.get_speculative_jules_lifecycle
    factory = MagicMock(return_value=lifecycle)
    try:
        pr_processor.get_speculative_jules_lifecycle = factory
        pr_processor.evaluate_retired_jules_ingress(github, "owner/repo", pr, refresh_metadata=False)
    finally:
        pr_processor.get_speculative_jules_lifecycle = original

    factory.assert_called_once_with(github, possible_retired_ownership=True)
    lifecycle.evaluate_pr.assert_called_once()
