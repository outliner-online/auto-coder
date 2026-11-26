"""Controller commit authorship uses verified App bot account evidence."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from auto_coder import controller_git_identity
from auto_coder.controller_git_identity import ControllerGitIdentity, ControllerGitIdentityError, resolve_controller_git_identity
from auto_coder.git_branch import git_commit_with_retry
from auto_coder.github_controller_auth import ControllerCredential, ControllerIdentity

pytestmark = pytest.mark.usefixtures("_use_custom_subprocess_mock")

REPOSITORY = "owner/repo"


@dataclass
class VerifiedProvider:
    credential: ControllerCredential = ControllerCredential("fixture-token", mode="app", repository=REPOSITORY, app_id=123, installation_id=456, authority_key="verified")
    identity: ControllerIdentity = ControllerIdentity("controller[bot]", app_id=123)
    calls: int = 0

    def credential_for(self, repository, permissions):
        assert repository == REPOSITORY
        assert permissions == {"contents": "write"}
        self.calls += 1
        return self.credential

    def identity_for(self, repository):
        assert repository == REPOSITORY
        return self.identity

    def invalidate(self, credential):
        pytest.fail("identity resolution must not replay authentication")


def _account(monkeypatch, account=ControllerIdentity("controller[bot]", account_id=98765)):
    def resolve(client, login):
        assert client.token == "fixture-token"
        assert login == "controller[bot]"
        return account

    monkeypatch.setattr(controller_git_identity.GitHubClient, "resolve_github_account_identity", resolve)


def test_verified_bot_account_id_drives_both_commit_roles(monkeypatch, tmp_path):
    _account(monkeypatch)
    provider = VerifiedProvider()
    identity = resolve_controller_git_identity(REPOSITORY, provider=provider, home=tmp_path)
    assert identity == ControllerGitIdentity(REPOSITORY, "controller[bot]", 98765, 123)
    assert identity.email == "98765+controller[bot]@users.noreply.github.com"
    assert provider.calls == 1
    before = dict(os.environ)
    env = identity.env()
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "controller[bot]"
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == identity.email
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("mode", "personal", "not configured"),
        ("repository", "owner/other", "does not match"),
        ("app_id", 777, "disagree"),
        ("api_origin", "https://other.example", "does not match"),
    ],
)
def test_unverified_controller_authority_never_resolves_a_fallback(monkeypatch, tmp_path, field, value, reason):
    provider = VerifiedProvider()
    provider.credential = replace(provider.credential, **{field: value})
    monkeypatch.setattr(controller_git_identity.GitHubClient, "resolve_github_account_identity", lambda *_: pytest.fail("fallback account lookup"))
    with pytest.raises(ControllerGitIdentityError, match=reason):
        resolve_controller_git_identity(REPOSITORY, provider=provider, home=tmp_path)


@pytest.mark.parametrize("account", [ControllerIdentity("person", account_id=98765), ControllerIdentity("controller[bot]"), ControllerIdentity("controller[bot]", account_id=0)])
def test_missing_or_conflicting_bot_account_is_not_app_registration_id(monkeypatch, tmp_path, account):
    _account(monkeypatch, account)
    with pytest.raises(ControllerGitIdentityError):
        resolve_controller_git_identity(REPOSITORY, provider=VerifiedProvider(), home=tmp_path)


def test_reviewer_app_is_not_a_controller_fallback(monkeypatch, tmp_path):
    config = tmp_path / ".auto-coder" / "config.toml"
    config.parent.mkdir()
    config.write_text("[github-app-auto-coder-reviewer]\napp_id = 123\n")
    monkeypatch.setattr(controller_git_identity.GitHubClient, "resolve_github_account_identity", lambda *_: pytest.fail("reviewer account fallback"))
    with pytest.raises(ControllerGitIdentityError, match="separate from the Reviewer"):
        resolve_controller_git_identity(REPOSITORY, provider=VerifiedProvider(), home=tmp_path)


def test_account_lookup_failure_is_sanitized(monkeypatch, tmp_path):
    def fail(*_args):
        raise RuntimeError("secret fixture-token internal response")

    monkeypatch.setattr(controller_git_identity.GitHubClient, "resolve_github_account_identity", fail)
    with pytest.raises(ControllerGitIdentityError, match="verification is unavailable") as caught:
        resolve_controller_git_identity(REPOSITORY, provider=VerifiedProvider(), home=tmp_path)
    assert "fixture-token" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_central_commit_helper_uses_verified_environment_without_git_config(monkeypatch, tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for variable in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(variable, raising=False)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.useConfigOnly", "true"], cwd=root, check=True)
    config_before = (root / ".git" / "config").read_bytes()
    (root / "source.txt").write_text("retained correction\n")
    subprocess.run(["git", "add", "source.txt"], cwd=root, check=True)
    identity = ControllerGitIdentity(REPOSITORY, "controller[bot]", 98765, 123)
    result = git_commit_with_retry("Retain reviewer correction", cwd=str(root), allow_repair=False, commit_env=identity.env())
    assert result.success, result.stderr
    observed = subprocess.check_output(["git", "show", "-s", "--format=%an%n%ae%n%cn%n%ce"], cwd=root, text=True).splitlines()
    assert observed == [identity.login, identity.email, identity.login, identity.email]
    assert (root / ".git" / "config").read_bytes() == config_before
