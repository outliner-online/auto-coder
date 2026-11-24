"""Native cancellation evidence at the production code-repair boundary."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_coder.ci_cancellation_recovery import RecoveryPolicy
from auto_coder.ci_observation import CIConclusion, WorkflowObservation
from auto_coder.ci_repair_authority import current_ci_failure_authority
from auto_coder.github_ci_observer import fence_active_ci_observations

pytestmark = pytest.mark.pr_repair_contract

_REPOSITORY = "owner/repo"
_HEAD = "a" * 40
_POLICY = RecoveryPolicy(".github/workflows/tests.yml", ("root-a", "root-b"), ("required-aggregate",), True)


class NativeRepairAPI:
    """Controlled GitHub transport; both production readers remain unmodified."""

    def __init__(self, *, independent_failure: bool = False, conclusion: str = "failure") -> None:
        self.pr = {"number": 7, "state": "open", "merged_at": None, "head": {"sha": _HEAD, "repo": {"full_name": _REPOSITORY}}, "base": {"repo": {"full_name": _REPOSITORY}}}
        self.run = {
            "id": 300,
            "workflow_id": 90,
            "path": _POLICY.workflow_path,
            "event": "pull_request",
            "head_sha": _HEAD,
            "run_number": 3,
            "run_attempt": 1,
            "pull_requests": [{"number": 7, "head": {"sha": _HEAD}}],
            "repository": {"full_name": _REPOSITORY},
            "head_repository": {"full_name": _REPOSITORY},
            "status": "completed",
            "conclusion": conclusion,
            "name": "PR Tests",
        }
        results = (("root-a", "cancelled"), ("root-b", "cancelled"), ("unrelated-green", "success"), ("independent-test", "failure" if independent_failure else "success"), ("required-aggregate", "failure"))
        self.jobs = [{"id": index, "run_id": 300, "run_attempt": 1, "head_sha": _HEAD, "name": name, "status": "completed", "conclusion": result, "check_run_url": f"https://api.github.com/repos/{_REPOSITORY}/check-runs/{100 + index}"} for index, (name, result) in enumerate(results, 1)]
        self.actions = SimpleNamespace(list_workflow_runs_for_repo=self.list_runs)
        self.checks = SimpleNamespace(list_for_ref=self.list_checks)
        self.detail_attempt = 1
        self.fence_on_job_read = False
        self.requests: list[str] = []

    def list_runs(self, owner: str, repo: str, **query: object) -> dict:
        assert (owner, repo) == ("owner", "repo")
        assert query["head_sha"] == _HEAD
        return {"workflow_runs": [deepcopy(self.run)]}

    def list_checks(self, owner: str, repo: str, **query: object) -> dict:
        assert (owner, repo) == ("owner", "repo")
        assert query["ref"] == _HEAD
        return {"check_runs": [{"id": 100 + job["id"], "app": {"id": 15368}, "head_sha": _HEAD, "name": "intentionally unrelated display name", "status": job["status"], "conclusion": job["conclusion"]} for job in self.jobs]}

    def __call__(self, path: str, *, verb: str, query: dict | None = None) -> dict:
        assert verb == "GET", "Repair admission must not recover jobs or mutate GitHub"
        self.requests.append(path)
        if path == f"/repos/{_REPOSITORY}/pulls/7":
            return deepcopy(self.pr)
        if path == f"/repos/{_REPOSITORY}/actions/runs":
            assert query and query["head_sha"] == _HEAD
            return {"total_count": 1, "workflow_runs": [deepcopy(self.run)]}
        if path == f"/repos/{_REPOSITORY}/actions/runs/300":
            return {**deepcopy(self.run), "run_attempt": self.detail_attempt}
        if path == f"/repos/{_REPOSITORY}/actions/runs/300/attempts/1/jobs":
            if self.fence_on_job_read:
                fence_active_ci_observations("controlled-native-job-read-invalidation")
            return {"total_count": len(self.jobs), "jobs": deepcopy(self.jobs)}
        raise AssertionError(f"Unexpected endpoint: {path}")

    def client(self) -> SimpleNamespace:
        def strict_metadata(repository: str, pr_number: int) -> dict:
            assert (repository, pr_number) == (_REPOSITORY, 7)
            return deepcopy(self.pr)

        return SimpleNamespace(token="controlled-token", get_pull_request_metadata_strict=strict_metadata)


@pytest.mark.parametrize("conclusion", ["cancelled", "failure"])
def test_production_repair_boundary_rejects_cancelled_roots_and_interruption_aggregate(conclusion: str) -> None:
    api = NativeRepairAPI(conclusion=conclusion)
    provider_effects: list[str] = []
    with patch("auto_coder.ci_repair_authority.get_ghapi_client", return_value=api):
        with current_ci_failure_authority(api.client(), _REPOSITORY, 7, _HEAD, recovery_policies=(_POLICY,)) as authority:
            if authority.allowed:
                provider_effects.append("repair")
            assert authority.allowed is False
            assert authority.failure_identities == ()
            assert authority.snapshot is not None
            native = [fact.conclusion for fact in authority.snapshot.facts]
            assert CIConclusion.FAILURE in native and CIConclusion.CANCELLED in native
    assert provider_effects == []
    assert f"/repos/{_REPOSITORY}/actions/runs/300/attempts/1/jobs" in api.requests


@pytest.mark.parametrize("conclusion", ["cancelled", "failure"])
def test_production_repair_boundary_admits_independent_failure_beside_two_cancellations(conclusion: str) -> None:
    api = NativeRepairAPI(independent_failure=True, conclusion=conclusion)
    provider_effects: list[tuple[str, ...]] = []
    with patch("auto_coder.ci_repair_authority.get_ghapi_client", return_value=api):
        with current_ci_failure_authority(api.client(), _REPOSITORY, 7, _HEAD, recovery_policies=(_POLICY,)) as authority:
            assert authority.allowed is True
            provider_effects.append(authority.failure_identities)
            assert "check:15368:104" in authority.failure_identities
            assert "check:15368:105" not in authority.failure_identities
            assert "check:15368:101" not in authority.failure_identities
            assert "check:15368:102" not in authority.failure_identities
            assert authority.snapshot is not None
            workflow = next(fact for fact in authority.snapshot.facts if isinstance(fact, WorkflowObservation))
            assert workflow.conclusion.value == conclusion
    assert len(provider_effects) == 1


@pytest.mark.parametrize("ambiguity", ["missing-check-id", "changed-attempt", "changed-head", "ambiguous-pr"])
def test_ambiguous_native_cancellation_evidence_defers_repair(ambiguity: str) -> None:
    api = NativeRepairAPI(independent_failure=True)
    if ambiguity == "missing-check-id":
        api.jobs[-1]["check_run_url"] = ""
    elif ambiguity == "changed-attempt":
        api.detail_attempt = 2
    elif ambiguity == "changed-head":
        api.pr["head"]["sha"] = "b" * 40
    else:
        api.run["pull_requests"] = []
    with patch("auto_coder.ci_repair_authority.get_ghapi_client", return_value=api):
        with current_ci_failure_authority(api.client(), _REPOSITORY, 7, _HEAD, recovery_policies=(_POLICY,)) as authority:
            assert authority.allowed is False
            assert authority.reason


def test_native_inspection_cannot_resurrect_an_invalidated_repair_snapshot() -> None:
    api = NativeRepairAPI(independent_failure=True)
    api.fence_on_job_read = True
    with patch("auto_coder.ci_repair_authority.get_ghapi_client", return_value=api):
        with current_ci_failure_authority(api.client(), _REPOSITORY, 7, _HEAD, recovery_policies=(_POLICY,)) as authority:
            assert authority.allowed is False
            assert authority.reason == "CI evidence was invalidated before repair initiation"


def test_durable_repair_composition_forwards_repository_recovery_policy(tmp_path) -> None:
    from contextlib import contextmanager

    from auto_coder.ci_repair_authority import CIRepairAuthority
    from auto_coder.local_job_handoff import LocalJobKind, LocalJobStore
    from auto_coder.pr_local_job_runtime import build_pr_local_job_adapters

    observed_policies = []

    @contextmanager
    def authority(client, repository, number, head, *, recovery_policies):
        assert (repository, number, head) == (_REPOSITORY, 7, _HEAD)
        observed_policies.append(recovery_policies)
        yield CIRepairAuthority(False, "controlled refusal", head)

    config = SimpleNamespace(CI_CANCELLATION_RECOVERY=(_POLICY,))
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    with patch("auto_coder.ci_repair_authority.current_ci_failure_authority", authority):
        adapters = build_pr_local_job_adapters(NativeRepairAPI().client(), config, store, repository_path=tmp_path)
        ci_adapter = adapters[LocalJobKind.PR_CI_FAILURE]
        with ci_adapter._ports.authority(None, _REPOSITORY, 7, _HEAD) as result:
            assert result.allowed is False
    assert observed_policies == [(_POLICY,)]


def test_unconfigured_optional_cancellation_does_not_hide_a_failed_required_aggregate() -> None:
    api = NativeRepairAPI()
    for job in api.jobs[:2]:
        job["conclusion"] = "success"
    api.jobs.append({"id": 6, "run_id": 300, "run_attempt": 1, "head_sha": _HEAD, "name": "optional-diagnostic", "status": "completed", "conclusion": "cancelled", "check_run_url": f"https://api.github.com/repos/{_REPOSITORY}/check-runs/106"})
    with patch("auto_coder.ci_repair_authority.get_ghapi_client", return_value=api):
        with current_ci_failure_authority(api.client(), _REPOSITORY, 7, _HEAD, recovery_policies=(_POLICY,)) as authority:
            assert authority.allowed is True
            assert "check:15368:105" in authority.failure_identities
            assert "check:15368:106" not in authority.failure_identities
