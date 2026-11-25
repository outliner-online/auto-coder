"""Identity/history semantics and pinned authority across real consumer entrypoints."""

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from auto_coder.controller_identity_history import (
    ControllerAttribution,
    ControllerHistoryCache,
    ControllerIdentityHistoryError,
    HistoryState,
    assert_active_controller_authority,
    assert_controller_authority,
    controller_author_set_for_client,
    controller_authority_scope,
    resolve_controller_author_set,
)
from auto_coder.github_controller_auth import ControllerIdentity

CURRENT = ControllerIdentity("new-controller", account_id=20)


def _write(home: Path, body: str) -> None:
    path = home / ".auto-coder" / "owner" / "repo" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _resolve(home, *, lookup=lambda login: ControllerIdentity(login.lower(), account_id=10), current=CURRENT, **kwargs):
    return resolve_controller_author_set("owner/repo", current, account_lookup=lookup, home=home, **kwargs)


def test_explicit_history_uses_canonical_ids_and_never_login_override(tmp_path: Path) -> None:
    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["Old-Controller"]\n')
    authors = _resolve(tmp_path)
    assert authors.state is HistoryState.KNOWN
    assert authors.prior == (ControllerIdentity("old-controller", account_id=10),)
    assert authors.classify(10, "renamed-login", relevant=True) is ControllerAttribution.PRIOR
    assert authors.classify(20, "new-controller", relevant=True) is ControllerAttribution.CURRENT
    assert authors.classify(30, "old-controller", relevant=True) is ControllerAttribution.OTHER
    assert authors.classify(30, "new-controller", relevant=True) is ControllerAttribution.OTHER
    assert authors.classify(None, "old-controller", relevant=True) is ControllerAttribution.UNATTRIBUTED_RELEVANT


@pytest.mark.parametrize("history", ["", "[github-controller-identity-history]\n"])
def test_absent_list_is_unknown_in_app_mode_and_explicit_empty_is_complete(tmp_path: Path, history: str) -> None:
    _write(tmp_path, "[github-app-auto-coder-controller]\napp_id = 123\n" + history)
    unknown = _resolve(tmp_path)
    assert unknown.state is HistoryState.UNKNOWN
    assert unknown.classify(10, "old", relevant=True) is ControllerAttribution.UNATTRIBUTED_RELEVANT
    assert unknown.classify(10, "old", relevant=False) is ControllerAttribution.OTHER
    assert unknown.classify(20, "new-controller", relevant=True) is ControllerAttribution.CURRENT
    _write(tmp_path, "[github-app-auto-coder-controller]\napp_id = 123\n[github-controller-identity-history]\nprior_logins = []\n")
    complete = _resolve(tmp_path)
    assert complete.state is HistoryState.KNOWN
    assert complete.classify(10, "old", relevant=True) is ControllerAttribution.OTHER
    assert complete.fingerprint != unknown.fingerprint


def test_unresolved_history_is_explicit_without_blocking_current_or_unrelated_evidence(tmp_path: Path) -> None:
    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["old"]\n')

    def unavailable(login):
        raise RuntimeError("unrelated transport payload must not escape")

    authors = _resolve(tmp_path, lookup=unavailable)
    assert authors.state is HistoryState.UNAVAILABLE
    assert authors.prior == ()
    assert "old" in authors.diagnostic
    assert "transport payload" not in authors.diagnostic
    assert authors.classify(10, "old", relevant=True) is ControllerAttribution.UNATTRIBUTED_RELEVANT
    assert authors.classify(10, "old", relevant=False) is ControllerAttribution.OTHER
    assert authors.classify(20, "new-controller", relevant=True) is ControllerAttribution.CURRENT


@pytest.mark.parametrize("declaration", ['["old", "OLD"]', '["old", "alias"]'])
def test_duplicate_login_or_conflicting_canonical_account_is_rejected(tmp_path: Path, declaration: str) -> None:
    _write(tmp_path, f"[github-controller-identity-history]\nprior_logins = {declaration}\n")
    with pytest.raises(ControllerIdentityHistoryError, match="duplicate|conflicting"):
        _resolve(tmp_path)


def test_current_account_cannot_also_be_declared_prior(tmp_path: Path) -> None:
    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["old"]\n')
    with pytest.raises(ControllerIdentityHistoryError, match="current identity"):
        _resolve(tmp_path, lookup=lambda login: ControllerIdentity("renamed", account_id=20))


@pytest.mark.parametrize("declaration", ['"old"', "true", "[1]", '["../old"]', '[" old"]', '["old/new"]', '[""]'])
def test_malformed_declarations_are_rejected(tmp_path: Path, declaration: str) -> None:
    _write(tmp_path, f"[github-controller-identity-history]\nprior_logins = {declaration}\n")
    with pytest.raises(ControllerIdentityHistoryError):
        _resolve(tmp_path)


@pytest.mark.parametrize("account_id", [None, True, 0, -1, "20"])
def test_current_identity_requires_positive_integer_account_id(tmp_path: Path, account_id) -> None:
    with pytest.raises(ControllerIdentityHistoryError, match="Current controller"):
        _resolve(tmp_path, current=ControllerIdentity("new-controller", account_id=account_id))


@pytest.mark.parametrize("author_id", [None, True, 0, -1, "20"])
def test_invalid_author_account_is_never_trusted_by_login(tmp_path: Path, author_id) -> None:
    authors = _resolve(tmp_path)
    assert authors.classify(author_id, "new-controller", relevant=True) is ControllerAttribution.UNATTRIBUTED_RELEVANT
    assert authors.classify(author_id, "new-controller", relevant=False) is ControllerAttribution.OTHER


def test_repository_history_is_not_inherited_from_global_or_other_repository(tmp_path: Path) -> None:
    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["old"]\n')
    (tmp_path / ".auto-coder" / "config.toml").write_text('[github-controller-identity-history]\nprior_logins = ["global-user"]\n')
    authors = resolve_controller_author_set("other/repo", CURRENT, account_lookup=lambda login: pytest.fail("history escaped its repository"), home=tmp_path)
    assert authors.state is HistoryState.LEGACY_CURRENT_ONLY
    assert authors.prior == ()


def test_invalid_repository_does_not_read_outside_its_configuration_scope(tmp_path: Path) -> None:
    with pytest.raises(ControllerIdentityHistoryError, match="exact owner/repository"):
        resolve_controller_author_set("../repo", CURRENT, account_lookup=lambda login: CURRENT, home=tmp_path)


def test_resolved_history_cache_is_bound_to_declaration_repository_and_origin(tmp_path: Path) -> None:
    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["old"]\n')
    cache = ControllerHistoryCache()
    calls = []

    def lookup(login):
        calls.append(login)
        return ControllerIdentity("canonical-old", account_id=10)

    initial = _resolve(tmp_path, lookup=lookup, cache=cache)
    assert initial.prior[0].login == "canonical-old"
    again = _resolve(tmp_path, lookup=lookup, cache=cache)
    assert again == initial
    assert calls == ["old"]
    changed_origin = _resolve(tmp_path, lookup=lookup, cache=cache, api_origin="https://github.example")
    assert changed_origin.fingerprint != initial.fingerprint
    assert calls == ["old", "old"]
    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["renamed"]\n')
    changed_declaration = _resolve(tmp_path, lookup=lookup, cache=cache)
    assert changed_declaration.fingerprint != initial.fingerprint
    assert calls == ["old", "old", "renamed"]


@dataclass
class ActualClient:
    identity: ControllerIdentity = CURRENT
    lookups: list[str] = field(default_factory=list)

    def get_authenticated_controller_identity(self, repo_name=None):
        assert repo_name == "owner/repo"
        return self.identity

    def resolve_github_account_identity(self, login):
        self.lookups.append(login)
        return ControllerIdentity(login, account_id=10)


@pytest.mark.parametrize("change", ["account", "app", "history"])
def test_scope_revalidates_actual_identity_and_history_before_effect(tmp_path: Path, monkeypatch, change: str) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    client = ActualClient()
    sends = []
    with pytest.raises(ControllerIdentityHistoryError, match="changed"):
        with controller_authority_scope(client, "owner/repo"):
            if change == "account":
                client.identity = ControllerIdentity("another", account_id=30)
            elif change == "app":
                client.identity = replace(CURRENT, app_id=101)
            else:
                _write(tmp_path, "[github-controller-identity-history]\nprior_logins = []\n")
            assert_active_controller_authority()
            sends.append("sent")
    assert sends == []
    # The scope resets even on failure, so unrelated calls are unaffected.
    assert_active_controller_authority()


def test_scope_does_not_claim_settlement_after_postsend_history_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    client = ActualClient()
    observed = []
    settled = []
    with pytest.raises(ControllerIdentityHistoryError, match="changed"):
        with controller_authority_scope(client, "owner/repo") as pinned:
            observed.append("accepted")
            _write(tmp_path, "[github-controller-identity-history]\nprior_logins = []\n")
            assert_controller_authority(client, "owner/repo", pinned)
            settled.append("delivered")
    assert observed == ["accepted"]
    assert settled == []


def test_nested_scope_preserves_prior_snapshot_and_token_only_refresh_is_valid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    client = ActualClient()
    with controller_authority_scope(client, "owner/repo") as initial:
        client.identity = replace(CURRENT)
        with controller_authority_scope(client, "owner/repo") as nested:
            assert nested is initial
            assert_active_controller_authority()
        different = replace(initial, fingerprint="different")
        with pytest.raises(ControllerIdentityHistoryError, match="Nested"):
            with controller_authority_scope(client, "owner/repo", expected=different):
                pytest.fail("nested authority changed")


def test_legacy_adapter_does_not_invent_an_account_or_bypass_declared_history(tmp_path: Path) -> None:
    class LegacyClient:
        def get_authenticated_user_login(self):
            return "legacy-controller"

    client = LegacyClient()
    authors = controller_author_set_for_client(client, "owner/repo", home=tmp_path)
    assert authors.current.account_id is None
    assert authors.classify(1, "unrelated", relevant=True) is ControllerAttribution.OTHER
    assert authors.classify(None, "legacy-controller", relevant=True) is ControllerAttribution.CURRENT
    _write(tmp_path, "[github-controller-identity-history]\nprior_logins = []\n")
    with pytest.raises(ControllerIdentityHistoryError, match="positive GitHub account"):
        controller_author_set_for_client(client, "owner/repo", home=tmp_path)


def test_proven_current_conflict_is_not_erased_by_later_unavailable_account(tmp_path: Path) -> None:
    _write(tmp_path, '[github-controller-identity-history]\nprior_logins = ["a-current", "z-unavailable"]\n')

    def lookup(login):
        if login == "a-current":
            return ControllerIdentity("new-controller", account_id=20)
        raise RuntimeError("account unavailable")

    with pytest.raises(ControllerIdentityHistoryError, match="current identity"):
        _resolve(tmp_path, lookup=lookup)
