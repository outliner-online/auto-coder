"""Real Strong acceptance -> authenticated canonical roots -> ordinary native send."""

import hashlib
import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_cloud_client import _codex_followup_state_path
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult
from auto_coder.codex_work_accounting import CodexWorkAccounting
from auto_coder.github_controller_auth import ControllerIdentity
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.pr_processor import _cloud_review_repair_state_path, _delegate_cloud_review_thread_repair
from auto_coder.pr_review_cycle import ContractSnapshot, FindingDisposition, PrReviewCycleRepository, RoundProvenance
from auto_coder.reviewer_session_registry import ReviewerSessionRegistry
from auto_coder.strong_correction_scope import FIELD, review_context
from auto_coder.util.gh_cache import PullRequestRepairMetadata, ReviewThread, ReviewThreadComment
from tests.test_accepted_finding_bridge import Env, accept_strong, finding_json, published_roots

REPO, PR, TASK = "owner/repo", 4001, "task_e_strong"
HEAD, NEXT, BASE = "a" * 40, "c" * 40, "b" * 40
COVERAGE = "Reject empty executable coverage while allowing empty type-only coverage."
SHARDS = "Preserve the failing shard exit status and failure log."
CONTRACT = ContractSnapshot(("#9",), f"Issue #9 REQ-001: {COVERAGE}\nIssue #9 REQ-002: {SHARDS}")


def attestation(requirement, context):
    return {
        "version": 1,
        "exhaustive": True,
        "independently_actionable": True,
        "requirements": [requirement],
        "older_operations": [{key: item[key] for key in ("operation_id", "snapshot_sha256", "native_membership")} for item in context["older_operations"]],
        "independence_evidence": "Complete coverage-oracle correction and independent failing-shard status/log correction are separately actionable under the explicit existing Requirements; neither reissues the other's correction.",
    }


def sources(slots):
    return slots._read()[ImplementationOwner("issue", 9).key][CodexWorkAccounting._FIELD][FIELD]


# Real owner-lock contention needs the production retry backoff.
@pytest.mark.usefixtures("_use_real_sleep")
@pytest.mark.parametrize(
    "control",
    [
        "fresh",
        "bound_draft",
        "alternate_first",
        "legacy_old",
        "edited_root",
        "stale_head",
        "native_lost",
        "crash_before_wire",
        "crash_after_wire",
        "wrong_revision",
        "duplicate",
        "whitespace_duplicate",
        "paraphrase",
        "unrelated_requirement",
        "incomplete_old",
        "forged_old",
        "contract_changed",
        "forged_actor",
        "birth_crash",
        "wildcard_first",
        "cross_lane_duplicate",
        "missing_run",
        "pending_effect",
        "exhausted",
        "edited_publication",
        "old_attempt_mismatch",
        "late_head",
        "remote_rejected",
        "accepted_bookkeeping",
        "ambiguous_bookkeeping",
        "old_native_key",
        "old_native_repository",
        "old_native_issue",
        "old_native_owner",
        "old_native_pr",
        "old_native_sha",
        "old_native_head",
        "old_native_base",
        "old_native_status",
        "old_native_accepted_status",
        "old_native_operation",
        "old_native_extra_null",
        "old_native_extra_list",
        "old_native_extra_string",
        "old_native_extra_object",
        "old_native_extra_reconcile",
        "old_native_extra_duplicate",
        "waited_root",
        "waited_revoked",
        "waited_reconciliation",
    ],
)
def test_real_strong_scope_birth_and_ordinary_mixed_dispatch(tmp_path, monkeypatch, control):
    monkeypatch.setenv("HOME", str(tmp_path))
    slots = ImplementationSlotRepository(REPO, 1)
    owner = ImplementationOwner("issue", 9)
    assert slots.reserve(owner)
    incarnation = slots.establish_incarnation(owner)
    CodexWorkAccounting(slots).initialize_fresh(owner, incarnation)
    assert slots.record_implementation_pr(owner, PR)
    CloudRunRepository(REPO).save(CloudRun(REPO, 9, 1, "codex-cloud", task_id=TASK, backend_name="codex-cloud", base_branch="main", launch_identity="launch-strong", publication_head_repository=REPO, publication_head_ref="work-branch", pull_request_numbers=[PR]))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    env = Env(tmp_path, worktree, BASE, HEAD, PrReviewCycleRepository(REPO), CanonicalPRBlockerLedger(), ReviewerSessionRegistry(tmp_path / "sessions.json"))
    coverage = finding_json("coverage", gap=False, requirement="#9/REQ-001", boundary="scripts/coverage-oracle.js:verify", scenario="Reject executable emptiness and accept legitimate type-only emptiness.")
    coverage.update(
        requirement_texts=[COVERAGE],
        actual_behavior="Executable and type-only maps both pass when empty.",
        expected_behavior="Reject empty executable maps and accept legitimate empty type-only maps.",
        counterexample="An executable source has an empty map.",
        correction_scope=attestation("#9/REQ-001", {"older_operations": []}),
    )
    state = {"head": HEAD, "threads": []}
    github = MagicMock()
    github.get_authenticated_controller_identity.return_value = ControllerIdentity("controller", account_id=20)

    def data():
        return {"number": PR, "state": "open", "body": "Fixes #9", "head": {"ref": "work-branch", "sha": state["head"], "repo": {"full_name": REPO}}, "base": {"ref": "main", "sha": BASE, "repo": {"id": 3}}, "labels": [], "user": {"login": "maintainer"}}

    github.get_pull_request_metadata_strict.side_effect = lambda *_args: data()
    github.get_pull_request_repair_metadata_strict.side_effect = lambda *_args: PullRequestRepairMetadata("work-branch", state["head"], "main")
    github.get_pr_review_threads_strict.side_effect = lambda *_args: state["threads"]
    github.get_pr_comments.return_value = []
    github.get_pr_comments_strict.return_value = []
    wham = MagicMock()
    wham.resolve_latest_assistant_turn.return_value = TASK + "~assttrn_1"
    wham.reconcile_follow_up.return_value = False
    wham.send_follow_up.side_effect = [FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, 502), FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)]
    with patch("tests.test_accepted_finding_bridge.CONTRACT", CONTRACT), patch("auto_coder.codex_cloud_client.CodexWhamClient", return_value=wham):
        if control == "birth_crash":
            write = env.cycle._write

            def fail_acceptance(state_doc):
                candidate = state_doc["prs"].get(str(PR), {})
                if candidate.get("findings", {}).get("coverage") and candidate.get("active_claim") is None:
                    raise OSError("Controlled accepted-result write failure after birth")
                write(state_doc)

            with patch.object(env.cycle, "_write", side_effect=fail_acceptance), pytest.raises(AssertionError, match="Controlled accepted-result write failure"):
                accept_strong(env, PR, [coverage])
            source, orphan = next(iter(sources(slots).items()))
            assert orphan["attempt"] is None and orphan["native_membership"] == []
            assert env.cycle.snapshot(PR).findings == ()
            from auto_coder.codex_cloud_client import CodexCloudClient

            assert not CodexCloudClient(repo_name=REPO).send_followup(TASK, "Forged uncommitted source request", ("forged-member",), strong_source=source, strong_revision=orphan["revision"], final_authority_check=lambda: True)
            wham.send_follow_up.assert_not_called()
            assert not _codex_followup_state_path(REPO).exists()
            return
        first_inputs = accept_strong(env, PR, [coverage])
        assert env.bridge().project(env.target(PR)).complete
        # Birth is real and durable before root publication; no native claim yet.
        first_source, first_row = next(iter(sources(slots).items()))
        assert first_row["attempt"] is None and first_row["native_membership"] == []
        env.cycle.acknowledge_publication(PR, env.cycle.snapshot(PR).accepted_strong_round.round_id)
        observation = published_roots(env, PR, monkeypatch, root_ids={"coverage": 100})
        assert env.bridge().project(env.target(PR), observation).complete
        old = ReviewThread(id="T100", comments=[ReviewThreadComment(100, observation.roots[0].body, "bot", 7, "Bot")])
        state["threads"] = [old]
        if control == "wildcard_first":
            from auto_coder.codex_cloud_client import CodexCloudClient
            from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt

            target = ExistingPrRepairTarget(REPO, PR, "work-branch", "main", HEAD)
            assert not CodexCloudClient(repo_name=REPO).send_followup(TASK, build_existing_pr_repair_prompt(target, "Existing authorized broad correction"), ("another-existing-lane",), repair_target=target)
            claimed = sources(slots)[first_source]["attempt"]
            assert claimed is not None and claimed["scoped"] is False
            with patch("auto_coder.pr_processor._two_tier_gate_inputs", return_value=first_inputs):
                _delegate_cloud_review_thread_repair(REPO, data(), github, (old,))
            wham.send_follow_up.assert_called_once()
            assert sources(slots)[first_source]["attempt"] == claimed
            return
        with patch("auto_coder.pr_processor._two_tier_gate_inputs", return_value=first_inputs):
            actions = _delegate_cloud_review_thread_repair(REPO, data(), github, (old,))
        wham.send_follow_up.assert_called_once()
        old_native = _codex_followup_state_path(REPO).read_bytes()
        old_scope = json.loads(json.dumps(sources(slots)[first_source]))
        assert old_scope["attempt"] is not None, actions
        if control == "old_attempt_mismatch":
            with slots._state_lock():
                records = slots._read()
                records[owner.key][CodexWorkAccounting._FIELD][FIELD][first_source]["attempt"]["operation_id"] = "another-operation"
                slots._write(records)
            assert "unavailable" in json.loads(review_context(REPO, PR))
            wham.send_follow_up.assert_called_once()
            return
        if control == "legacy_old":
            # An existing ordinary operation without exhaustive source evidence
            # is not adopted by the new attester, even though IDs are known.
            with slots._state_lock():
                records = slots._read()
                op = records[owner.key][CodexWorkAccounting._FIELD]["operations"][old_scope["attempt"]["operation_id"]]
                op["send_provenance"]["strong_source"] = None
                op["send_provenance"]["strong_revision"] = None
                slots._write(records)
            assert "unavailable" in json.loads(review_context(REPO, PR))
            return
        # Supported lifecycle progression: ordinary closure on a new head finds
        # expanded scope and schedules the next normal Strong audit. Delivery
        # uncertainty is deliberately unchanged by independent code verification.
        state["head"] = NEXT
        before = env.cycle.snapshot(PR)
        env.cycle.record_ordinary_pass(PR, RoundProvenance(NEXT, BASE), CONTRACT)
        env.cycle.certify_closure(
            PR,
            RoundProvenance(NEXT, BASE),
            CONTRACT,
            first_inputs.policy,
            before.accepted_strong_round.round_id,
            before.finding_set_revision,
            [FindingDisposition("coverage", "FIXED", "Independent coverage oracle verifies executable/type-only behavior at this head", NEXT)],
            False,
            "A failing-shard production path expanded the independently reviewed correction scope.",
        )
        assert env.cycle.snapshot(PR).phase == "STRONG_PENDING"
        context = json.loads(review_context(REPO, PR))
        assert len(context["older_operations"]) == 1
        failure = finding_json("failure", gap=False, requirement="#9/REQ-002", boundary="scripts/run-shards.sh:wait", scenario="Fail a child shard and verify nonzero status plus retained failure log.")
        failure.update(
            requirement_texts=[SHARDS], actual_behavior="The runner discards a child exit status and log.", expected_behavior="Return nonzero and preserve the child failure log.", counterexample="A child exits 3 while the runner exits 0.", correction_scope=attestation("#9/REQ-002", context)
        )
        if control in {"duplicate", "whitespace_duplicate", "paraphrase"}:
            failure = {**coverage, "finding_id": "duplicate", "correction_scope": attestation("#9/REQ-001", context)}
            if control == "whitespace_duplicate":
                failure["actual_behavior"] = "  " + failure["actual_behavior"] + "  "
            if control == "paraphrase":
                failure["actual_behavior"] = "Both empty executable and type declaration coverage are accepted."
                failure["expected_behavior"] = "Keep declaration-only emptiness valid but fail empty executable coverage."
                failure["counterexample"] = "An executable module produces zero coverage records."
            with pytest.raises(AssertionError, match="Shared registered Requirement remains overlapping"):
                accept_strong(env, PR, [failure], head=NEXT)
            wham.send_follow_up.assert_called_once()
            return
        if control == "unrelated_requirement":
            failure["requirement_texts"] = ["Publish unrelated repository settings"]
            with pytest.raises(AssertionError, match="not in the authoritative contract"):
                accept_strong(env, PR, [failure], head=NEXT)
            wham.send_follow_up.assert_called_once()
            return
        if control in {"incomplete_old", "forged_old"}:
            if control == "incomplete_old":
                failure["correction_scope"]["older_operations"] = []
            else:
                failure["correction_scope"]["older_operations"][0]["snapshot_sha256"] = "0" * 64
            with pytest.raises(AssertionError, match="exact registered finding/older operations"):
                accept_strong(env, PR, [failure], head=NEXT)
            wham.send_follow_up.assert_called_once()
            assert sources(slots)[first_source] == old_scope
            return
        second_inputs = accept_strong(env, PR, [failure], head=NEXT)
        assert env.bridge().project(env.target(PR, NEXT)).complete
        env.cycle.acknowledge_publication(PR, env.cycle.snapshot(PR).accepted_strong_round.round_id)
        observation2 = published_roots(env, PR, monkeypatch, root_ids={"failure": 110})
        if control == "edited_publication":
            from dataclasses import replace

            edited = replace(observation2, roots=tuple(replace(root, body=root.body + "\nChanged publication") for root in observation2.roots))
            assert not env.bridge().project(env.target(PR, NEXT), edited).complete
            assert all(not row["native_membership"] for key, row in sources(slots).items() if key != first_source)
            wham.send_follow_up.assert_called_once()
            return
        assert env.bridge().project(env.target(PR, NEXT), observation2).complete
        new = ReviewThread(id="T110", comments=[ReviewThreadComment(110, observation2.roots[0].body, "bot", 7, "Bot")])
        state["threads"] = [old, new]
        new_source = next(source for source in sources(slots) if source != first_source)
        if control == "edited_root":
            new.comments[0].body += "\nEdited remote source"
        if control == "stale_head":
            state["head"] = "d" * 40
        if control == "native_lost":
            _codex_followup_state_path(REPO).write_text("{}")
        if control == "contract_changed":
            from dataclasses import replace

            second_inputs = replace(second_inputs, contract=ContractSnapshot(("#9",), "Issue #9 REQ-002: Changed requirement"))
        if control == "forged_actor":
            with env.cycle._locked():
                state_doc = env.cycle._read()
                state_doc["prs"][str(PR)]["strong_rounds"][-1]["reviewer_provenance"] = "forged/unaccepted/actor"
                env.cycle._write(state_doc)
        if control == "wrong_revision":
            with slots._state_lock():
                records = slots._read()
                records[owner.key][CodexWorkAccounting._FIELD][FIELD][new_source]["revision"] = "0" * 64
                slots._write(records)
        if control == "missing_run":
            from auto_coder.codex_cloud_client import CodexCloudClient

            path = CloudRunRepository(REPO).storage_path
            original_run = path.read_bytes()
            path.write_text("{}")
            try:
                assert not CodexCloudClient(repo_name=REPO).send_followup(TASK, "Existing broad route with missing tracking", ("untracked-fallback",), force=True)
                wham.send_follow_up.assert_called_once()
            finally:
                path.write_bytes(original_run)
        if control == "alternate_first":
            from auto_coder.codex_cloud_client import CodexCloudClient
            from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt

            target = ExistingPrRepairTarget(REPO, PR, "work-branch", "main", NEXT)
            assert not CodexCloudClient(repo_name=REPO).continue_if_paused(TASK, prompt=build_existing_pr_repair_prompt(target, "Unbounded alternate correction"), repair_target=target, force=True)
            wham.send_follow_up.assert_called_once()
        if control.startswith("old_native_"):
            path = _codex_followup_state_path(REPO)
            native = json.loads(path.read_text())
            key = hashlib.sha256(f"{TASK}\0{old_scope['native_membership'][0]}".encode()).hexdigest()
            if control == "old_native_key":
                native["e" * 64] = native.pop(key)
            elif control.startswith("old_native_extra_"):
                native["f" * 64] = {"old_native_extra_null": None, "old_native_extra_list": [], "old_native_extra_string": "unknown identity", "old_native_extra_object": {}, "old_native_extra_reconcile": None, "old_native_extra_duplicate": None}[control]
                if control == "old_native_extra_duplicate":
                    native.pop("f" * 64)
                if control == "old_native_extra_reconcile":
                    wham.reconcile_follow_up.return_value = True
                    wham.reconcile_follow_up.reset_mock()
            elif control in {"old_native_accepted_status", "old_native_operation"}:
                with slots._state_lock():
                    records = slots._read()
                    operation = records[owner.key][CodexWorkAccounting._FIELD]["operations"][old_scope["attempt"]["operation_id"]]
                    if control == "old_native_accepted_status":
                        operation["send_provenance"]["outcome"] = "accepted"
                    else:
                        operation["source_request_id"] = "another-request"
                    slots._write(records)
            else:
                field, value = {
                    "old_native_repository": ("repair_repository", "other/repository"),
                    "old_native_issue": ("repair_issue_number", 10),
                    "old_native_owner": ("repair_owner_incarnation", "other-incarnation"),
                    "old_native_pr": ("repair_pr_number", PR + 1),
                    "old_native_sha": ("repair_head_sha", "e" * 40),
                    "old_native_head": ("repair_head_branch", "other-branch"),
                    "old_native_base": ("repair_base_branch", "other-base"),
                    "old_native_status": ("status", "not_delivered"),
                }[control]
                native[key][field] = value
            encoded = json.dumps(native)
            if control == "old_native_extra_duplicate":
                # A normal dict reader would hide the malformed duplicate
                # behind the final valid original row at the canonical key.
                encoded = encoded[:-1] + f', "{key}": null, "{key}": ' + json.dumps(native[key]) + "}"
            path.write_text(encoded)
            old_native = path.read_bytes()  # Preserve even the deficient old evidence verbatim.
        if control == "remote_rejected":
            wham.send_follow_up.side_effect = [FollowUpDeliveryResult(FollowUpDeliveryOutcome.NOT_DELIVERED, 429)]
        if control in {"accepted_bookkeeping", "ambiguous_bookkeeping"}:
            from auto_coder.codex_work_fence import CodexWorkFence

            if control == "ambiguous_bookkeeping":
                wham.send_follow_up.side_effect = [FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, 502)]

            def fail_bookkeeping(*_args, **_kwargs):
                raise RuntimeError("Controlled lifecycle bookkeeping failure after native certainty")

            monkeypatch.setattr(CodexWorkFence, "record_delivery", fail_bookkeeping)
        if control == "crash_after_wire":
            wham.send_follow_up.side_effect = RuntimeError("Controlled crash after entering wire")
        if control == "late_head":
            from auto_coder.codex_work_fence import CodexWorkFence

            execute = CodexWorkFence.execute

            def change_after_outer(fence, identity, send, *args, **kwargs):
                assert kwargs.get("final_authority_check") is not None
                state["head"] = "d" * 40
                return execute(fence, identity, send, *args, **kwargs)

            monkeypatch.setattr(CodexWorkFence, "execute", change_after_outer)
        if control == "pending_effect":
            monkeypatch.setattr("auto_coder.pr_processor._get_adjudication_effect_store", lambda: MagicMock(force_revalidation_needed=lambda *_args: True))
        if control == "exhausted":
            monkeypatch.setattr("auto_coder.pr_processor.check_pr_repair_exhaustion", lambda *_args: MagicMock(is_exhausted=True))
        from auto_coder.pr_processor import DraftPRRepairAuthority, _resolve_cloud_task_origin, draft_pr_repair_authority_scope

        authority = None
        if control == "bound_draft":
            origin = _resolve_cloud_task_origin(REPO, data(), github).origin
            assert origin is not None and origin.task_id == TASK
            authority = DraftPRRepairAuthority(REPO, PR, "work-branch", NEXT, (9,), origin.provider, origin.task_id, origin.attribution_token)
        with patch("auto_coder.pr_processor._two_tier_gate_inputs", return_value=second_inputs), draft_pr_repair_authority_scope(authority):
            if control == "crash_before_wire":
                with patch("auto_coder.codex_cloud_client._save_pending_followups", side_effect=OSError("Controlled failure after durable operation claim")):
                    result = _delegate_cloud_review_thread_repair(REPO, data(), github, (old, new))
            elif control.startswith("waited_"):

                def change_remote_authority():
                    if control == "waited_root":
                        new.comments[0].body += "\nRemote edit during owner-lock wait"
                        return
                    ledger = env.ledger.get_snapshot("https://api.github.com", REPO, PR)
                    blocker = sources(slots)[new_source]["canonical_blocker"]
                    if control == "waited_revoked":
                        from auto_coder.canonical_pr_blocker_ledger import BlockerDisposition

                        env.ledger.record_transition("https://api.github.com", REPO, PR, "controlled-revocation", ledger.ledger_revision, blocker, BlockerDisposition.AUTHORIZED_INVALIDATION, "Controlled accepted invalidation during wait")
                    else:
                        env.ledger.record_requirement_manifest_change("https://api.github.com", REPO, PR, "controlled-reconciliation", ledger.ledger_revision, blocker, "new-manifest", "Scope must be reconciled")

                result = _after_owner_wait(slots, lambda: _delegate_cloud_review_thread_repair(REPO, data(), github, (old, new)), change_remote_authority)
                assert sources(slots)[new_source]["attempt"] is None
            else:
                result = _delegate_cloud_review_thread_repair(REPO, data(), github, (old, new))
            _delegate_cloud_review_thread_repair(REPO, data(), github, (old, new))
            _delegate_cloud_review_thread_repair(REPO, data(), github, (old, new))
        expected = 2 if control in {"fresh", "bound_draft", "alternate_first", "cross_lane_duplicate", "missing_run", "crash_after_wire", "remote_rejected", "accepted_bookkeeping", "ambiguous_bookkeeping"} else 1
        assert wham.send_follow_up.call_count == expected, result
        assert sources(slots)[first_source] == old_scope
        if control.startswith("old_native_extra_"):
            assert _codex_followup_state_path(REPO).read_bytes() == old_native
            assert sources(slots)[new_source]["attempt"] is None
            if control == "old_native_extra_reconcile":
                wham.reconcile_follow_up.assert_not_called()
        if control != "native_lost":
            current = json.loads(_codex_followup_state_path(REPO).read_text())
            assert all(current[key] == value for key, value in json.loads(old_native).items())
        if control in {"fresh", "bound_draft", "alternate_first", "cross_lane_duplicate", "missing_run"}:
            assert result.delivered
            prompt = wham.send_follow_up.call_args.kwargs["prompt"]
            assert SHARDS in prompt and COVERAGE not in prompt
            assert sources(slots)[new_source]["attempt"] is not None
        if control == "cross_lane_duplicate":
            from auto_coder.codex_cloud_client import CodexCloudClient
            from auto_coder.strong_correction_scope import instruction

            receipt = sources(slots)[new_source]
            assert not CodexCloudClient(repo_name=REPO).send_followup(TASK, instruction(REPO, new_source, receipt), ("different-lane-id",), strong_source=new_source, strong_revision=receipt["sealed_revision"], final_authority_check=lambda: True, force=True)
            assert wham.send_follow_up.call_count == 2
        if control in {"remote_rejected", "accepted_bookkeeping", "ambiguous_bookkeeping"}:
            attempt = sources(slots)[new_source]["attempt"]
            assert attempt is not None
            operation = slots._read()[owner.key][CodexWorkAccounting._FIELD]["operations"][attempt["operation_id"]]
            expected_outcome = {"remote_rejected": "not_delivered", "accepted_bookkeeping": "accepted", "ambiguous_bookkeeping": "unknown"}[control]
            assert operation["send_provenance"]["outcome"] == expected_outcome
        state_doc = json.loads(_cloud_review_repair_state_path(REPO).read_text())
        if control == "remote_rejected":
            assert not set(sources(slots)[new_source]["native_membership"]) & set(state_doc["pending_feedback"])
        assert set(old_scope["native_membership"]).issubset(state_doc["pending_feedback"])


def _after_owner_wait(slots, dispatch, change):
    """Change only remote/canonical authority after the real native lock competes."""
    import auto_coder.runtime_locks as locks
    from auto_coder.codex_work_fence import CodexWorkFence
    from tests.test_scoped_final_authority import _hold_owner

    at_boundary, waiting = threading.Event(), threading.Event()
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    holder = context.Process(target=_hold_owner, args=(str(slots.storage_path), acquired, release))
    execute, flock = CodexWorkFence.execute, locks.fcntl.flock

    def gate(fence, identity, send, *args, **kwargs):
        assert kwargs.get("final_authority_check") is not None
        at_boundary.set()
        assert acquired.wait(10)
        return execute(fence, identity, send, *args, **kwargs)

    def observe(fd, operation):
        try:
            return flock(fd, operation)
        except BlockingIOError:
            if acquired.is_set():
                waiting.set()
            raise

    with patch.object(CodexWorkFence, "execute", gate), patch.object(locks.fcntl, "flock", observe), ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(dispatch)
        try:
            assert at_boundary.wait(10)
            holder.start()
            assert acquired.wait(10)
            assert waiting.wait(10), "Actual owner-lock contention was not observed"
            change()
            release.set()
            return future.result(timeout=10)
        finally:
            release.set()
            if holder.pid is not None:
                holder.join(10)
                assert holder.exitcode == 0
