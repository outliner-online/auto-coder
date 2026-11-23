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


@pytest.mark.parametrize("invalid_location", ["global", "override"])
def test_invalid_config_encoding_is_sanitized_without_fallback(tmp_path: Path, invalid_location: str) -> None:
    config_root = tmp_path / ".auto-coder"
    global_config = config_root / "config.toml"
    global_config.parent.mkdir()
    global_config.write_text("")
    invalid_config = global_config
    if invalid_location == "override":
        invalid_config = config_root / "owner" / "project" / "config.toml"
        invalid_config.parent.mkdir(parents=True)
    invalid_config.write_bytes(b"\xff\xfePRIVATE-CONFIG-SENTINEL")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"login": "fallback", "id": 1})

    with pytest.raises(ControllerAuthError) as captured:
        select_controller_auth(
            REPO,
            provided_token="must-not-win",
            home=tmp_path,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    error = captured.value
    assert error.stage == "configuration"
    assert error.category == "unreadable_or_malformed"
    assert "PRIVATE-CONFIG-SENTINEL" not in str(error)
    assert error.__context__ is None
    assert error.__cause__ is None
    assert requests == []


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


def app_provider(tmp_path: Path, handler, clock=lambda: 1_800_000_000.0):
    key_path = tmp_path / "controller.pem"
    key(key_path)
    config = tmp_path / "config.toml"
    config.write_text(f'[github-app-auto-coder-controller]\napp_id = 123\nprivate_key_path = "{key_path}"\n')
    return select_controller_auth(REPO, config_path=config, home=tmp_path, clock=clock, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_contradictory_mint_scope_is_rejected_without_fallback(tmp_path: Path) -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 123, "slug": "controller"})
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 456})
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "minted", "expires_at": "2027-01-15T08:02:00Z", "permissions": {}, "repository_selection": "selected", "repositories": [{"full_name": REPO}, {"full_name": "owner/other"}]})
        return httpx.Response(200, json={"total_count": 1, "repositories": [{"full_name": REPO}]})

    with pytest.raises(ControllerAuthError, match="contradictory"):
        app_provider(tmp_path, handler).credential_for(REPO, {})
    assert "/installation/repositories" not in paths


@pytest.mark.parametrize("repositories", [[{"full_name": REPO}, {"full_name": REPO}], [{"full_name": REPO}, {}]])
def test_incomplete_mint_requires_strict_complete_listing(tmp_path: Path, repositories) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 123, "slug": "controller"})
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 456})
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "minted", "expires_at": "2027-01-15T08:02:00Z", "permissions": {}})
        return httpx.Response(200, json={"total_count": 1, "repositories": repositories})

    with pytest.raises(ControllerAuthError, match="broader_or_unproven"):
        app_provider(tmp_path, handler).credential_for(REPO, {})


def test_incomplete_mint_accepts_exact_listing_and_rechecks_expiry(tmp_path: Path) -> None:
    now = [1_800_000_000.0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 123, "slug": "controller"})
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 456})
        if request.url.path.endswith("/access_tokens"):
            expiry = datetime.fromtimestamp(now[0] + 61, timezone.utc).isoformat()
            return httpx.Response(200, json={"token": "minted", "expires_at": expiry, "permissions": {}})
        now[0] += 2
        return httpx.Response(200, json={"total_count": 1, "repositories": [{"full_name": REPO}]})

    with pytest.raises(ControllerAuthError, match="insufficient_lifetime"):
        app_provider(tmp_path, handler, lambda: now[0]).credential_for(REPO, {})


@pytest.mark.parametrize("bad_id", [True, 123.0])
def test_first_acquisition_rejects_non_integer_app_identity(tmp_path: Path, bad_id) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": bad_id, "slug": "controller"})

    with pytest.raises(ControllerAuthError, match="app_identity"):
        app_provider(tmp_path, handler).credential_for(REPO, {})


def test_rejects_boolean_installation_id(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 123, "slug": "controller"})
        return httpx.Response(200, json={"id": True})

    with pytest.raises(ControllerAuthError, match="installation"):
        app_provider(tmp_path, handler).credential_for(REPO, {})


def test_sanitized_private_key_traceback(tmp_path: Path) -> None:
    import traceback

    key_path = tmp_path / "secret.pem"
    key_path.write_text("DISTINCTIVE-PRIVATE-KEY-SENTINEL")
    config = tmp_path / "config.toml"
    config.write_text(f'[github-app-auto-coder-controller]\napp_id = 123\nprivate_key_path = "{key_path}"\n')
    provider = select_controller_auth(REPO, config_path=config, home=tmp_path)
    try:
        provider.credential_for(REPO, {})
    except ControllerAuthError as error:
        rendered = "".join(traceback.format_exception(error))
        assert error.stage == "private_key"
        assert error.category == "unavailable_or_invalid"
        assert "DISTINCTIVE-PRIVATE-KEY-SENTINEL" not in rendered
        assert error.__cause__ is None
    else:
        raise AssertionError("invalid key unexpectedly accepted")


def test_concurrent_renewal_waits_and_shares_valid_snapshot(tmp_path: Path) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    now = [1_800_000_000.0]
    mint_count = [0]
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 123, "slug": "controller"})
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 456})
        mint_count[0] += 1
        if mint_count[0] == 2:
            refresh_started.set()
            assert release_refresh.wait(2)
        expiry = datetime.fromtimestamp(now[0] + 120, timezone.utc).isoformat()
        return httpx.Response(200, json={"token": f"token-{mint_count[0]}", "expires_at": expiry, "permissions": {}, "repository_selection": "selected", "repositories": [{"full_name": REPO}]})

    provider = app_provider(tmp_path, handler, lambda: now[0])
    provider.credential_for(REPO, {})
    now[0] += 61
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(provider.credential_for, REPO, {})
        assert refresh_started.wait(2)
        contender_entered = threading.Event()
        original_bound = provider._bound
        bound_calls = [0]
        bound_lock = threading.Lock()

        def observed_bound(repo_name):
            with bound_lock:
                bound_calls[0] += 1
                if bound_calls[0] == 1:
                    contender_entered.set()
            original_bound(repo_name)

        provider._bound = observed_bound
        second = executor.submit(provider.credential_for, REPO, {})
        assert contender_entered.wait(2)
        assert not second.done()
        release_refresh.set()
        first_result = first.result(timeout=2)
        second_result = second.result(timeout=2)
    assert first_result is second_result
    assert first_result.token == "token-2"
    assert mint_count == [2]


@pytest.mark.parametrize("contradiction_at", [1, 2])
def test_explicit_broad_selection_without_rows_never_falls_back(tmp_path: Path, contradiction_at: int) -> None:
    now = [1_800_000_000.0]
    mint_count = [0]
    listing_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 123, "slug": "controller"})
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 456})
        if request.url.path.endswith("/access_tokens"):
            mint_count[0] += 1
            expiry = datetime.fromtimestamp(now[0] + 120, timezone.utc).isoformat()
            if mint_count[0] == contradiction_at:
                return httpx.Response(200, json={"token": "broad", "expires_at": expiry, "permissions": {}, "repository_selection": "all"})
            return httpx.Response(200, json={"token": "exact", "expires_at": expiry, "permissions": {}, "repository_selection": "selected", "repositories": [{"full_name": REPO}]})
        listing_count[0] += 1
        return httpx.Response(200, json={"total_count": 1, "repositories": [{"full_name": REPO}]})

    provider = app_provider(tmp_path, handler, lambda: now[0])
    if contradiction_at == 2:
        provider.credential_for(REPO, {})
        now[0] += 61
    with pytest.raises(ControllerAuthError, match="contradictory"):
        provider.credential_for(REPO, {})
    assert listing_count == [0]


@pytest.mark.parametrize("account_id", [True, 42.0])
def test_personal_identity_rejects_non_integer_account_id(tmp_path: Path, account_id) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"login": "person", "id": account_id})

    provider = select_controller_auth(REPO, provided_token="token", home=tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ControllerAuthError, match="personal_identity"):
        provider.identity_for(REPO)


def test_naive_expiry_is_rejected_and_aware_expiry_is_normalized(tmp_path: Path) -> None:
    expiry_values = ["2027-01-15T08:02:00", "2027-01-15T10:02:00+02:00"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 123, "slug": "controller"})
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 456})
        return httpx.Response(200, json={"token": "minted", "expires_at": expiry_values.pop(0), "permissions": {}, "repository_selection": "selected", "repositories": [{"full_name": REPO}]})

    provider = app_provider(tmp_path, handler)
    with pytest.raises(ControllerAuthError, match="invalid_expiry"):
        provider.credential_for(REPO, {})
    credential = provider.credential_for(REPO, {})
    assert credential.expires_at == datetime(2027, 1, 15, 8, 2, tzinfo=timezone.utc)


def test_transport_failure_has_sanitized_exception_graph_and_log(tmp_path: Path) -> None:
    import io
    import traceback

    from loguru import logger

    sentinel = "DISTINCTIVE-TRANSPORT-SENTINEL"

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(sentinel)

    output = io.StringIO()
    sink = logger.add(output, format="{message}", backtrace=True, diagnose=True)
    try:
        provider = app_provider(tmp_path, handler)
        with pytest.raises(ControllerAuthError) as captured:
            provider.credential_for(REPO, {})
        error = captured.value
        logger.opt(exception=error).error("controller authentication unavailable")
    finally:
        logger.remove(sink)
    assert error.stage == "github_api"
    assert error.__context__ is None
    assert error.__cause__ is None
    assert sentinel not in "".join(traceback.format_exception(error))
    assert sentinel not in output.getvalue()
