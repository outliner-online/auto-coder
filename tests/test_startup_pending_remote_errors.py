"""Controlled HTTP observations through the production startup lifecycle.

The ownership parser deliberately sees an ordinary local closing directive;
the cross-repository prose/parser regression belongs to PR #2864.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import httpx
import pytest
from click.testing import CliRunner

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import STARTUP_RECONCILIATION_EFFECT, STARTUP_RECONCILIATION_STAGE, AutomationEngine
from auto_coder.cli_commands_pending_work import pending_work_group
from auto_coder.github_pending_work import MAX_THROTTLED_RETRIES, PendingReason, WorkIdentity, get_pending_work_store
from auto_coder.github_request_governor import GitHubRequestGovernor
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.util.gh_cache import GitHubClient
from auto_coder.util.github_request_outcome import GitHubApiOutcome, GitHubRequestError, github_http_client
from tests.test_pending_work_resumption import _github_error


async def _until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), 5)


@pytest.mark.parametrize(
    ("response_kind", "retained_state"),
    [(kind, None) for kind in ("missing", "remote", "remote-errors-retry-after", "throttle", "secondary", "available", "invalid")]
    + [("available", state) for state in ("remote", "authentication", "forbidden", "indeterminate", "exhausted", "future", "due", "selected", "interrupted", "read-race")]
    + [("throttle", "last-retry"), ("invalid", "malformed-due"), ("invalid", "malformed-selected")],
)
def test_start_automation_preserves_provider_authority_and_selected_recovery(tmp_path, monkeypatch, response_kind, retained_state):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.db"))
    now = [time.time()]
    governor_path = tmp_path / "governor.db"
    governor = GitHubRequestGovernor(store_path=governor_path, wall_time=lambda: now[0], monotonic=lambda: now[0])
    observations = []
    requests = []
    warnings = []
    monkeypatch.setattr("auto_coder.automation_engine.logger.warning", lambda message, *args: warnings.append(message.format(*args)))
    state = {"corrected": response_kind in {"available", "invalid"}, "malformed": response_kind == "invalid"}

    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/issues/100"):
            if not state["corrected"]:
                status = {"missing": 404, "remote": 503, "remote-errors-retry-after": 503, "throttle": 429, "secondary": 403}[response_kind]
                headers = {"X-RateLimit-Remaining": "2901"}
                if response_kind in {"throttle", "secondary", "remote-errors-retry-after"}:
                    headers["Retry-After"] = "60"
                body = {"message": "raw-response-canary"}
                if response_kind == "remote-errors-retry-after":
                    body["errors"] = [{"message": "upstream unavailable"}]
                return httpx.Response(status, headers=headers, json=body)
            return httpx.Response(200, json={"number": 100, "state": "closed"})
        assert request.url.path.endswith(("/issues", "/pulls"))
        return httpx.Response(200, json={"bad": "raw-response-canary"} if state["malformed"] else [])

    def observe(outcome):
        observations.append(outcome)
        governor.observe(outcome)

    def request(method, url, **kwargs):
        with github_http_client(subsystem="strict-startup-test", transport=httpx.MockTransport(respond), admission_hook=governor.admit, observation_hook=observe) as client:
            response = client.request(method, url, **kwargs)
        outcome = observations[-1]
        if outcome.classification is not GitHubApiOutcome.SUCCESS:
            raise GitHubRequestError(outcome)
        return response

    monkeypatch.setattr("auto_coder.util.gh_cache._strict_request", request)

    class StartupGithub:
        token = "raw-credential-canary"

        def get_open_pull_requests(self, repo):
            return [{"number": 108, "body": "Closes #100", "head": {"ref": "work"}}]

        def get_issue_strict(self, repo, number):
            return request("GET", f"https://api.github.com/repos/{repo}/issues/{number}?token=raw-query-canary", headers={"Authorization": f"Bearer {self.token}"}).json()

        get_issue = get_issue_strict

        def get_issue_details(self, issue):
            return issue

        def get_connected_prs(self, repo, number, strict=False):
            assert strict
            return []

        def get_pull_request(self, repo, number):
            return {"number": number, "state": "open", "merged": False}

        def get_pr_details(self, pr):
            return pr

        get_open_entities_strict = GitHubClient.get_open_entities_strict

    engine = AutomationEngine(StartupGithub(), AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 100)
    assert slots.reserve(owner)
    engine.implementation_slots = slots
    store = get_pending_work_store("owner/repo")
    identity = WorkIdentity("owner/repo", "startup", STARTUP_RECONCILIATION_STAGE)
    other = WorkIdentity("owner/repo", "issue:999", "unrelated-stage")
    seeded = None
    if retained_state is not None:
        from auto_coder import github_pending_work
        from auto_coder.util.github_request_outcome import DeliveryCertainty

        classification = {
            "remote": GitHubApiOutcome.REMOTE_ERROR,
            "authentication": GitHubApiOutcome.AUTHENTICATION_FAILURE,
            "forbidden": GitHubApiOutcome.FORBIDDEN,
            "indeterminate": GitHubApiOutcome.TRANSPORT_FAILURE,
            "selected": GitHubApiOutcome.AUTHENTICATION_FAILURE,
            "malformed-selected": GitHubApiOutcome.AUTHENTICATION_FAILURE,
        }.get(retained_state, GitHubApiOutcome.SECONDARY_THROTTLED)
        error = _github_error(classification, delivery=DeliveryCertainty.INDETERMINATE if retained_state == "indeterminate" else DeliveryCertainty.HTTP_RESPONSE_RECEIVED, retry_after=600 if retained_state == "future" else 0)
        attempts = MAX_THROTTLED_RETRIES + 1 if retained_state == "exhausted" else MAX_THROTTLED_RETRIES if retained_state == "last-retry" else 1
        for _ in range(attempts):
            seeded = store.defer(identity, error, (STARTUP_RECONCILIATION_EFFECT,), now=now[0] if retained_state == "future" else now[0] - 10)
        if retained_state == "interrupted":
            store.mark_running(identity)
        store.defer(other, _github_error(GitHubApiOutcome.AUTHENTICATION_FAILURE), ("unrelated",))
        # Reopen from the durable destination, as a new controller does.
        github_pending_work._READY_STORES.clear()
        store = get_pending_work_store("owner/repo")
        assert store.get(identity).throttle_attempts == seeded.throttle_attempts
        if retained_state in {"selected", "malformed-selected"}:
            result = CliRunner().invoke(pending_work_group, ["retry", "--repository", "owner/repo", "--entity", "startup", "--stage", STARTUP_RECONCILIATION_STAGE])
            assert result.exit_code == 0, result.output

    read_entered = threading.Event()
    release_read = threading.Event()
    original_get = store.get
    if retained_state == "read-race":

        def get_after_recovery(identity_to_read):
            if identity_to_read == identity and not read_entered.is_set():
                read_entered.set()
                assert release_read.wait(5)
            return original_get(identity_to_read)

        monkeypatch.setattr(store, "get", get_after_recovery)
    entered = []

    async def steady(*args):
        entered.append(args)
        await engine._shutdown_event.wait()

    for name in ("_worker_loop", "_producer_loop", "_invalidation_loop", "_capacity_refill_loop", "_claude_followup_recovery_loop"):
        monkeypatch.setattr(engine, name, steady)
    monkeypatch.setattr("auto_coder.codex_pr_recovery.CodexPRRecoveryMonitor.run", lambda self, shutdown: shutdown.wait())
    monkeypatch.setattr("auto_coder.automation_engine.install_asyncio_diagnostics", lambda loop: None)
    monkeypatch.setattr("auto_coder.automation_engine.get_health_monitor", lambda: type("Monitor", (), {"start": lambda self: None})())

    async def scenario():
        unrelated = store.get(other)
        task = asyncio.create_task(engine.start_automation("owner/repo", concurrency=1))
        try:
            if retained_state is not None:
                if retained_state in {"malformed-due", "malformed-selected"}:
                    await _until(lambda: engine.pending_work_scheduler is not None)
                    scheduler = engine.pending_work_scheduler
                    now[0] = max(now[0], store.get(identity).not_before)
                    scheduler._clock = lambda: now[0]
                    await _until(lambda: requests.count("/repos/owner/repo/issues") == 1)
                    await _until(lambda: not scheduler._tasks and store.get(identity).status == "waiting")
                    retained = store.get(identity)
                    observed_requests = list(requests)
                    for _ in range(MAX_THROTTLED_RETRIES + 2):
                        now[0] += 3600
                        scheduler.wake()
                        await scheduler._dispatch_due()
                        await asyncio.gather(*tuple(scheduler._tasks))
                    assert requests == observed_requests
                    assert retained.reason is PendingReason.EVALUATION_FAILED and retained.not_before == 0
                    assert retained.throttle_attempts == (0 if retained_state == "malformed-selected" else seeded.throttle_attempts)
                    assert retained.unfinished_effects == (STARTUP_RECONCILIATION_EFFECT,)
                    assert store.get(identity) == retained
                    assert "invalid or incomplete observations" in retained.last_error and "pending-work retry" in retained.last_error
                    assert "raw-" not in retained.last_error
                    assert observations[-1].classification is GitHubApiOutcome.SUCCESS and observations[-1].status == 200
                    assert not task.done() and not engine.startup_reconciled and not engine.daemon_serving
                    assert not engine._startup_reconciliation_event.is_set()
                    assert not any(len(args) == 3 for args in entered)
                    assert slots.active_owners() == (owner,)
                    assert engine.get_status()["open_target_observation"]["availability"] == "unavailable"
                    state["malformed"] = False
                    result = await asyncio.to_thread(CliRunner().invoke, pending_work_group, ["retry", "--repository", "owner/repo", "--entity", "startup", "--stage", STARTUP_RECONCILIATION_STAGE])
                    assert result.exit_code == 0, result.output
                    assert store.get(other) == unrelated
                    scheduler.wake()
                elif retained_state == "read-race":
                    await _until(read_entered.is_set)
                    await _until(lambda: engine._startup_reconciliation_event.is_set() and original_get(identity) is None)
                    release_read.set()
                elif retained_state not in {"due", "selected", "interrupted"}:
                    await _until(lambda: bool(warnings) or engine.daemon_serving)
                    scheduler = engine.pending_work_scheduler
                    scheduler._clock = lambda: now[0]
                    if retained_state == "last-retry":
                        await _until(lambda: store.get(identity).reason is PendingReason.RETRIES_EXHAUSTED)
                        await asyncio.gather(*tuple(scheduler._tasks))
                        assert store.get(identity).throttle_attempts == MAX_THROTTLED_RETRIES + 1
                        assert len(requests) == 1
                    else:
                        assert requests == []
                        assert store.get(identity) == seeded
                    for _ in range(MAX_THROTTLED_RETRIES + 2):
                        if retained_state != "future":
                            now[0] += 3600
                        scheduler.wake()
                        await scheduler._dispatch_due()
                    assert len(requests) == (1 if retained_state == "last-retry" else 0)
                    assert not engine.startup_reconciled and not engine.daemon_serving and not task.done()
                    assert slots.active_owners() == (owner,) and slots.snapshot().owners[0].implementation_prs == ()
                    assert not any(len(args) == 3 for args in entered)
                    if retained_state == "future":
                        now[0] = seeded.not_before
                    else:
                        state["corrected"] = True
                        result = await asyncio.to_thread(CliRunner().invoke, pending_work_group, ["retry", "--repository", "owner/repo", "--entity", "startup", "--stage", STARTUP_RECONCILIATION_STAGE])
                        assert result.exit_code == 0, result.output
                    scheduler.wake()
            if response_kind == "invalid" and retained_state is None:
                with pytest.raises(RuntimeError, match="response was not a list"):
                    await asyncio.wait_for(task, 5)
                assert not engine.startup_reconciled and not engine.daemon_serving
                assert engine.get_status()["open_target_observation"]["availability"] == "unavailable"
                assert not any(len(args) == 3 for args in entered)
                assert store.get(identity) is None
                assert slots.active_owners() == (owner,)
                return
            if response_kind != "available" and retained_state is None:
                await _until(lambda: store.get(identity) is not None and bool(warnings))
                scheduler = engine.pending_work_scheduler
                scheduler._clock = lambda: now[0]
                retained = store.get(identity)
                assert retained.unfinished_effects == (STARTUP_RECONCILIATION_EFFECT,)
                assert not task.done() and not engine.startup_reconciled and not engine.daemon_serving
                assert engine.get_status()["open_target_observation"]["availability"] == "unavailable"
                assert not any(len(args) == 3 for args in entered)
                assert slots.active_owners() == (owner,)
                assert slots.snapshot().owners[0].implementation_prs == ()
                assert not slots.reserve(ImplementationOwner("pr", 108))
                if response_kind in {"missing", "remote", "remote-errors-retry-after"}:
                    assert retained.reason is PendingReason.EVALUATION_FAILED
                    assert retained.throttle_attempts == 0 and retained.not_before == 0
                    assert observations[-1].classification is GitHubApiOutcome.REMOTE_ERROR
                    assert observations[-1].metadata.rate_limit_remaining == 2901
                    with sqlite3.connect(governor_path) as db:
                        assert db.execute("SELECT episode_active, throttle_count FROM origin_state").fetchone() == (0, 0)
                    assert f"HTTP {404 if response_kind == 'missing' else 503}" in retained.last_error
                    assert "pending-work retry" in retained.last_error
                    assert "no automatic retry is scheduled" in warnings[-1]
                    assert "will retry automatically" not in warnings[-1]
                    assert "raw-" not in retained.last_error + warnings[-1]
                    listing = await asyncio.to_thread(CliRunner().invoke, pending_work_group, ["list", "--repository", "owner/repo"])
                    assert listing.exit_code == 0
                    assert retained.last_error in listing.output
                    assert "raw-" not in listing.output
                    assert scheduler.snapshot()[0]["blocked"] == "operational block: evaluation_failed"
                    for _ in range(MAX_THROTTLED_RETRIES + 2):
                        now[0] += 3600
                        scheduler.wake()
                        await scheduler._dispatch_due()
                    assert requests == ["/repos/owner/repo/issues/100"]
                    assert store.get(identity) == retained
                else:
                    assert retained.reason is PendingReason.THROTTLED
                    assert retained.throttle_attempts == 1 and retained.not_before > now[0]
                    assert "will retry automatically" in warnings[-1]
                    for attempt in range(2, MAX_THROTTLED_RETRIES + 2):
                        now[0] += 7200
                        await scheduler._dispatch_due()
                        await asyncio.gather(*tuple(scheduler._tasks))
                        assert store.get(identity).throttle_attempts == attempt
                    retained = store.get(identity)
                    assert retained.reason is PendingReason.RETRIES_EXHAUSTED
                    assert retained.not_before == 0
                    assert len(requests) == MAX_THROTTLED_RETRIES + 1
                    assert not engine.startup_reconciled and slots.active_owners() == (owner,)
                # The shipped command rearms exactly this obligation. It neither
                # clears an independent block nor bypasses fresh provider reads.
                store.defer(other, GitHubRequestError(observations[0]), ("unrelated",))
                unrelated = store.get(other)
                state["corrected"] = True
                now[0] += 7200
                result = await asyncio.to_thread(CliRunner().invoke, pending_work_group, ["retry", "--repository", "owner/repo", "--entity", "startup", "--stage", STARTUP_RECONCILIATION_STAGE])
                assert result.exit_code == 0, result.output
                assert store.get(other) == unrelated
                assert store.get(identity).unfinished_effects == retained.unfinished_effects
                scheduler.wake()
            await _until(lambda: engine.daemon_serving)
            await _until(lambda: any(len(args) == 3 for args in entered))
            assert engine.startup_reconciled and not task.done()
            assert engine.get_status()["open_target_observation"]["availability"] == "available"
            assert store.get(identity) is None
            assert slots.active_owners() == (owner,)
            assert slots.snapshot().owners[0].implementation_prs == (108,)
            assert requests.count("/repos/owner/repo/issues") == (2 if retained_state in {"malformed-due", "malformed-selected"} else 1)
            assert requests.count("/repos/owner/repo/pulls") == 1
            if response_kind != "available" or retained_state is not None:
                assert store.get(other) == unrelated
        finally:
            release_read.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            governor.close()

    asyncio.run(scenario())
