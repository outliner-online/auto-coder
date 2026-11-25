"""Repository-scoped attribution and effect authority for controller evidence."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
import tomllib
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional, cast

from .github_controller_auth import ControllerIdentity
from .llm_backend_config import resolve_repo_override_path
from .util.github_request_outcome import normalize_api_origin
from .utils import is_same_github_login


class HistoryState(str, Enum):
    LEGACY_CURRENT_ONLY = "legacy-current-only"
    UNKNOWN = "unknown"
    KNOWN = "known"
    UNAVAILABLE = "unavailable"


class ControllerAttribution(str, Enum):
    CURRENT = "current"
    PRIOR = "prior"
    UNATTRIBUTED_RELEVANT = "unattributed-relevant"
    OTHER = "other"


class ControllerIdentityHistoryError(RuntimeError):
    """The trusted controller-author declaration cannot be evaluated."""


@dataclass(frozen=True)
class ControllerAuthorSet:
    api_origin: str
    repository: str
    current: ControllerIdentity
    prior: tuple[ControllerIdentity, ...]
    state: HistoryState
    fingerprint: str
    diagnostic: str = ""

    def classify(self, author_id: Optional[int], author_login: str, *, relevant: bool) -> ControllerAttribution:
        # Only the explicit legacy adapter lacks numeric account evidence. Never
        # invent an account ID or let a matching login override a different ID.
        if self.state is HistoryState.LEGACY_CURRENT_ONLY and self.current.account_id is None:
            return ControllerAttribution.CURRENT if is_same_github_login(self.current.login, author_login) else ControllerAttribution.OTHER
        if not _positive_id(author_id):
            return ControllerAttribution.UNATTRIBUTED_RELEVANT if relevant else ControllerAttribution.OTHER
        if self.current.account_id == author_id:
            return ControllerAttribution.CURRENT
        if any(identity.account_id == author_id for identity in self.prior):
            return ControllerAttribution.PRIOR
        if relevant and self.state in (HistoryState.UNKNOWN, HistoryState.UNAVAILABLE):
            return ControllerAttribution.UNATTRIBUTED_RELEVANT
        return ControllerAttribution.OTHER


@dataclass
class ControllerHistoryCache:
    """Successful account resolutions for one immutable declaration at a time."""

    declaration_key: str = ""
    prior: tuple[ControllerIdentity, ...] = ()
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


@dataclass(frozen=True)
class _ControllerAuthority:
    client: object = field(repr=False)
    repository: str
    authors: ControllerAuthorSet


_authority_context: ContextVar[tuple[_ControllerAuthority, ...]] = ContextVar("controller_authority_context", default=())
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?\Z")


def _positive_id(value: object) -> bool:
    return type(value) is int and value > 0


def _valid_identity(identity: object) -> bool:
    return isinstance(identity, ControllerIdentity) and isinstance(identity.login, str) and bool(_LOGIN.fullmatch(identity.login)) and _positive_id(identity.account_id) and (identity.app_id is None or _positive_id(identity.app_id))


def _repo_config(repo_name: str, home: Path) -> tuple[bool, bool, tuple[str, ...]]:
    path_value = resolve_repo_override_path(repo_name, override_root_dir=str(home / ".auto-coder"), filename="config.toml")
    if not path_value or repo_name != repo_name.strip().strip("/") or repo_name.endswith(".git"):
        raise ControllerIdentityHistoryError("Controller history requires an exact owner/repository identity")
    path = Path(path_value)
    global_path = home / ".auto-coder" / "config.toml"
    app_mode = False
    if global_path.exists():
        try:
            with global_path.open("rb") as stream:
                app_mode = "github-app-auto-coder-controller" in tomllib.load(stream)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ControllerIdentityHistoryError("Controller configuration is unreadable or malformed") from exc
    if not path.exists():
        return app_mode, False, ()
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ControllerIdentityHistoryError("Controller identity history configuration is unreadable or malformed") from exc
    app_mode = app_mode or "github-app-auto-coder-controller" in data
    if "github-controller-identity-history" not in data:
        return app_mode, False, ()
    section = data["github-controller-identity-history"]
    if not isinstance(section, dict) or set(section) - {"prior_logins"}:
        raise ControllerIdentityHistoryError("Controller identity history declaration is malformed")
    # Only an explicit [] declares that the complete prior-controller set is
    # empty. An optional field omitted from an empty table makes no declaration.
    if "prior_logins" not in section:
        return app_mode, False, ()
    raw = section["prior_logins"]
    if not isinstance(raw, list) or any(not isinstance(value, str) or not _LOGIN.fullmatch(value) for value in raw):
        raise ControllerIdentityHistoryError("Controller identity history prior_logins must be a list of GitHub logins")
    logins = tuple(sorted(raw, key=str.casefold))
    if len({value.casefold() for value in logins}) != len(logins):
        raise ControllerIdentityHistoryError("Controller identity history contains duplicate logins")
    return app_mode, True, logins


def _fingerprint(material: object) -> str:
    return hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()


def resolve_controller_author_set(
    repo_name: str,
    current_identity: ControllerIdentity,
    *,
    account_lookup: Callable[[str], ControllerIdentity],
    api_origin: str = "https://api.github.com",
    home: Optional[Path] = None,
    cache: Optional[ControllerHistoryCache] = None,
) -> ControllerAuthorSet:
    """Resolve the complete trusted declaration for one repository/API origin.

    A failed historical lookup is unavailable membership, not an empty set. It
    affects only relevant historical candidates; proven current evidence and a
    fresh operation without historical candidates remain usable.
    """
    if not _valid_identity(current_identity):
        raise ControllerIdentityHistoryError("Current controller account identity is unavailable")
    app_mode, declared, logins = _repo_config(repo_name, home or Path.home())
    origin = normalize_api_origin(api_origin)
    state = HistoryState.KNOWN if declared else (HistoryState.UNKNOWN if app_mode else HistoryState.LEGACY_CURRENT_ONLY)
    declaration_key = _fingerprint([origin, repo_name.casefold(), state.value, [login.casefold() for login in logins]])
    prior: list[ControllerIdentity] = []
    diagnostic = ""
    active_cache = cache or ControllerHistoryCache()
    with active_cache.lock:
        if active_cache.declaration_key == declaration_key:
            prior = list(active_cache.prior)
        else:
            for login in logins:
                try:
                    identity = account_lookup(login)
                except Exception:
                    identity = None
                if not _valid_identity(identity):
                    state = HistoryState.UNAVAILABLE
                    diagnostic = f"Historical controller identity is unavailable for {login}; verify the repository declaration and GitHub account access"
                    prior = []
                    break
                assert isinstance(identity, ControllerIdentity)
                if identity.account_id == current_identity.account_id:
                    raise ControllerIdentityHistoryError("Controller identity history conflicts with the current identity")
                if any(item.account_id == identity.account_id for item in prior):
                    raise ControllerIdentityHistoryError("Controller identity history contains conflicting account identities")
                # The API's canonical login and positive account ID are the
                # identity, including redirects after an account rename.
                prior.append(identity)
            if state is not HistoryState.UNAVAILABLE:
                active_cache.declaration_key = declaration_key
                active_cache.prior = tuple(prior)
    if any(item.account_id == current_identity.account_id for item in prior):
        raise ControllerIdentityHistoryError("Controller identity history conflicts with the current identity")
    material = [origin, repo_name.casefold(), current_identity.account_id, current_identity.app_id, state.value, declaration_key, sorted(cast(int, item.account_id) for item in prior)]
    return ControllerAuthorSet(origin, repo_name.casefold(), current_identity, tuple(prior), state, _fingerprint(material), diagnostic)


def _explicit_method(client: object, name: str) -> bool:
    return callable(getattr(type(client), name, None)) or name in getattr(client, "__dict__", {})


def controller_author_set_for_client(client: object, repo_name: str, *, home: Optional[Path] = None) -> ControllerAuthorSet:
    """Use the current credential's identity, never a separately configured App."""
    identity_reader = getattr(client, "get_authenticated_controller_identity", None)
    lookup = getattr(client, "resolve_github_account_identity", None)
    identity = None
    if _explicit_method(client, "get_authenticated_controller_identity") and callable(identity_reader):
        try:
            try:
                inspect.signature(identity_reader).bind(repo_name)
            except (TypeError, ValueError):
                identity = identity_reader()
            else:
                identity = identity_reader(repo_name)
        except Exception as exc:
            raise ControllerIdentityHistoryError("Current controller account identity is unavailable") from exc
    if isinstance(identity, ControllerIdentity):
        if not callable(lookup):
            raise ControllerIdentityHistoryError("GitHub account identity resolution is unavailable")
        origin = getattr(client, "api_origin", "https://api.github.com")
        if not isinstance(origin, str):
            origin = "https://api.github.com"
        cache = getattr(client, "_controller_history_cache", None)
        if not isinstance(cache, ControllerHistoryCache):
            cache = ControllerHistoryCache()
            try:
                setattr(client, "_controller_history_cache", cache)
            except (AttributeError, TypeError):
                pass
        return resolve_controller_author_set(repo_name, identity, account_lookup=lookup, api_origin=origin, home=home, cache=cache)
    if _explicit_method(client, "get_authenticated_controller_identity"):
        raise ControllerIdentityHistoryError("Current controller account identity is unavailable")

    # Legacy injected clients expose an authoritative login-only reader. This
    # adapter is limited to undeclared personal mode and does not manufacture a
    # numeric identity or silently ignore configured historical association.
    app_mode, declared, _ = _repo_config(repo_name, home or Path.home())
    if app_mode or declared:
        raise ControllerIdentityHistoryError("Controller history requires positive GitHub account identity evidence")
    login_reader = getattr(client, "get_authenticated_user_login", None)
    login = login_reader() if callable(login_reader) else ""
    if not isinstance(login, str) or not login.strip():
        raise ControllerIdentityHistoryError("GitHub client cannot prove controller account identity")
    current = ControllerIdentity(login.strip())
    origin = normalize_api_origin("https://api.github.com")
    material = [origin, repo_name.casefold(), current.login.casefold(), HistoryState.LEGACY_CURRENT_ONLY.value]
    return ControllerAuthorSet(origin, repo_name.casefold(), current, (), HistoryState.LEGACY_CURRENT_ONLY, _fingerprint(material))


def assert_controller_authority(client: object, repo_name: str, expected: ControllerAuthorSet) -> ControllerAuthorSet:
    """Revalidate a pinned association before dispatch or successful settlement."""
    current = controller_author_set_for_client(client, repo_name)
    if expected.repository != repo_name.casefold() or current.fingerprint != expected.fingerprint:
        raise ControllerIdentityHistoryError("Controller identity or repository history changed; retain pending evidence and retry reconciliation with the current authority")
    return current


def assert_active_controller_authority() -> None:
    """Final provider-dispatch guard; unrelated provider calls have no scope."""
    for authority in _authority_context.get():
        assert_controller_authority(authority.client, authority.repository, authority.authors)


@contextmanager
def controller_authority_scope(client: object, repo_name: str, expected: Optional[ControllerAuthorSet] = None) -> Iterator[ControllerAuthorSet]:
    """Pin attribution across an existing receipt/effect critical section.

    GitHubClient supplies the wire-level credential binding; provider transports
    call assert_active_controller_authority immediately before their native send.
    Callers must also assert before each success write, not rely on context exit.
    """
    active = _authority_context.get()
    inherited = next((item.authors for item in reversed(active) if item.client is client and item.repository == repo_name.casefold()), None)
    if inherited is not None and expected is not None and inherited.fingerprint != expected.fingerprint:
        raise ControllerIdentityHistoryError("Nested controller operation changed its pinned identity history")
    pinned = expected or inherited or controller_author_set_for_client(client, repo_name)
    assert_controller_authority(client, repo_name, pinned)
    authority = _ControllerAuthority(client, repo_name.casefold(), pinned)
    context_token = _authority_context.set((*active, authority))
    transport_scope = getattr(client, "controller_authority_scope", None)
    try:
        with transport_scope(repo_name, pinned) if _explicit_method(client, "controller_authority_scope") and callable(transport_scope) else nullcontext():
            yield pinned
            assert_controller_authority(client, repo_name, pinned)
    finally:
        _authority_context.reset(context_token)
