from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auto_coder.github_controller_auth import ControllerAuthError, select_controller_auth

REPO = "owner/project"


def key(path: Path) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def test_absent_config_preserves_personal_mode(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        return httpx.Response(200, json={"login": "person", "id": 42})

    provider = select_controller_auth(REPO, provided_token="secret-token", home=tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler)))
    credential = provider.credential_for(REPO, {})
    assert credential.mode == "personal"
    assert "secret-token" not in repr(credential)
    assert provider.identity_for(REPO).account_id == 42


@pytest.mark.parametrize("section", ["[github-app-auto-coder-controller]\n", "[github-app-auto-coder-controller]\napp_id = 0\n"])
def test_present_invalid_config_never_falls_back(tmp_path: Path, section: str) -> None:
    config = tmp_path / ".auto-coder" / "config.toml"
    config.parent.mkdir()
    config.write_text(section)
    with pytest.raises(ControllerAuthError, match="configuration"):
        select_controller_auth(REPO, provided_token="must-not-win", home=tmp_path)


def test_app_verifies_identity_scope_renews_and_raced_invalidation(tmp_path: Path) -> None:
    key_path = tmp_path / "controller.pem"
    key(key_path)
    config = tmp_path / "config.toml"
    config.write_text(f'[github-app-auto-coder-controller]\napp_id = 123\nprivate_key_path = "{key_path}"\n')
    now = [1_800_000_000.0]
    minted = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 123, "slug": "controller"})
        if request.url.path == "/repos/owner/project/installation":
            return httpx.Response(200, json={"id": 456})
        if request.url.path.endswith("/access_tokens"):
            minted[0] += 1
            assert request.read().decode().find('"repositories":["project"]') >= 0
            expiry = datetime.fromtimestamp(now[0] + 120, timezone.utc).isoformat()
            return httpx.Response(200, json={"token": f"token-{minted[0]}", "expires_at": expiry, "permissions": {"issues": "write"}, "repository_selection": "selected", "repositories": [{"full_name": REPO}]})
        raise AssertionError(request.url)

    provider = select_controller_auth(REPO, config_path=config, home=tmp_path, clock=lambda: now[0], client=httpx.Client(transport=httpx.MockTransport(handler)))
    first = provider.credential_for(REPO, {"issues": "write"})
    assert provider.identity_for(REPO).login == "controller[bot]"
    now[0] += 61
    second = provider.credential_for(REPO, {"issues": "write"})
    assert (first.token, second.token) == ("token-1", "token-2")
    assert first.authority_key == second.authority_key
    provider.invalidate(first)
    assert provider.credential_for(REPO, {"issues": "write"}) is second


def test_app_rejects_mismatched_first_identity_before_mint(tmp_path: Path) -> None:
    key_path = tmp_path / "controller.pem"
    key(key_path)
    config = tmp_path / "config.toml"
    config.write_text(f'[github-app-auto-coder-controller]\napp_id = 123\nprivate_key_path = "{key_path}"\n')
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"id": 999, "slug": "wrong"})

    provider = select_controller_auth(REPO, config_path=config, home=tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ControllerAuthError, match="app_identity"):
        provider.credential_for(REPO, {})
    assert paths == ["/app"]
