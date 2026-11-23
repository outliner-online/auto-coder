"""Repository-bound, renewable authentication for the controller GitHub App."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

import httpx
import jwt

from .auth_utils import get_github_token
from .llm_backend_config import deep_merge_config_dict, resolve_repo_override_path
from .util.github_request_outcome import instrument_github_client


class ControllerAuthError(RuntimeError):
    """Sanitized authentication failure with an operational stage/category."""

    def __init__(self, stage: str, category: str = "invalid_response") -> None:
        self.stage = stage
        self.category = category
        super().__init__(f"Controller authentication failed ({stage}/{category})")


@dataclass(frozen=True)
class ControllerIdentity:
    login: str
    account_id: Optional[int] = None
    app_id: Optional[int] = None


@dataclass(frozen=True)
class ControllerCredential:
    token: str = field(repr=False)
    mode: str = ""
    api_origin: str = "https://api.github.com"
    repository: str = ""
    permissions: tuple[tuple[str, str], ...] = ()
    authority_key: str = ""
    expires_at: Optional[datetime] = None
    app_id: Optional[int] = None
    installation_id: Optional[int] = None
    snapshot_id: str = ""


@dataclass(frozen=True)
class ControllerAppConfig:
    app_id: int
    private_key_path: Path


class ControllerAuthProvider(Protocol):
    def credential_for(self, repo_name: str, required_permissions: Mapping[str, str]) -> ControllerCredential: ...
    def identity_for(self, repo_name: str) -> ControllerIdentity: ...
    def invalidate(self, credential: ControllerCredential) -> None: ...


def _read_config(path: Path, *, missing_ok: bool) -> dict:
    if missing_ok and not path.exists():
        return {}
    failed = False
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        failed = True
        value = {}
    if failed:
        raise ControllerAuthError("configuration", "unreadable_or_malformed")
    if not isinstance(value, dict):
        raise ControllerAuthError("configuration")
    return value


def _load_controller_config(repo_name: str, config_path: Optional[Path], home: Path) -> Optional[ControllerAppConfig]:
    data = _read_config(config_path or home / ".auto-coder" / "config.toml", missing_ok=True)
    override_name = resolve_repo_override_path(repo_name, override_root_dir=str(home / ".auto-coder"), filename="config.toml")
    if override_name and Path(override_name).exists():
        data = deep_merge_config_dict(data, _read_config(Path(override_name), missing_ok=False))
    if "github-app-auto-coder-controller" not in data:
        return None
    section = data["github-app-auto-coder-controller"]
    if not isinstance(section, dict):
        raise ControllerAuthError("configuration")
    app_id = section.get("app_id")
    if not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0:
        raise ControllerAuthError("configuration", "invalid_app_id")
    raw_path = section.get("private_key_path", "~/.auto-coder/auto-coder-controller.pem")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ControllerAuthError("configuration", "invalid_key_path")
    raw_path = raw_path.strip()
    if raw_path.startswith("~/"):
        key_path = home / raw_path[2:]
    else:
        key_path = Path(raw_path)
    if not key_path.is_absolute():
        raise ControllerAuthError("configuration", "invalid_key_path")
    return ControllerAppConfig(app_id, key_path)


class PersonalControllerAuth:
    def __init__(self, repository: str, token: str, client: httpx.Client, api_origin: str) -> None:
        self.repository, self._token, self._client, self.api_origin = repository, token, client, api_origin
        self._identity: Optional[ControllerIdentity] = None

    def _bound(self, repo_name: str) -> None:
        if repo_name != self.repository:
            raise ControllerAuthError("repository", "binding_mismatch")

    def credential_for(self, repo_name: str, required_permissions: Mapping[str, str]) -> ControllerCredential:
        self._bound(repo_name)
        scope = tuple(sorted((str(k), str(v)) for k, v in required_permissions.items()))
        key = hashlib.sha256(json.dumps(["personal", self.api_origin, repo_name, scope]).encode()).hexdigest()
        return ControllerCredential(self._token, "personal", self.api_origin, repo_name, scope, key, snapshot_id="personal")

    def identity_for(self, repo_name: str) -> ControllerIdentity:
        self._bound(repo_name)
        if self._identity is None:
            try:
                response = self._client.get(f"{self.api_origin}/user", headers={"Authorization": f"Bearer {self._token}"})
                response.raise_for_status()
                body = response.json()
                login, account_id = body.get("login"), body.get("id")
                if not isinstance(login, str) or not login or type(account_id) is not int or account_id <= 0:
                    raise ControllerAuthError("personal_identity")
                self._identity = ControllerIdentity(login, account_id=account_id)
            except ControllerAuthError:
                raise
            except Exception:
                failed = True
            else:
                failed = False
            if failed:
                raise ControllerAuthError("personal_identity", "github_rejection")
        assert self._identity is not None
        return self._identity

    def invalidate(self, credential: ControllerCredential) -> None:
        return None


class GitHubAppControllerAuth:
    def __init__(self, repository: str, config: ControllerAppConfig, client: httpx.Client, api_origin: str, clock: Callable[[], float]) -> None:
        self.repository, self.config, self._client, self.api_origin, self._clock = repository, config, client, api_origin.rstrip("/"), clock
        self._lock = threading.Lock()
        self._credential: Optional[ControllerCredential] = None
        self._identity: Optional[ControllerIdentity] = None

    def _bound(self, repo_name: str) -> None:
        if repo_name != self.repository:
            raise ControllerAuthError("repository", "binding_mismatch")

    def _jwt(self) -> str:
        failed = False
        key = ""
        try:
            key = self.config.private_key_path.read_text()
            now = int(self._clock())
            encoded = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.config.app_id)}, key, algorithm="RS256")
        except Exception:
            failed = True
            encoded = ""
        finally:
            key = ""
        if failed:
            raise ControllerAuthError("private_key", "unavailable_or_invalid")
        return encoded

    def _request(self, method: str, path: str, token: str, json_body: Optional[dict[str, object]] = None) -> dict:
        failed = False
        try:
            response = self._client.request(method, f"{self.api_origin}{path}", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, json=json_body)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            failed = True
            body = {}
        if failed:
            token = ""
            response = httpx.Response(599)
            raise ControllerAuthError("github_api", "rejected_or_malformed")
        return body

    def _verify_identity(self, app_jwt: str) -> ControllerIdentity:
        try:
            body = self._request("GET", "/app", app_jwt)
            app_id, slug = body.get("id"), body.get("slug")
            if type(app_id) is not int or app_id <= 0 or app_id != self.config.app_id or not isinstance(slug, str) or not slug.strip():
                raise ControllerAuthError("app_identity", "missing_or_mismatched")
            return ControllerIdentity(f"{slug}[bot]", app_id=app_id)
        finally:
            app_jwt = ""

    @staticmethod
    def _permissions_sufficient(granted: object, required: tuple[tuple[str, str], ...]) -> bool:
        if not isinstance(granted, dict):
            return False
        rank = {"read": 1, "write": 2, "admin": 3}
        return all(isinstance(granted.get(k), str) and rank.get(granted[k], 0) >= rank.get(v, 99) for k, v in required)

    def credential_for(self, repo_name: str, required_permissions: Mapping[str, str]) -> ControllerCredential:
        self._bound(repo_name)
        scope = tuple(sorted((str(k), str(v)) for k, v in required_permissions.items()))
        app_jwt = ""
        token = ""
        minted: dict = {}
        try:
            return self._credential_for_locked(repo_name, scope)
        finally:
            # Traceback diagnostics may inspect frame locals. Never retain wire
            # credentials or response bodies in a failing public frame.
            app_jwt = ""
            token = ""
            minted = {}

    def _credential_for_locked(self, repo_name: str, scope: tuple[tuple[str, str], ...]) -> ControllerCredential:
        app_jwt = ""
        token: object = ""
        minted: dict = {}
        installation: dict = {}
        listing: dict = {}
        try:
            with self._lock:
                if self._credential and self._credential.permissions == scope and self._credential.expires_at and self._credential.expires_at.timestamp() - self._clock() > 60:
                    return self._credential
                app_jwt = self._jwt()
                self._identity = self._verify_identity(app_jwt)
                installation = self._request("GET", f"/repos/{repo_name}/installation", app_jwt)
                installation_id = installation.get("id")
                if type(installation_id) is not int or installation_id <= 0:
                    raise ControllerAuthError("installation")
                minted = self._request("POST", f"/app/installations/{installation_id}/access_tokens", app_jwt, json_body={"repositories": [repo_name.split("/", 1)[1]], "permissions": dict(scope)})
                token, expires_raw = minted.get("token"), minted.get("expires_at")
                try:
                    expires = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
                except ValueError:
                    expires = None
                if expires is None or expires.tzinfo is None or expires.utcoffset() is None:
                    raise ControllerAuthError("token", "invalid_expiry")
                expires = expires.astimezone(timezone.utc)
                if not isinstance(token, str) or not token or expires.timestamp() - self._clock() <= 60 or not self._permissions_sufficient(minted.get("permissions"), scope):
                    raise ControllerAuthError("token", "invalid_or_insufficient")
                selection = minted.get("repository_selection")
                if selection is not None and selection != "selected":
                    raise ControllerAuthError("repository_grant", "contradictory")
                if "repositories" in minted:
                    repositories = minted["repositories"]
                    if minted.get("repository_selection") != "selected" or not isinstance(repositories, list) or len(repositories) != 1 or not isinstance(repositories[0], dict) or repositories[0].get("full_name") != repo_name:
                        raise ControllerAuthError("repository_grant", "contradictory")
                else:
                    listing = self._request("GET", "/installation/repositories?per_page=100", token)
                    repos = listing.get("repositories")
                    total_count = listing.get("total_count")
                    if type(total_count) is not int or total_count != 1 or not isinstance(repos, list) or len(repos) != 1 or not isinstance(repos[0], dict) or repos[0].get("full_name") != repo_name:
                        raise ControllerAuthError("repository_grant", "broader_or_unproven")
                if expires.timestamp() - self._clock() <= 60:
                    raise ControllerAuthError("token", "insufficient_lifetime")
                authority = hashlib.sha256(json.dumps(["app", self.api_origin, self.config.app_id, installation_id, repo_name, scope]).encode()).hexdigest()
                self._credential = ControllerCredential(token, "app", self.api_origin, repo_name, scope, authority, expires, self.config.app_id, installation_id, uuid.uuid4().hex)
                return self._credential
        finally:
            app_jwt = ""
            token = ""
            minted = {}
            installation = {}
            listing = {}

    def identity_for(self, repo_name: str) -> ControllerIdentity:
        self._bound(repo_name)
        with self._lock:
            if self._identity is None:
                self._identity = self._verify_identity(self._jwt())
            return self._identity

    def invalidate(self, credential: ControllerCredential) -> None:
        with self._lock:
            if self._credential and self._credential.snapshot_id == credential.snapshot_id:
                self._credential = None


def select_controller_auth(repo_name: str, *, provided_token: Optional[str] = None, config_path: Optional[Path] = None, home: Optional[Path] = None, client: Optional[httpx.Client] = None, api_origin: str = "https://api.github.com", clock: Callable[[], float] = time.time) -> ControllerAuthProvider:
    """Select App mode when configured, otherwise preserve personal-token precedence."""
    base_home = home or Path.home()
    config = _load_controller_config(repo_name, config_path, base_home)
    transport = instrument_github_client(client or httpx.Client(timeout=30), subsystem="controller-auth", api_origin=api_origin)
    if config is not None:
        return GitHubAppControllerAuth(repo_name, config, transport, api_origin, clock)
    token = provided_token or get_github_token()
    if not token:
        raise ControllerAuthError("personal_token", "unavailable")
    return PersonalControllerAuth(repo_name, token, transport, api_origin.rstrip("/"))
