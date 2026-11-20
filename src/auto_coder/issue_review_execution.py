"""Repository checkout binding for Muse individual specification review."""

from __future__ import annotations

import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .invocation_admission import current_invocation_target
from .llm_backend_config import load_app_config_data
from .utils import bind_command_execution_cwd, reset_command_execution_cwd


class IssueReviewCheckoutError(RuntimeError):
    """A retryable local setup failure, never a semantic review verdict."""


_GITHUB_REMOTE = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/\s]+/[^/\s]+?)(?:\.git)?/?")


def _verified_checkout(repository: str) -> Path:
    try:
        data = load_app_config_data(repo_name=repository)
        section = data.get("issue_review", {})
        selected = section.get("checkout") if isinstance(section, dict) else None
        if not isinstance(selected, str) or not selected.strip() or not Path(selected).is_absolute():
            raise IssueReviewCheckoutError("issue_review.checkout must name an explicit absolute target repository checkout")
        checkout = Path(selected).resolve(strict=True)
        if not checkout.is_dir():
            raise IssueReviewCheckoutError("issue_review.checkout is not a directory")
        if any(name in os.environ for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")):
            raise IssueReviewCheckoutError("Git directory overrides make the selected review checkout ambiguous")

        def git(*arguments: str) -> str:
            result = subprocess.run(["git", *arguments], cwd=checkout, capture_output=True, text=True, timeout=10, check=False)
            if result.returncode:
                raise IssueReviewCheckoutError("selected review checkout is missing usable Git worktree, origin, or HEAD evidence")
            return result.stdout.strip()

        if git("rev-parse", "--is-inside-work-tree") != "true" or Path(git("rev-parse", "--show-toplevel")).resolve() != checkout:
            raise IssueReviewCheckoutError("issue_review.checkout must identify the selected Git worktree root")
        remotes = git("config", "--get-all", "remote.origin.url").splitlines()
        remote = _GITHUB_REMOTE.fullmatch(remotes[0]) if len(remotes) == 1 else None
        if remote is None or remote.group(1).lower() != repository.lower():
            raise IssueReviewCheckoutError("selected review checkout origin does not uniquely match the target repository")
        git("rev-parse", "--verify", "HEAD^{commit}")
        return checkout
    except IssueReviewCheckoutError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise IssueReviewCheckoutError("selected review checkout configuration or Git evidence is unavailable") from exc


@contextmanager
def issue_specification_execution_cwd(backend_type: str, is_noedit: bool) -> Iterator[None]:
    """Bind only Muse individual review, using the lifecycle's exact target.

    The lifecycle establishes this identity inside the actual scheduled callback.
    Binding before private-workspace preparation carries the verified source to
    Muse's existing Git snapshot without changing other providers or PR review.
    """
    target = current_invocation_target()
    if backend_type != "muse" or not is_noedit or target is None or target.stage != "specification_validation":
        yield
        return
    checkout = _verified_checkout(target.repository)
    token = bind_command_execution_cwd(str(checkout))
    try:
        yield
    finally:
        reset_command_execution_cwd(token)
