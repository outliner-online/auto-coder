"""Production-boundary coverage for renewable CI observation credentials."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx

from auto_coder.automation_config import AutomationConfig
from auto_coder.ci_observation import CIConclusion, ObservationAvailability, WorkflowObservation
from auto_coder.ci_repair_authority import evaluate_ci_repair_snapshot
from auto_coder.github_controller_auth import ControllerCredential, ControllerIdentity
from auto_coder.util.gh_cache import GitHubClient
from auto_coder.util.github_action import _check_github_actions_status


class _ObservationProvider:
    def __init__(self, role: str, *, fail_after_first_send: bool = False) -> None:
        self.role = role
        self.fail_after_first_send = fail_after_first_send
        self.requests: list[tuple[str, dict[str, str]]] = []

    def credential_for(self, repo_name, required_permissions):
        request_number = len(self.requests)
        self.requests.append((repo_name, dict(required_permissions)))
        # The first selection supplies the stable authority namespace. The next
        # selection is the first wire send, so rejecting selection two models a
        # renewal failure before the second page can be sent.
        if self.fail_after_first_send and request_number >= 2:
            raise RuntimeError("installation credential renewal unavailable")
        return ControllerCredential(
            f"{self.role}-credential-{request_number}",
            "app",
            repository=repo_name,
            permissions=tuple(sorted(required_permissions.items())),
            expires_at=datetime.now(timezone.utc),
            app_id=17,
            installation_id=100 + request_number,
            snapshot_id=f"{self.role}-{request_number}",
        )

    def identity_for(self, repo_name):
        return ControllerIdentity(f"{self.role}[bot]", app_id=17)

    def invalidate(self, credential):
        return None


def _workflow(run_id: int, head_sha: str) -> dict[str, object]:
    return {
        "id": run_id,
        "workflow_id": 9000 + run_id,
        "run_attempt": 1,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "success",
        "name": f"ci-{run_id}",
        "path": ".github/workflows/ci.yml",
    }


def _observe(repository: str, role: str, *, fail_renewal: bool = False):
    head_sha = f"{role}-exact-head"
    provider = _ObservationProvider(role, fail_after_first_send=fail_renewal)
    client = GitHubClient(auth_provider=provider, repository=repository)
    sent: list[tuple[str, int, str]] = []
    first_page = [_workflow(run_id, head_sha) for run_id in range(1, 101)]

    def send(method, url, **kwargs):
        request = httpx.Request(method, url, params=kwargs.get("params"))
        page = int((kwargs.get("params") or {}).get("page", 1))
        sent.append((request.url.path, page, kwargs["headers"]["Authorization"]))
        if request.url.path.endswith("/actions/runs"):
            payload = first_page if page == 1 else [_workflow(101, head_sha)]
            return httpx.Response(200, json={"workflow_runs": payload}, request=request)
        if request.url.path.endswith(f"/commits/{head_sha}/check-runs"):
            return httpx.Response(200, json={"check_runs": []}, request=request)
        raise AssertionError(f"unexpected CI observation request: {request.url}")

    transport = MagicMock()
    transport.request.side_effect = send
    with (
        patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport),
        patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=transport),
    ):
        result = _check_github_actions_status(
            repository,
            {"number": 7, "head": {"sha": head_sha}},
            AutomationConfig(),
            github_client=client,
        )
    return result, provider, sent, head_sha


def test_ci_observation_renews_wire_auth_per_page_and_separates_repositories():
    first, first_provider, first_sends, first_head = _observe("owner/primary", "ci-reader")
    second, second_provider, second_sends, second_head = _observe("owner/secondary", "audit-reader")

    for result, repository, head_sha in (
        (first, "owner/primary", first_head),
        (second, "owner/secondary", second_head),
    ):
        assert result.success is True
        assert result.observation is not None
        assert result.observation.availability is ObservationAvailability.KNOWN
        assert result.observation.subject.repository == repository
        assert result.observation.subject.head_sha == head_sha
        assert len(result.observation.facts) == 101
        assert all(isinstance(fact, WorkflowObservation) for fact in result.observation.facts)
        assert {fact.execution.run_id for fact in result.observation.facts} == {str(run_id) for run_id in range(1, 102)}
        assert {fact.conclusion for fact in result.observation.facts} == {CIConclusion.SUCCESS}

    assert first_sends == [
        ("/repos/owner/primary/actions/runs", 1, "Bearer ci-reader-credential-1"),
        ("/repos/owner/primary/actions/runs", 2, "Bearer ci-reader-credential-2"),
        (
            f"/repos/owner/primary/commits/{first_head}/check-runs",
            1,
            "Bearer ci-reader-credential-3",
        ),
    ]
    assert second_sends == [
        (
            "/repos/owner/secondary/actions/runs",
            1,
            "Bearer audit-reader-credential-1",
        ),
        (
            "/repos/owner/secondary/actions/runs",
            2,
            "Bearer audit-reader-credential-2",
        ),
        (
            f"/repos/owner/secondary/commits/{second_head}/check-runs",
            1,
            "Bearer audit-reader-credential-3",
        ),
    ]
    assert {repository for repository, _ in first_provider.requests} == {"owner/primary"}
    assert {repository for repository, _ in second_provider.requests} == {"owner/secondary"}
    assert {authorization for _, _, authorization in first_sends}.isdisjoint({authorization for _, _, authorization in second_sends})


def test_ci_observation_failed_later_renewal_is_partial_and_not_repair_eligible():
    result, provider, sends, head_sha = _observe("owner/primary", "ci-reader", fail_renewal=True)

    assert result.success is False
    assert result.observation is not None
    assert result.observation.availability is ObservationAvailability.PARTIAL
    assert result.observation.subject.head_sha == head_sha
    assert result.observation.facts == ()
    assert evaluate_ci_repair_snapshot(result.observation) == (False, "CI evidence is partial")
    assert sends == [
        (
            "/repos/owner/primary/actions/runs",
            1,
            "Bearer ci-reader-credential-1",
        )
    ]
    assert provider.requests == [
        ("owner/primary", {"actions": "read", "checks": "read"}),
        ("owner/primary", {"actions": "read", "checks": "read"}),
        ("owner/primary", {"actions": "read", "checks": "read"}),
    ]
