"""Current review context distinguishes absent parents from unavailable evidence."""

from unittest.mock import MagicMock

import pytest

from auto_coder.github_request_governor import GitHubRequestDeferred
from auto_coder.issue_context import IssueOracleResolution, VerifiedIssueOracle, get_linked_issues_context
from auto_coder.util.github_request_outcome import GitHubRequestContext


class ParentContextClient:
    """Control the strict external reads while preserving the public renderer."""

    def __init__(self):
        self.parent = {"number": 99}
        self.details = {"number": 99, "title": "Background", "body": "Parent context."}
        self.failure = None
        self.fail_at = "parent"
        self.calls = []

    def get_parent_issue_details_strict(self, repository, number):
        self.calls.append(("parent", repository, number))
        if self.failure is not None and self.fail_at == "parent":
            raise self.failure
        return self.parent

    def get_issue_dispatch_snapshot_strict(self, repository, number):
        self.calls.append(("issue", repository, number))
        if self.failure is not None and self.fail_at == "issue":
            raise self.failure
        return self.details

    def get_parent_issue_details(self, *_args):
        raise AssertionError("The strict renderer must not use a best-effort parent read.")

    def get_parent_issue_body(self, *_args):
        raise AssertionError("The strict renderer must not use a best-effort body read.")

    def add_sub_issue(self, *_args, **_kwargs):
        raise AssertionError("Reading validation context must not mutate hierarchy.")


def _resolution(body="Child contract."):
    return IssueOracleResolution(candidates=(100,), issues=(VerifiedIssueOracle(100, "Child", body),))


def _context(client, **kwargs):
    return get_linked_issues_context(client, "owner/repo", resolution=_resolution(), strict=True, **kwargs)


@pytest.mark.parametrize("fail_at", ["parent", "issue"])
def test_strict_parent_context_preserves_rate_limit_reason_and_retry(fail_at):
    client = ParentContextClient()
    request = GitHubRequestContext("parent-context", "read-one", "pr-validation", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}/issues/{number}/parent")
    client.failure = GitHubRequestDeferred(request, "rate_limit_cooldown", 4000000000.0)
    client.fail_at = fail_at

    with pytest.raises(GitHubRequestDeferred) as captured:
        _context(client)

    assert captured.value is client.failure
    assert captured.value.reason == "rate_limit_cooldown"
    assert captured.value.retry_at == 4000000000.0


def test_strict_parent_context_observes_relationship_and_complete_description():
    client = ParentContextClient()
    context = _context(client)

    assert "Linked Issue #100: Child" in context
    assert "Parent Issue #99 (CONTEXT ONLY - Parent of #100): Background" in context
    assert "Parent Issue Description:\nParent context." in context
    assert client.calls == [("parent", "owner/repo", 100), ("issue", "owner/repo", 99)]

    client.details["body"] = "Updated background."
    changed = _context(client)
    assert changed != context and "Updated background." in changed


def test_confirmed_parent_absence_is_complete_child_context():
    client = ParentContextClient()
    client.parent = None

    context = _context(client)

    assert context == "Linked Issues Context:\nLinked Issue #100: Child\n\nIssue Description:\nChild contract."
    assert client.calls == [("parent", "owner/repo", 100)]


def test_metadata_parent_fallback_is_read_only_after_confirmed_native_absence():
    client = ParentContextClient()
    client.parent = None

    context = get_linked_issues_context(client, "owner/repo", resolution=_resolution("Child contract.\nParent-Issue: #99"), strict=True)

    assert "Parent Issue Description:\nParent context." in context
    assert client.calls == [("parent", "owner/repo", 100), ("issue", "owner/repo", 99)]


@pytest.mark.parametrize("body", [None, ""])
def test_explicit_empty_parent_description_is_complete(body):
    client = ParentContextClient()
    client.details["body"] = body

    context = _context(client)

    assert "Linked Issue #100" in context
    assert "Parent Issue Description" not in context


@pytest.mark.parametrize("problem", ["missing_body", "wrong_parent", "pull_request"])
def test_incomplete_parent_description_cannot_be_returned_as_complete_context(problem):
    client = ParentContextClient()
    if problem == "missing_body":
        del client.details["body"]
    elif problem == "wrong_parent":
        client.details["number"] = 98
    else:
        client.details["pull_request"] = {}

    with pytest.raises(ValueError, match="Parent Issue #99"):
        _context(client)


def test_best_effort_context_still_tolerates_parent_lookup_failures():
    client = MagicMock()
    client.get_parent_issue_details.side_effect = ConnectionError("parent lookup failed")

    context = get_linked_issues_context(client, "owner/repo", resolution=_resolution())

    assert "Issue Description:\nChild contract." in context
    assert "Parent Issue Description" not in context


def test_strict_resolution_error_is_not_empty_or_partial_context():
    resolution = IssueOracleResolution(candidates=(100,), error="Current child evidence is unavailable")
    assert get_linked_issues_context(ParentContextClient(), "owner/repo", resolution=resolution) == ""
    with pytest.raises(RuntimeError, match="Current child evidence is unavailable"):
        get_linked_issues_context(ParentContextClient(), "owner/repo", resolution=resolution, strict=True)
