"""Verified, repository-scoped Controller App identity for corrective Git commits."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .github_app_reviewer import load_reviewer_app_config
from .github_controller_auth import ControllerAuthError, ControllerAuthProvider, select_controller_auth
from .util.gh_cache import GitHubClient


class ControllerGitIdentityError(RuntimeError):
    """A nonsecret reason why a corrective commit cannot select its actor."""


@dataclass(frozen=True)
class ControllerGitIdentity:
    repository: str
    login: str
    account_id: int
    app_id: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", self.repository):
            raise ControllerGitIdentityError("controller_git_identity: invalid repository binding")
        if not re.fullmatch(r"[A-Za-z0-9-]+\[bot\]", self.login):
            raise ControllerGitIdentityError("controller_git_identity: verified App bot login is unavailable")
        if type(self.account_id) is not int or self.account_id <= 0 or type(self.app_id) is not int or self.app_id <= 0:
            raise ControllerGitIdentityError("controller_git_identity: verified App or bot account ID is unavailable")

    @property
    def email(self) -> str:
        """GitHub's noreply address uses the bot user ID, not the App ID."""
        return f"{self.account_id}+{self.login}@users.noreply.github.com"

    def env(self) -> dict[str, str]:
        """Bind both Git identity roles to one command without changing config."""
        return {
            **os.environ,
            "GIT_AUTHOR_NAME": self.login,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.login,
            "GIT_COMMITTER_EMAIL": self.email,
        }


def resolve_controller_git_identity(
    repository: str,
    *,
    provider: Optional[ControllerAuthProvider] = None,
    home: Optional[Path] = None,
) -> ControllerGitIdentity:
    """Verify configured App authority and resolve its independent bot account.

    This is a publication-time gate. Startup enforcement belongs to the later
    Controller App runtime activation; this function never changes App roles,
    installation membership, global Git configuration or personal credentials.
    """
    try:
        selected = provider or select_controller_auth(repository, home=home)
        credential = selected.credential_for(repository, {"contents": "write"})
        if credential.mode != "app":
            raise ControllerGitIdentityError("controller_git_identity: Controller App is not configured for this repository")
        if credential.repository != repository or credential.api_origin != "https://api.github.com":
            raise ControllerGitIdentityError("controller_git_identity: repository or GitHub origin does not match")
        identity = selected.identity_for(repository)
        if identity.app_id is None or identity.app_id != credential.app_id:
            raise ControllerGitIdentityError("controller_git_identity: verified App identity and repository credential disagree")
        try:
            reviewer = load_reviewer_app_config(home=home, repo_name=repository)
        except ValueError:
            reviewer = None
        if reviewer is not None and int(reviewer.app_id) == identity.app_id:
            raise ControllerGitIdentityError("controller_git_identity: Controller App must be separate from the Reviewer App")
        account = GitHubClient(credential.token).resolve_github_account_identity(identity.login)
        if account.login != identity.login or account.account_id is None:
            raise ControllerGitIdentityError("controller_git_identity: GitHub bot account does not match the verified App")
        return ControllerGitIdentity(repository, account.login, account.account_id, identity.app_id)
    except ControllerGitIdentityError:
        raise
    except ControllerAuthError as exc:
        raise ControllerGitIdentityError(f"controller_git_identity: {exc.stage}/{exc.category}") from None
    except Exception:
        raise ControllerGitIdentityError("controller_git_identity: GitHub bot account verification is unavailable") from None
