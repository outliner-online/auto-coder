"""Authenticated native adjudication -> durable source -> real Codex sender."""

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_work_accounting import CodexWorkAccounting
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.pr_processor import _adjudication_context_store_path, _apply_review_adjudication_effects, _cloud_review_repair_state_path
from auto_coder.review_adjudication import AdjudicationLedger, Decision, ScopedFollowup, parse_decision, render_decision
from auto_coder.review_adjudication_github import AdjudicationContextStore, IssueEvidence, PullRequestBinding, build_issue_contracts, new_context
from auto_coder.scoped_review_followup import snapshot_digest
from auto_coder.util.gh_cache import PullRequestRepairMetadata, ReviewThread, ReviewThreadComment

REPO = "owner/repo"
PR = 4001
HEAD = "a" * 40
TASK = "task_e_scoped"


COVERAGE_REQUIREMENT = "Reject incomplete executable-file coverage while permitting empty type-only coverage."
FAILURE_REQUIREMENT = "Return a failing status and preserve the log when a unit-test shard fails."
CONTRACT_BODY = f"## Objective\nPreserve trustworthy shard validation.\n## Requirements\nREQ-001: {COVERAGE_REQUIREMENT}\nREQ-002: {FAILURE_REQUIREMENT}\nREQ-003: Preserve the dashboard read-only API."


def _registered_findings(tmp_path, monkeypatch, control):
    from auto_coder.canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
    from auto_coder.pr_review_cycle import ContractSnapshot, PrReviewCycleRepository
    from auto_coder.reviewer_session_registry import ReviewerSessionRegistry
    from tests.test_accepted_finding_bridge import Env, accept_strong, finding_json, published_roots

    body = CONTRACT_BODY
    if control == "whitespace_only":
        body = body.replace(FAILURE_REQUIREMENT, "  " + "   ".join(COVERAGE_REQUIREMENT.split()) + "  ")
    monkeypatch.setattr(__import__(__name__, fromlist=["CONTRACT_BODY"]), "CONTRACT_BODY", body)
    contracts = build_issue_contracts([IssueEvidence(90, 9, "Contract", body)])
    requirement_texts = {item.id: item.text for item in contracts[0].requirements}
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    env = Env(tmp_path, worktree, "b" * 40, HEAD, PrReviewCycleRepository(REPO), CanonicalPRBlockerLedger(), ReviewerSessionRegistry(tmp_path / "sessions.json"))
    coverage = finding_json("coverage", gap=False, boundary="scripts/ci/coverage-oracle.js:verify_source", requirement="#9/REQ-001", scenario="Reject an empty executable map and accept legitimate type-only emptiness.")
    coverage.update(
        requirement_texts=[requirement_texts["REQ-001"]],
        actual_behavior="An executable file with no covered statements is accepted.",
        expected_behavior="Reject missing executable coverage while allowing empty type-only maps.",
        counterexample="A known executable source and a type-only source both produce empty maps.",
    )
    failure = finding_json("failure", gap=False, boundary="scripts/ci/run-unit-tests.sh:wait_for_shards", requirement="#9/REQ-002", scenario="Fail one shard and verify the runner status and retained shard log.")
    failure.update(requirement_texts=[requirement_texts["REQ-002"]], actual_behavior="The runner discards a child shard failure status.", expected_behavior="Return a nonzero status and retain the failing shard log.", counterexample="A child shard exits with status 3 but the runner exits 0.")
    if control == "duplicate_registered_obligation":
        failure["expected_behavior"] = "  " + "   ".join(coverage["expected_behavior"].split()) + "  "
    with patch("tests.test_accepted_finding_bridge.CONTRACT", ContractSnapshot(("#9",), body)):
        inputs = accept_strong(env, PR, [coverage, failure])
    before = env.bridge().project(env.target(PR))
    assert before.complete
    strong = env.cycle.snapshot(PR).accepted_strong_round
    inputs.gate.state.acknowledge_publication(PR, strong.round_id)
    observation = published_roots(env, PR, monkeypatch, root_ids={"coverage": 100, "failure": 110})
    associated = env.bridge().project(env.target(PR), observation)
    assert associated.complete
    return [ReviewThread(id=f"T{root.comment_id}", comments=[ReviewThreadComment(root.comment_id, root.body, "bot", 7, "Bot", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")]) for root in observation.roots]


def _input(threads, incarnation, requirement_id, refs=(), actor=8, decision_id=None, scoped=True):
    root_id = 100 if requirement_id == "REQ-001" else 110
    thread = next(item for item in threads if item.comments[0].database_id == root_id)
    context = new_context(
        PullRequestBinding(3, REPO, PR, HEAD, "b" * 40, "main"),
        thread,
        build_issue_contracts([IssueEvidence(90, 9, "Contract", CONTRACT_BODY)]),
    )
    ledger = AdjudicationContextStore(_adjudication_context_store_path()).register(context, "root-source")
    context = ledger.context
    value = Decision(
        decision_id or str(uuid.uuid4()),
        context.context_id,
        HEAD,
        context.contract_digest,
        "UPHOLD",
        "FIX",
        ledger.tips(),
        "Only the registered requirement's correction is authorized.",
        "chatgpt-assisted",
        ScopedFollowup(TASK, incarnation, 9, requirement_id, refs) if scoped else None,
    )
    body = render_decision(value)
    assert parse_decision(body) == value
    thread.comments.append(ReviewThreadComment(root_id + len(thread.comments), body, "human", actor, "User", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", root_id))
    return value


@pytest.mark.parametrize(
    "control",
    [
        "fresh",
        "missing_snapshot",
        "missing_native",
        "wrong_digest",
        "unauthorized",
        "owner_changed",
        "head_changed",
        "unknown_new",
        "legacy_claim",
        "same_payload",
        "different_principal",
        "pending_effect",
        "edited_source",
        "reused_id",
        "closed",
        "lost_new_native",
        "remote_rejected",
        "legacy_same_source",
        "legacy_v1",
        "native_duplicate",
        "incomplete_accounting",
        "unrelated",
        "whitespace_only",
        "prewire_refusal",
        "prewire_exception",
        "crash_after_wire",
        "duplicate_registered_obligation",
    ],
)
def test_native_scoped_source_is_claimed_once_without_replaying_unknown_old(tmp_path, monkeypatch, control):
    from dataclasses import asdict

    from auto_coder.codex_cloud_client import CodexCloudClient, PendingCodexFollowUp, _codex_followup_state_path
    from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult

    monkeypatch.setenv("HOME", str(tmp_path))
    slots = ImplementationSlotRepository(REPO, 1)
    owner = ImplementationOwner("issue", 9)
    assert slots.reserve(owner)
    incarnation = slots.establish_incarnation(owner)
    CodexWorkAccounting(slots).initialize_fresh(owner, incarnation)
    assert slots.record_implementation_pr(owner, PR)
    CloudRunRepository(REPO).save(CloudRun(REPO, 9, 1, "codex-cloud", task_id=TASK, backend_name="codex-cloud", launch_identity="launch-9", publication_head_repository=REPO, publication_head_ref="work-branch", pull_request_numbers=[PR]))
    data = {"number": PR, "body": "Fixes #9", "head": {"ref": "work-branch", "sha": HEAD, "repo": {"full_name": REPO}}, "base": {"ref": "main", "sha": "b" * 40, "repo": {"id": 3}}, "labels": []}
    github = MagicMock()
    threads = _registered_findings(tmp_path, monkeypatch, control)
    github.get_pr_review_threads_strict.side_effect = lambda *_args: threads
    github.get_pull_request_metadata_strict.return_value = {**data, "state": "open"}
    github.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata("work-branch", HEAD, "main")
    wham = MagicMock()
    wham.resolve_latest_assistant_turn.return_value = TASK + "~assttrn_1"
    wham.reconcile_follow_up.return_value = False
    wham.send_follow_up.side_effect = [
        FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, 502),
        FollowUpDeliveryResult(
            FollowUpDeliveryOutcome.INDETERMINATE if control in {"unknown_new", "lost_new_native"} else FollowUpDeliveryOutcome.NOT_DELIVERED if control == "remote_rejected" else FollowUpDeliveryOutcome.DELIVERED,
            502 if control in {"unknown_new", "lost_new_native"} else 400 if control == "remote_rejected" else 200,
        ),
    ]
    if control == "crash_after_wire":
        wham.send_follow_up.side_effect = [FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, 502), RuntimeError("lost process outcome after entering the wire call")]
    stages = []
    native_send = CodexCloudClient.send_followup

    def controlled_native(client, task_id, message, logical_identities=(), *args, **kwargs):
        if control == "native_duplicate" and "#9/REQ-002" in message:
            import hashlib

            native_path = _codex_followup_state_path(REPO)
            records = json.loads(native_path.read_text())
            key = hashlib.sha256(f"{task_id}\0{logical_identities[0]}".encode()).hexdigest()
            records[key] = asdict(PendingCodexFollowUp(task_id=task_id, logical_identity=logical_identities[0], pre_send_turn_id="concurrent-turn"))
            native_path.write_text(json.dumps(records))
        return native_send(client, task_id, message, logical_identities, *args, **kwargs)

    with (
        patch.object(CodexCloudClient, "send_followup", autospec=True, side_effect=controlled_native),
        patch("auto_coder.pr_processor._record_pr_stage", side_effect=lambda *args: stages.append(args)),
        patch("auto_coder.pr_processor.get_pr_review_allowlist_from_config", return_value=[7]),
        patch("auto_coder.pr_processor.get_review_adjudicator_allowlist_from_config", return_value=[8, 9]),
        patch("auto_coder.codex_cloud_client.CodexWhamClient", return_value=wham),
    ):
        first = _input(threads, incarnation, "REQ-001", scoped=control != "legacy_v1")
        first_actions, _ = _apply_review_adjudication_effects(REPO, PR, data, github)
        if control == "legacy_v1":
            from auto_coder.pr_processor import _get_adjudication_effect_store

            effects = _get_adjudication_effect_store()
            original_effect = effects.get(first.context_id)
            assert original_effect is not None and original_effect.status == "pending"
            original_native = _codex_followup_state_path(REPO).read_bytes()
            _input(threads, incarnation, "REQ-002")
            for _ in range(3):
                _apply_review_adjudication_effects(REPO, PR, data, github)
            wham.send_follow_up.assert_called_once()
            assert effects.get(first.context_id) == original_effect
            assert _codex_followup_state_path(REPO).read_bytes() == original_native
            return
        assert any("delivery_unknown" in value for value in first_actions), first_actions
        path = _cloud_review_repair_state_path(REPO)
        doc = json.loads(path.read_text())
        old = doc["scoped_review_followups"][first.decision_id]
        old_native = json.loads(_codex_followup_state_path(REPO).read_text())
        from auto_coder.pr_processor import _record_review_feedback_state

        _record_review_feedback_state(path, set(), set())
        assert json.loads(path.read_text())["scoped_review_followups"][first.decision_id] == old
        digest = snapshot_digest(old["snapshot"])
        old_operations = tuple(item for item in CodexWorkAccounting(slots).snapshot(owner, incarnation).operations if item.source_request_id == old["snapshot"]["identity"])
        assert len(old_operations) == 1
        if control == "incomplete_accounting":
            with slots._state_lock():
                records = slots._read()
                records[owner.key][CodexWorkAccounting._FIELD]["operations"][old_operations[0].logical_operation_id]["request_fingerprint"] = None
                slots._write(records)
            old_operations = tuple(item for item in CodexWorkAccounting(slots).snapshot(owner, incarnation).operations if item.source_request_id == old["snapshot"]["identity"])
        if control == "missing_snapshot":
            doc.pop("scoped_review_followups")
            path.write_text(json.dumps(doc))
        elif control == "missing_native":
            _codex_followup_state_path(REPO).write_text("{}")
        elif control == "legacy_claim":
            doc["pending_feedback"].append(f"{REPO}#{PR}:codex-cloud:{TASK}:legacy")
            path.write_text(json.dumps(doc))
        second = _input(
            threads,
            incarnation + "changed" if control == "owner_changed" else incarnation,
            "REQ-001" if control == "same_payload" else "REQ-003" if control == "unrelated" else "REQ-002",
            ((first.decision_id, "0" * 64 if control == "wrong_digest" else digest),),
            actor=99 if control == "unauthorized" else 9 if control == "different_principal" else 8,
            decision_id=first.decision_id if control == "reused_id" else None,
        )
        if control == "head_changed":
            github.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata("work-branch", "c" * 40, "main")
        if control == "legacy_same_source":
            import hashlib

            state = json.loads(path.read_text())
            generic = f"{REPO}#{PR}:codex-cloud:{TASK}:" + hashlib.sha256(f"comment:{next(item for item in threads if item.comments[-1].database_id == (101 if control == "same_payload" else 111)).comments[-1].database_id}".encode()).hexdigest()
            state["delivered_feedback"] = [generic]
            path.write_text(json.dumps(state))
        if control == "pending_effect":
            from auto_coder.pr_processor import _get_adjudication_effect_store

            effects = _get_adjudication_effect_store()
            effects.begin("legacy", REPO, PR, "legacy-decision", HEAD, first.contract_digest, "UPHOLD")
            effects.finish("legacy", "unknown")
        elif control == "edited_source":
            next(item for item in threads if item.comments[-1].database_id == (101 if control == "same_payload" else 111)).comments[-1].body += "edited"
        elif control == "closed":
            github.get_pull_request_metadata_strict.return_value = {**data, "state": "closed"}
        refused = None
        if control in {"prewire_refusal", "prewire_exception"}:
            refused = second
            refusal = {"fired": False}

            def metadata_at_wire_boundary(*_args):
                document = json.loads(path.read_text())
                row = document.get("scoped_review_followups", {}).get(refused.decision_id)
                if row is not None and row["state"] == "reserved" and not refusal["fired"]:
                    refusal["fired"] = True
                    _record_review_feedback_state(path, set(), {f"{REPO}#9999:peer-pending"})
                    if control == "prewire_exception":
                        raise OSError("authoritative source read failed before wire")
                    return {**data, "state": "closed"}
                return {**data, "state": "open"}

            github.get_pull_request_metadata_strict.side_effect = metadata_at_wire_boundary
        actions, _ = _apply_review_adjudication_effects(REPO, PR, data, github)
        if refused is not None:
            assert refusal["fired"]
            wham.send_follow_up.assert_called_once()  # Only the earlier unknown source.
            released = json.loads(path.read_text())
            refused_row = released["scoped_review_followups"][refused.decision_id]
            assert refused_row["state"] == "not_sent"
            assert refused_row["snapshot"]["identity"] not in released["pending_feedback"]
            assert old["snapshot"]["identity"] in released["pending_feedback"]
            assert f"{REPO}#9999:peer-pending" in released["pending_feedback"]
            _apply_review_adjudication_effects(REPO, PR, data, github)
            wham.send_follow_up.assert_called_once()
            second = _input(threads, incarnation, "REQ-002", ((first.decision_id, digest),))
            actions, _ = _apply_review_adjudication_effects(REPO, PR, data, github)
            assert json.loads(path.read_text())["scoped_review_followups"][refused.decision_id] == refused_row
        if control == "lost_new_native":
            current = json.loads(_codex_followup_state_path(REPO).read_text())
            _codex_followup_state_path(REPO).write_text(json.dumps({key: value for key, value in current.items() if key in old_native}))
        # Reconstruct every reader/client through the real entry on each pass.
        _apply_review_adjudication_effects(REPO, PR, data, github)
        _apply_review_adjudication_effects(REPO, PR, data, github)
    # Until exhaustive ordinary scope has a real producer, even the v2 source
    # cannot bypass an older wildcard operation at the shared owner boundary.
    expected = 1
    assert wham.send_follow_up.call_count == expected, actions
    retained = json.loads(path.read_text())
    if control != "missing_snapshot":
        assert retained["scoped_review_followups"][first.decision_id] == old
    if control != "missing_native":
        current_native = json.loads(_codex_followup_state_path(REPO).read_text())
        assert all(current_native[key] == value for key, value in old_native.items())
    assert old["snapshot"]["identity"] in retained["pending_feedback"]
    assert old["snapshot"]["identity"] not in retained.get("delivered_feedback", [])
    scoped_stages = [args for args in stages if args[1] == "pr.review-adjudication-effect" and args[4].get("effect_kind") == "SCOPED_FIX"]
    assert scoped_stages
    from auto_coder.execution_trace import Outcome

    assert all(args[3] is Outcome.DEFERRED for args in scoped_stages if args[4]["status"].startswith("held_") or args[4]["status"] == "already_accepted")
    if control in {"fresh", "unknown_new", "lost_new_native", "remote_rejected", "prewire_refusal", "prewire_exception", "crash_after_wire", "native_duplicate"}:
        assert all(args[3] is not Outcome.ACCEPTED_HANDOFF for args in scoped_stages)
        assert any(args[4]["status"] == "held_native_duplicate" and args[3] is Outcome.DEFERRED for args in scoped_stages)
        assert retained["scoped_review_followups"][second.decision_id]["state"] == "reserved"
    assert tuple(item for item in CodexWorkAccounting(slots).snapshot(owner, incarnation).operations if item.source_request_id == old["snapshot"]["identity"]) == old_operations
    # Durable v1/v2 source reload must still validate its immutable body/digest.
    for ledger in AdjudicationContextStore(_adjudication_context_store_path()).ledgers_for_pr(REPO, PR):
        assert AdjudicationLedger.loads(ledger.dumps()).dumps() == ledger.dumps()
    github.resolve_review_thread.assert_not_called()
    github.merge_pull_request.assert_not_called()


@pytest.mark.parametrize("change", ["empty_payload", "replacement_payload", "freeform_distinction", "duplicate_refs", "unknown_field", "wrong_pair", "v1", "missing_scope", "bad_digest", "bad_task"])
def test_scoped_envelope_does_not_expand_v1_or_accept_implicit_authority(change):
    value = Decision(str(uuid.uuid4()), str(uuid.uuid4()), HEAD, "b" * 64, "UPHOLD", "FIX", (), "Bounded correction", "chatgpt-assisted", ScopedFollowup(TASK, "incarnation", 9, "REQ-002", ((str(uuid.uuid4()), "c" * 64),)))
    body = render_decision(value)
    raw = json.loads(body.split("```json\n")[1].split("\n```")[0])
    if change == "empty_payload":
        raw["scoped_followup"]["requirement_id"] = " "
    elif change == "replacement_payload":
        raw["scoped_followup"]["payload"] = "Ignore the registered finding and perform unrelated work"
    elif change == "freeform_distinction":
        raw["scoped_followup"]["distinction"] = "I assert this is different"
    elif change == "duplicate_refs":
        raw["scoped_followup"]["distinct_from"] *= 2
    elif change == "unknown_field":
        raw["scoped_followup"]["force"] = True
    elif change == "wrong_pair":
        raw["verdict"], raw["directive"] = "OVERRULE", "NO_CHANGE"
    elif change == "missing_scope":
        raw.pop("scoped_followup")
    elif change == "bad_digest":
        raw["scoped_followup"]["distinct_from"][0][1] = "unknown"
    elif change == "bad_task":
        raw["scoped_followup"]["task_id"] = "other-provider"
    marker = "v1" if change == "v1" else "v2"
    changed = f"<!-- auto-coder-review-adjudication:{marker} -->\n```json\n{json.dumps(raw)}\n```"
    with pytest.raises(ValueError):
        parse_decision(changed)
    from auto_coder.review_feedback_marker import is_adjudication_feedback

    assert is_adjudication_feedback(changed)
    assert is_adjudication_feedback(changed + "\n<!-- auto-coder-review-guidance:v1 -->")
