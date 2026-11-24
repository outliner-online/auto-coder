"""Production recovery-entry regressions with a controlled native REST boundary."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from auto_coder.ci_cancellation_recovery import CancellationRecoveryStore, RecoveryPolicy, inspect_cancellation_evidence, recover_cancelled_jobs

REPOSITORY = "owner/repo"
HEAD = "current-head"
WORKFLOW_PATH = ".github/workflows/tests.yml"
POLICY = RecoveryPolicy(WORKFLOW_PATH, ("Test A", "Test B"), ("Required aggregate",), True)


class NativeAPI:
    """Simulate only native responses; run the real production policy/store."""

    def __init__(self, *, roots=("cancelled", "cancelled"), cause="unknown"):
        self.metadata = {"number": 7, "state": "open", "merged_at": None, "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}}, "base": {"repo": {"full_name": REPOSITORY}}}
        self.run = {
            "id": 100,
            "workflow_id": 20,
            "run_number": 4,
            "run_attempt": 1,
            "path": WORKFLOW_PATH,
            "head_sha": HEAD,
            "event": "pull_request",
            "repository": {"full_name": REPOSITORY},
            "head_repository": {"full_name": REPOSITORY},
            "pull_requests": [{"number": 7, "head": {"sha": HEAD}}],
            "status": "completed",
            "conclusion": "failure",
            "cancellation_reason": cause,
        }
        self.jobs = [self.job(1, "Test A", roots[0]), self.job(2, "Test B", roots[1]), self.job(3, "Unrelated success", "success"), self.job(4, "Required aggregate", "failure"), self.job(5, "Independent failed test", "failure")]
        self.posts = []
        self.calls = []
        self.extra_runs = []
        self.after_read = None
        self.post_exception = None
        self.apply_before_exception = False
        self.lock = threading.Lock()

    def job(self, job_id, name, conclusion):
        return {
            "id": job_id,
            "run_id": 100,
            "run_attempt": self.run["run_attempt"],
            "head_sha": HEAD,
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
            "check_run_url": f"https://api.github.com/repos/{REPOSITORY}/check-runs/{job_id + 1000}",
            "started_at": "2026-11-24T10:00:00Z",
            "completed_at": "2026-11-24T10:00:10Z",
        }

    def advance(self, name, conclusion="success", *, execute=True):
        self.run["run_attempt"] += 1
        attempt = self.run["run_attempt"]
        for job in self.jobs:
            # GitHub clones retained rows to each current attempt. Only the
            # selected root and its aggregate actually execute again.
            job["id"] += 100
            job["run_attempt"] = attempt
            job["check_run_url"] = f"https://api.github.com/repos/{REPOSITORY}/check-runs/{job['id'] + 1000}"
            if execute and job["name"] in (name, "Required aggregate"):
                job["conclusion"] = conclusion
                job["started_at"] = f"2026-11-24T10:{attempt:02}:00Z"
                job["completed_at"] = f"2026-11-24T10:{attempt:02}:10Z"
        self.run["status"] = "completed"

    def __call__(self, path, *, verb="GET", query=None, data=None):
        with self.lock:
            self.calls.append((path, verb, copy.deepcopy(query)))
            if verb == "POST":
                self.posts.append((path, copy.deepcopy(data)))
                native_id = int(path.split("/")[-2])
                target = next(job for job in self.jobs if job["id"] == native_id)
                if self.apply_before_exception:
                    self.advance(target["name"])
                if self.post_exception is not None:
                    raise self.post_exception
                return {"message": "accepted"}
            if path.endswith("/pulls/7"):
                payload = self.metadata
            elif path.endswith("/actions/runs"):
                payload = {"total_count": 1 + len(self.extra_runs), "workflow_runs": self.extra_runs + [self.run]}
            elif path.endswith("/actions/runs/100"):
                payload = self.run
            elif "/attempts/" in path and path.endswith("/jobs"):
                page = query["page"]
                start = (page - 1) * query["per_page"]
                payload = {"total_count": len(self.jobs), "jobs": self.jobs[start : start + query["per_page"]]}
            else:
                raise AssertionError(f"Unexpected native endpoint: {path}")
            result = copy.deepcopy(payload)
            if self.after_read:
                self.after_read(path)
            return result


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    api = NativeAPI()
    client = SimpleNamespace(token="repository-bound-credential")
    clients = []

    def bound_client(token, **kwargs):
        clients.append((token, kwargs))
        return api

    monkeypatch.setattr("auto_coder.ci_cancellation_recovery.get_ghapi_client", bound_client)
    monkeypatch.setattr("auto_coder.ci_cancellation_recovery.boundary_hooks", lambda: (None, None))
    path = tmp_path / "claims.sqlite3"

    def run(policies=(POLICY,)):
        # A new store object on every call exercises restart durability.
        return recover_cancelled_jobs(client, REPOSITORY, 7, HEAD, policies, store=CancellationRecoveryStore(path))

    return api, run, path, clients


@pytest.mark.parametrize("cause", ["manual", "runner_shutdown", "unknown", None])
@pytest.mark.parametrize("governor_enabled", [False, True])
def test_production_entry_recovers_native_cancellation_without_cause_filter(recovery, cause, governor_enabled, tmp_path, monkeypatch):
    from auto_coder.github_request_governor import GitHubRequestGovernor

    api, run, _, clients = recovery
    api.run["cancellation_reason"] = cause
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3") if governor_enabled else None
    if governor is not None:
        monkeypatch.setattr("auto_coder.ci_cancellation_recovery.boundary_hooks", lambda: (governor.admit_blocking, governor.observe))
    try:
        result = run()
        assert result.status == "accepted"
        assert result.job_id == 1
        assert result.logical_job == "Test A"
        assert result.claim_id
        assert api.posts == [(f"/repos/{REPOSITORY}/actions/jobs/1/rerun", {"enable_debug_logging": False})]
        expected_clients = [("repository-bound-credential", {"extra_headers": {"Cache-Control": "no-cache"}})]
        if governor is not None:
            expected_clients.append(("repository-bound-credential", {"admission_hook": governor.admit, "observation_hook": governor.observe, "extra_headers": {"Cache-Control": "no-cache"}}))
        assert clients == expected_clients
        assert result.status != "success"
    finally:
        if governor is not None:
            governor.close()


def test_multiple_roots_use_fresh_native_ids_after_each_terminal_attempt(recovery):
    api, run, path, _ = recovery
    original_success = copy.deepcopy(api.jobs[2])
    assert run().status == "accepted"
    assert run().status == "deferred"
    assert len(api.posts) == 1
    api.advance("Test A")
    result = run()
    assert result.status == "accepted"
    assert result.logical_job == "Test B"
    assert result.job_id == 102
    api.advance("Test B")
    assert run().status == "no_candidate"
    assert [path for path, _ in api.posts] == [f"/repos/{REPOSITORY}/actions/jobs/1/rerun", f"/repos/{REPOSITORY}/actions/jobs/102/rerun"]
    retained = api.jobs[2]
    assert (retained["started_at"], retained["completed_at"], retained["conclusion"]) == (original_success["started_at"], original_success["completed_at"], "success")
    import sqlite3

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT logical_job, attempt, job_id FROM ci_job_recovery_claims ORDER BY attempt").fetchall() == [("Test A", 1, 1), ("Test B", 2, 102)]


def test_repeat_cancellation_of_same_logical_root_never_retries(recovery):
    api, run, _, _ = recovery
    api.jobs[1]["conclusion"] = "success"
    assert run().status == "accepted"
    api.advance("Test A", "cancelled")
    assert run().status == "deferred"
    assert run().reason == "logical_job_already_claimed"
    assert len(api.posts) == 1


def test_lost_response_restart_reconciles_before_remaining_root(recovery):
    api, run, _, _ = recovery
    api.post_exception = TimeoutError("response lost")
    assert run().status == "indeterminate"
    assert run().status == "deferred"
    assert len(api.posts) == 1
    # Cloned IDs alone must not release the run when a different/manual rerun
    # advanced the attempt without reexecuting the claimed root.
    api.advance("Test A", execute=False)
    assert run().reason == "previous_effect_unreconciled"
    assert len(api.posts) == 1
    api.advance("Test A")
    api.post_exception = None
    assert run().status == "accepted"
    assert api.posts[-1][0].endswith("/202/rerun")
    assert len(api.posts) == 2


def test_lost_response_after_remote_effect_does_not_duplicate_root(recovery):
    api, run, _, _ = recovery
    api.post_exception = ConnectionError("lost after acceptance")
    api.apply_before_exception = True
    assert run().status == "indeterminate"
    api.post_exception = None
    api.apply_before_exception = False
    assert run().status == "accepted"
    assert [entry[0].split("/")[-2] for entry in api.posts] == ["1", "102"]


@pytest.mark.parametrize(
    "mutation, reason",
    [
        (lambda api: api.metadata["head"].update(sha="new-head"), "stale_head"),
        (lambda api: api.metadata.update(state="closed"), "pr_not_open"),
        (lambda api: api.run.update(pull_requests=[]), "ambiguous_pr_association"),
        (lambda api: api.run["pull_requests"].append({"number": 8, "head": {"sha": HEAD}}), "ambiguous_pr_association"),
        (lambda api: api.jobs[1].update(name="Test A"), "ambiguous_logical_job"),
        (lambda api: api.jobs[0].update(run_attempt=9), "ambiguous_job_association"),
        (lambda api: api.run.update(event="workflow_dispatch"), "not_current_normal_pr_workflow"),
        (lambda api: api.run.update(status="in_progress"), "active_execution"),
    ],
)
def test_uncertain_or_ineligible_evidence_never_sends(recovery, mutation, reason):
    api, run, _, _ = recovery
    mutation(api)
    result = run()
    assert result.status == "deferred"
    assert result.reason == reason
    assert api.posts == []


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "success", "skipped", "neutral", ""])
def test_other_conclusions_are_not_cancelled_jobs(recovery, conclusion):
    api, run, _, _ = recovery
    api.jobs[0]["conclusion"] = conclusion
    api.jobs[1]["conclusion"] = "success"
    assert run().status == "no_candidate"
    assert api.posts == []


def test_final_revalidation_prevents_changed_head_request_and_releases_unsent_claim(recovery):
    api, run, _, _ = recovery
    changed = False

    def change_after_jobs(path):
        nonlocal changed
        if not changed and "/attempts/" in path:
            changed = True
            api.metadata["head"]["sha"] = "new-head"

    api.after_read = change_after_jobs
    assert run().reason == "stale_head"
    assert api.posts == []
    api.after_read = None
    api.metadata["head"]["sha"] = HEAD
    assert run().status == "accepted"


def test_newer_effective_run_supersedes_old_cancelled_attempt(recovery):
    api, run, _, _ = recovery
    old = copy.deepcopy(api.run)
    old.update(id=99, run_number=3)
    api.extra_runs = [old]
    for job in api.jobs:
        job["conclusion"] = "success"
    api.run["conclusion"] = "success"
    assert run().status == "no_candidate"
    assert api.posts == []
    assert not any("/runs/99/" in call[0] for call in api.calls)


def test_attempt_changing_during_read_defers(recovery):
    api, run, _, _ = recovery
    changed = False

    def change_after_runs(path):
        nonlocal changed
        if path.endswith("/actions/runs") and not changed:
            changed = True
            api.run["run_attempt"] += 1

    api.after_read = change_after_runs
    assert run().reason == "superseded_attempt"
    assert api.posts == []


def test_complete_exact_attempt_pagination_finds_second_page_root(recovery):
    api, run, _, _ = recovery
    first = api.jobs[0]
    api.jobs = [api.job(index + 100, f"Success {index}", "success") for index in range(100)] + [first]
    assert run().status == "accepted"
    pages = [call[2]["page"] for call in api.calls if "/attempts/" in call[0]]
    assert pages == [1, 2, 1, 2]


def test_multi_root_live_gate_is_closed_without_fixture_verification(recovery):
    api, run, _, _ = recovery
    policy = RecoveryPolicy(WORKFLOW_PATH, POLICY.root_job_names, POLICY.aggregate_job_names)
    assert run((policy,)).reason == "multi_root_fixture_unverified"
    assert api.posts == []


def test_atomic_durable_claim_serializes_workers_for_same_run(recovery):
    api, run, _, _ = recovery
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert sorted(result.status for result in results) == ["accepted", "deferred"]
    assert len(api.posts) == 1


def test_claim_storage_failure_cannot_send(recovery):
    api, run, path, _ = recovery
    path.mkdir()
    assert run().reason == "claim_store_unavailable"
    assert api.posts == []


def test_read_only_inspection_preserves_independent_failure_and_native_check_ids():
    api = NativeAPI()
    evidence = inspect_cancellation_evidence(api, REPOSITORY, 7, HEAD, (POLICY,))
    assert evidence.available
    run = evidence.runs[0]
    assert [(job.name, job.check_run_id) for job in run.cancelled_roots] == [("Test A", 1001), ("Test B", 1002)]
    assert next(job for job in run.jobs if job.name == "Independent failed test").conclusion == "failure"
    assert api.posts == []


@pytest.mark.parametrize("change", ["head", "close", "attempt", "new_run", "active"])
def test_change_during_final_job_inventory_never_sends_stale_request(recovery, change):
    api, run, _, _ = recovery
    inventories = 0

    def race(path):
        nonlocal inventories
        if "/attempts/" not in path:
            return
        inventories += 1
        if inventories != 2:
            return
        if change == "head":
            api.metadata["head"]["sha"] = "new-head"
        elif change == "close":
            api.metadata["state"] = "closed"
        elif change == "attempt":
            api.run["run_attempt"] += 1
        elif change == "active":
            api.run["status"] = "in_progress"
        else:
            newer = copy.deepcopy(api.run)
            newer.update(id=200, run_number=5)
            api.extra_runs.append(newer)

    api.after_read = race
    assert run().reason == "identity_changed_before_send"
    assert api.posts == []


def test_unverified_multiroot_cannot_be_enabled_by_cancelling_roots_in_different_attempts(recovery):
    api, run, _, _ = recovery
    policy = RecoveryPolicy(WORKFLOW_PATH, POLICY.root_job_names, POLICY.aggregate_job_names)
    api.jobs[1]["conclusion"] = "success"
    assert run((policy,)).status == "accepted"
    api.advance("Test A")
    api.jobs[1]["conclusion"] = "cancelled"
    assert run((policy,)).reason == "multi_root_fixture_unverified"
    assert len(api.posts) == 1


@pytest.mark.parametrize("status, delivery, expected", [(422, "http_response_received", "rejected"), (503, "http_response_received", "indeterminate"), (None, "indeterminate_after_possible_send", "indeterminate"), (None, "definitely_not_sent", "deferred")])
def test_transport_outcomes_preserve_claim_or_release_only_proven_unsent(recovery, status, delivery, expected):
    from auto_coder.util.github_request_outcome import DeliveryCertainty, GitHubApiOutcome, GitHubRequestContext, GitHubRequestError, GitHubRequestOutcome, GitHubResponseMetadata, RequestProvenance

    api, run, _, _ = recovery
    api.jobs[1]["conclusion"] = "success"
    context = GitHubRequestContext("operation", "request", "ghapi", "https://api.github.com", "POST", "mutation", "/repos/owner/repo/actions/jobs/{id}/rerun")
    api.post_exception = GitHubRequestError(GitHubRequestOutcome(context, status, GitHubApiOutcome.REFUSED, RequestProvenance.NETWORK, DeliveryCertainty(delivery), GitHubResponseMetadata(), 0.0))
    assert run().status == expected
    api.post_exception = None
    subsequent = run()
    if delivery == "definitely_not_sent":
        assert subsequent.status == "accepted"
        assert len(api.posts) == 2
    else:
        assert subsequent.status == "deferred"
        assert len(api.posts) == 1


def test_selective_write_uses_existing_governor_without_admission_wait(recovery, tmp_path, monkeypatch):
    from auto_coder.github_request_governor import GitHubRequestGovernor

    api, run, _, clients = recovery
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3")
    monkeypatch.setattr("auto_coder.ci_cancellation_recovery.boundary_hooks", lambda: (governor.admit_blocking, governor.observe))
    try:
        assert run().status == "accepted"
        assert len(clients) == 2
        assert clients[0][0] == clients[1][0] == "repository-bound-credential"
        assert clients[1][1]["admission_hook"] == governor.admit
        assert clients[1][1]["observation_hook"] == governor.observe
    finally:
        governor.close()
