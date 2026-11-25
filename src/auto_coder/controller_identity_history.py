"""Repository-scoped attribution of historical controller publications."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

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

    def classify(self, author_id: Optional[int], author_login: str, *, relevant: bool) -> ControllerAttribution:
        if (self.state is HistoryState.LEGACY_CURRENT_ONLY and is_same_github_login(self.current.login, author_login)) or _matches(self.current, author_id, author_login):
            return ControllerAttribution.CURRENT
        if any(_matches(identity, author_id, author_login) for identity in self.prior):
            return ControllerAttribution.PRIOR
        if relevant and self.state in (HistoryState.UNKNOWN, HistoryState.UNAVAILABLE):
            return ControllerAttribution.UNATTRIBUTED_RELEVANT
        return ControllerAttribution.OTHER


def _matches(identity: ControllerIdentity, author_id: Optional[int], author_login: str) -> bool:
    if identity.account_id is not None and author_id is not None:
        return identity.account_id == author_id
    return is_same_github_login(identity.login, author_login)


def _repo_config(repo_name: str, home: Path) -> tuple[bool, bool, tuple[str, ...]]:
    path_value = resolve_repo_override_path(repo_name, override_root_dir=str(home / ".auto-coder"), filename="config.toml")
    path = Path(path_value) if path_value else home / ".auto-coder" / repo_name / "config.toml"
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
    raw = section.get("prior_logins", [])
    if not isinstance(raw, list) or any(not isinstance(value, str) or not value.strip() for value in raw):
        raise ControllerIdentityHistoryError("Controller identity history prior_logins must be a list of logins")
    logins = tuple(value.strip() for value in raw)
    if len({value.casefold() for value in logins}) != len(logins):
        raise ControllerIdentityHistoryError("Controller identity history contains duplicate logins")
    return app_mode, True, logins


def resolve_controller_author_set(
    repo_name: str,
    current_identity: ControllerIdentity,
    *,
    account_lookup: Callable[[str], ControllerIdentity],
    api_origin: str = "https://api.github.com",
    home: Optional[Path] = None,
) -> ControllerAuthorSet:
    """Resolve the complete trusted declaration for one repository/API origin."""
    if not current_identity.login or not isinstance(current_identity.account_id, int) or current_identity.account_id <= 0:
        raise ControllerIdentityHistoryError("Current controller account identity is unavailable")
    app_mode, declared, logins = _repo_config(repo_name, home or Path.home())
    state = HistoryState.KNOWN if declared else (HistoryState.UNKNOWN if app_mode else HistoryState.LEGACY_CURRENT_ONLY)
    prior: list[ControllerIdentity] = []
    for login in logins:
        try:
            identity = account_lookup(login)
        except Exception as exc:
            raise ControllerIdentityHistoryError(f"Historical controller account identity is unavailable for {login}") from exc
        if not identity.login or not isinstance(identity.account_id, int) or identity.account_id <= 0:
            raise ControllerIdentityHistoryError(f"Historical controller account identity is unavailable for {login}")
        if not is_same_github_login(identity.login, login):
            raise ControllerIdentityHistoryError(f"Historical controller login resolved inconsistently for {login}")
        if identity.account_id == current_identity.account_id or any(item.account_id == identity.account_id for item in prior):
            raise ControllerIdentityHistoryError("Controller identity history conflicts with current or prior identity")
        prior.append(identity)
    origin = normalize_api_origin(api_origin)
    material = [origin, repo_name.casefold(), current_identity.account_id, state.value, [(item.account_id, item.login.casefold()) for item in prior]]
    fingerprint = hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()
    return ControllerAuthorSet(origin, repo_name.casefold(), current_identity, tuple(prior), state, fingerprint)


def controller_author_set_for_client(client: object, repo_name: str, *, home: Optional[Path] = None) -> ControllerAuthorSet:
    """Resolve attribution from the identity proven by the actual client."""
    identity_reader = getattr(client, "get_authenticated_controller_identity", None)
    lookup = getattr(client, "resolve_github_account_identity", None)
    identity = identity_reader() if callable(identity_reader) else None
    if isinstance(identity, ControllerIdentity) and callable(lookup):
        return resolve_controller_author_set(repo_name, identity, account_lookup=lookup, home=home)

    # Legacy injected clients predate positive-ID observations. Preserve their
    # current-author-only contract; production GitHubClient always takes the
    # strict branch above, so App/history mode can never enter this adapter.
    login_reader = getattr(client, "get_authenticated_user_login", None)
    login = login_reader() if callable(login_reader) else ""
    if not isinstance(login, str) or not login.strip():
        raise ControllerIdentityHistoryError("GitHub client cannot prove controller account identity")
    current = ControllerIdentity(login.strip(), account_id=1)
    origin = normalize_api_origin("https://api.github.com")
    material = [origin, repo_name.casefold(), current.login.casefold(), HistoryState.LEGACY_CURRENT_ONLY.value]
    fingerprint = hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()
    return ControllerAuthorSet(origin, repo_name.casefold(), current, (), HistoryState.LEGACY_CURRENT_ONLY, fingerprint)
