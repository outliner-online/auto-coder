"""Explicit one-commit GitHub history fixtures for unrelated PR-path tests."""

from __future__ import annotations

from typing import Callable
from unittest.mock import Mock

from auto_coder.pr_automation_guard import PRCommit, PRCommitHistory


def single_commit_history(head: str) -> PRCommitHistory:
    """Declare the controlled fixture's existing commit at its current head."""
    if not isinstance(head, str) or not head:
        raise ValueError("A controlled PR history fixture requires its current head")
    return PRCommitHistory(head, 1, (PRCommit(head, "Existing controlled PR candidate"),))


def install_pr_history(client: object, head: str | Callable[[str, int], str]) -> Mock:
    """Supply history on this client; the real intervention guard still executes."""

    def observe(repository: str, pr_number: int) -> PRCommitHistory:
        return single_commit_history(head(repository, pr_number) if callable(head) else head)

    reader = Mock(side_effect=observe)
    setattr(client, "get_pr_automation_history_strict", reader)
    return reader


def install_mock_pr_history(client: object, default_head: str) -> Mock:
    """Follow configured mock metadata without consuming its scripted reads."""

    def head(_repository: str, pr_number: int) -> str:
        candidates = []
        for method_name in ("get_open_prs_json", "get_pull_request_metadata_strict", "get_pr_details", "get_pull_request", "get_pr", "get_pull_request_routing_metadata_strict", "get_pull_request_repair_metadata_strict"):
            method = getattr(client, method_name, None)
            value = getattr(method, "return_value", None)
            if isinstance(value, list):
                candidates.extend(value)
            elif value is not None:
                candidates.append(value)
        for candidate in candidates:
            if isinstance(candidate, dict):
                if candidate.get("number", pr_number) != pr_number:
                    continue
                metadata = candidate.get("head", {})
                sha = metadata.get("sha") if isinstance(metadata, dict) else None
            else:
                sha = getattr(candidate, "head_sha", None)
            if isinstance(sha, str) and sha:
                return sha
        return default_head

    return install_pr_history(client, head)
