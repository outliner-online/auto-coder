from pathlib import Path

import pytest

from auto_coder.controller_identity_history import (
    ControllerAttribution,
    ControllerIdentityHistoryError,
    HistoryState,
    resolve_controller_author_set,
)
from auto_coder.github_controller_auth import ControllerIdentity


def _write(home: Path, body: str) -> None:
    path = home / ".auto-coder" / "owner" / "repo" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_explicit_history_resolves_ids_and_classifies_authors(tmp_path: Path) -> None:
    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["Old-Controller"]\n')
    authors = resolve_controller_author_set(
        "owner/repo",
        ControllerIdentity("new-controller", account_id=20),
        account_lookup=lambda login: ControllerIdentity(login.lower(), account_id=10),
        home=tmp_path,
    )

    assert authors.state is HistoryState.KNOWN
    assert authors.classify(10, "renamed-login", relevant=True) is ControllerAttribution.PRIOR
    assert authors.classify(20, "new-controller", relevant=True) is ControllerAttribution.CURRENT
    assert authors.classify(30, "copier", relevant=True) is ControllerAttribution.OTHER


def test_app_absence_is_unknown_but_explicit_empty_is_complete(tmp_path: Path) -> None:
    _write(tmp_path, "[github-app-auto-coder-controller]\napp_id = 123\n")
    unknown = resolve_controller_author_set("owner/repo", ControllerIdentity("app[bot]", account_id=20), account_lookup=lambda login: None, home=tmp_path)
    assert unknown.state is HistoryState.UNKNOWN
    assert unknown.classify(10, "old", relevant=True) is ControllerAttribution.UNATTRIBUTED_RELEVANT
    _write(tmp_path, "[github-app-auto-coder-controller]\napp_id = 123\n[github-controller-identity-history]\nprior_logins = []\n")
    complete = resolve_controller_author_set("owner/repo", ControllerIdentity("app[bot]", account_id=20), account_lookup=lambda login: None, home=tmp_path)
    assert complete.state is HistoryState.KNOWN
    assert complete.classify(10, "old", relevant=True) is ControllerAttribution.OTHER


def test_conflicting_or_unresolved_declarations_fail_closed(tmp_path: Path) -> None:
    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["old", "OLD"]\n')
    with pytest.raises(ControllerIdentityHistoryError, match="duplicate"):
        resolve_controller_author_set("owner/repo", ControllerIdentity("current", account_id=20), account_lookup=lambda login: ControllerIdentity(login, account_id=10), home=tmp_path)

    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["old"]\n')
    with pytest.raises(ControllerIdentityHistoryError, match="unavailable"):
        resolve_controller_author_set("owner/repo", ControllerIdentity("current", account_id=20), account_lookup=lambda login: (_ for _ in ()).throw(RuntimeError()), home=tmp_path)
