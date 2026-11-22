"""One explicitly adjudicated correction beside retained uncertain operations."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .accepted_finding_bridge import SOURCE_ALIAS_TYPE, source_identity_for
from .canonical_pr_blocker_ledger import BlockerDisposition, CanonicalPRBlockerLedger
from .cloud_provider_instructions import CloudTaskOperation, prepare_cloud_task
from .codex_cloud_client import _codex_followup_state_path, _followup_state_lock, _load_pending_followups
from .codex_work_accounting import CodexWorkPhase
from .codex_work_fence import CodexWorkAuthorityRefused, production_codex_fence
from .implementation_slots import ImplementationOwner
from .pr_no_commit_recovery import pr_correction_lock, review_repair_state_lock
from .pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
from .pr_review_cycle import OPEN, PrReviewCycleRepository
from .prompt_loader import render_prompt
from .review_adjudication_orchestrator import UpholdEffect


def snapshot_digest(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _read(path: Path) -> dict:
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict) or not isinstance(value.get("scoped_review_followups", {}), dict):
        raise ValueError("scoped correction document is unreadable")
    return value


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".json.{os.getpid()}.tmp")
    with temporary.open("w") as output:
        json.dump(document, output, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


def _registered_obligation(target: ExistingPrRepairTarget, effect: UpholdEffect) -> dict | None:
    """Select a single pre-existing normative obligation, never replacement prose."""
    scoped = effect.scoped_followup
    if scoped is None:
        return None
    requirements = [item for contract in effect.contracts if contract.issue_number == scoped.issue_number for item in contract.requirements if item.id == scoped.requirement_id]
    if len(requirements) != 1 or not _normalized(requirements[0].text):
        return None
    ledger = CanonicalPRBlockerLedger().get_snapshot("https://api.github.com", target.repo_name, target.pr_number, require_retained_state=True)
    blockers = ledger.get_blockers_for_alias("github_root_comment", str(effect.root_comment_id))
    if len(blockers) != 1:
        return None
    blocker = blockers[0]
    selected = (scoped.issue_number, scoped.requirement_id)
    if blocker.disposition is not BlockerDisposition.OPEN or blocker.reconciliation_needs or len(blocker.qualified_requirements) != 1 or (blocker.qualified_requirements[0].issue_number, blocker.qualified_requirements[0].requirement_id) != selected:
        return None
    if any(alias.alias_type == "reconciliation_split_scope" for alias in blocker.aliases):
        return None
    source_aliases = {alias.alias_value for alias in blocker.aliases if alias.alias_type == SOURCE_ALIAS_TYPE}
    if len(source_aliases) != 1:
        return None
    cycle = PrReviewCycleRepository(target.repo_name).snapshot(target.pr_number)
    accepted = [(record, finding) for record in cycle.strong_rounds for finding in cycle.findings if finding.origin_round_id == record.claim_id and finding.finding_id in record.finding_ids and source_identity_for(record.round_id, finding.finding_id) in source_aliases]
    if len(accepted) != 1:
        return None
    record, finding = accepted[0]
    source_identity = source_identity_for(record.round_id, finding.finding_id)
    owners = ledger.get_blockers_for_alias(SOURCE_ALIAS_TYPE, source_identity)
    if len(owners) != 1 or owners[0].blocker_id != blocker.blocker_id or record.open_epoch != cycle.open_epoch or record.head_sha != target.head_sha or record.base_sha != effect.base_sha or finding.status != OPEN or finding.is_regression_gap:
        return None
    if finding.requirement_ids != (f"#{scoped.issue_number}/{scoped.requirement_id}",) or len(finding.requirement_texts) != 1 or finding.requirement_texts != (requirements[0].text,):
        return None
    if not all(_normalized(text) for text in (finding.actual_behavior, finding.expected_behavior, finding.counterexample, finding.affected_boundary)):
        return None
    return {
        "issue_number": scoped.issue_number,
        "requirement_id": scoped.requirement_id,
        "requirement_text": requirements[0].text,
        "round_id": record.round_id,
        "source_identity": source_identity,
        "blocker_id": blocker.blocker_id,
        "contract_identity": record.contract_identity,
        "base_sha": record.base_sha,
        "boundary": finding.affected_boundary,
        "actual_behavior": finding.actual_behavior,
        "expected_behavior": finding.expected_behavior,
        "counterexample": finding.counterexample,
        "regression_scenario": finding.focused_regression_scenario,
    }


def _separate_obligation(old: dict, new: dict) -> bool:
    """Require distinct declared obligations; normalization only rejects duplicates."""
    required = set(new)
    if set(old) != required or any(old[key] != new[key] for key in ("round_id", "contract_identity", "base_sha")):
        return False
    if (old["issue_number"], old["requirement_id"]) == (new["issue_number"], new["requirement_id"]) or old["source_identity"] == new["source_identity"] or old["blocker_id"] == new["blocker_id"]:
        return False
    return all(_normalized(old[key]) != _normalized(new[key]) for key in ("requirement_text", "actual_behavior", "expected_behavior"))


def _settle_not_sent(path: Path, decision_id: str, snapshot: dict, identity: str) -> None:
    """Release only this invocation's exact pre-wire reservation under the shared guard."""
    with review_repair_state_lock(path):
        document = _read(path)
        record = document.get("scoped_review_followups", {}).get(decision_id)
        if record is None or record.get("snapshot") != snapshot or record.get("state") != "reserved":
            raise ValueError("exact pre-wire reservation no longer matches")
        if identity in document.get("delivered_feedback", []):
            raise ValueError("a confirmed claim cannot be released as not sent")
        record["state"] = "not_sent"
        record["reason"] = "source_refused_before_wire"
        document["pending_feedback"] = sorted(set(document.get("pending_feedback", [])) - {identity})
        _write(path, document)


def deliver_scoped_followup(
    path: Path,
    target: ExistingPrRepairTarget,
    effect: UpholdEffect,
    current_source: Callable[[], bool],
    send: Callable[[str, str, Callable[[], bool]], str],
) -> str:
    """Claim an exact accepted source once; old unknowns are never changed.

    Caller supplies an applicable UPHOLD from the existing authenticated reader
    and rechecks that source and verified owner immediately before transport.
    Legacy operations without these producer snapshots cannot be separated.
    """
    scoped = effect.scoped_followup
    if scoped is None or not effect.source_actor_id or not effect.source_comment_id or not effect.source_body_hash or target.head_sha != effect.head_sha:
        return "held_source"
    identity = f"{target.repo_name}#{target.pr_number}:codex-cloud:{scoped.task_id}:scoped:{effect.decision_id}"
    comment_identity = f"{target.repo_name}#{target.pr_number}:codex-cloud:{scoped.task_id}:" + hashlib.sha256(f"comment:{effect.source_comment_id}".encode()).hexdigest()
    source_aliases = {identity, comment_identity, effect.decision_id}
    try:
        obligation = _registered_obligation(target, effect)
    except Exception:
        return "held_registered_obligation"
    if obligation is None:
        return "held_registered_obligation"
    details = render_prompt("codex_cloud.scoped_registered_correction", include_header=False, **obligation)
    prompt = build_existing_pr_repair_prompt(target, details)
    prepared = prepare_cloud_task(prompt, recipient="codex-cloud", operation=CloudTaskOperation.CONTINUATION, no_edit=False).prepared_task
    snapshot = {
        "decision_id": effect.decision_id,
        "context_id": effect.context_id,
        "source_comment_id": effect.source_comment_id,
        "source_actor_id": effect.source_actor_id,
        "source_body_hash": effect.source_body_hash,
        "contract_digest": effect.contract_digest,
        "target": asdict(target),
        "task_id": scoped.task_id,
        "owner_incarnation": scoped.owner_incarnation,
        "identity": identity,
        "payload": prompt,
        "native_fingerprint": hashlib.sha256(prepared.encode()).hexdigest(),
        "distinct_from": list(map(list, scoped.distinct_from)),
        "obligation": obligation,
    }
    with pr_correction_lock(path, target.pr_number):
        if not current_source():
            return "held_source"
        fence, work = production_codex_fence(target.repo_name, scoped.task_id, "follow-up", identity, reconcile=False)
        if work.incarnation != scoped.owner_incarnation:
            return "held_owner"
        inventory = fence.accounting.snapshot(ImplementationOwner("issue", work.issue_number), work.incarnation)
        with review_repair_state_lock(path):
            document = _read(path)
            records = document.setdefault("scoped_review_followups", {})
            prior = records.get(effect.decision_id)
            if prior is not None:
                if prior.get("snapshot") != snapshot:
                    return "held_source_collision"
                return "already_accepted" if prior.get("state") == "accepted" else "held_previous_attempt"
            # Complete pre-send membership is required, not a new native key.
            delivered = set(document.get("delivered_feedback", []))
            pending = set(document.get("pending_feedback", [])) - delivered
            relevant = {key: value for key, value in records.items() if value["snapshot"]["target"]["pr_number"] == target.pr_number and value["state"] not in {"accepted", "not_sent"}}
            for record in records.values():
                old_snapshot = record["snapshot"]
                if old_snapshot["target"]["pr_number"] == target.pr_number and record["state"] != "not_sent" and not _separate_obligation(old_snapshot.get("obligation", {}), obligation):
                    return "held_obligation_overlap"
            refs = dict(scoped.distinct_from)
            if set(refs) != set(relevant):
                return "held_old_inventory"
            known = {record["snapshot"]["identity"] for record in relevant.values()}
            if any(item.startswith(f"{target.repo_name}#{target.pr_number}:") and item not in known for item in pending):
                return "held_legacy_claim"
            with _followup_state_lock:
                native_path = _codex_followup_state_path(target.repo_name)
                raw = json.loads(native_path.read_text()) if native_path.exists() else {}
                if not isinstance(raw, dict) or any(not isinstance(value, dict) for value in raw.values()):
                    return "held_native_inventory"
                native = _load_pending_followups(native_path)
            by_identity = {record.logical_identity: record for record in native.values() if record.task_id == scoped.task_id}
            if source_aliases.intersection(by_identity) or source_aliases.intersection(pending | delivered):
                return "held_previous_attempt"
            for old_id, record in relevant.items():
                old = record["snapshot"]
                native_old = by_identity.get(old["identity"])
                operations = [item for item in inventory.operations if item.kind == "follow-up" and item.task_id == scoped.task_id and item.source_request_id == old["identity"]]
                native_key = hashlib.sha256(f"{scoped.task_id}\0{old['identity']}".encode()).hexdigest()
                if (
                    snapshot_digest(old) != refs[old_id]
                    or old["payload"] == prompt
                    or old["target"] != asdict(target)
                    or old["source_actor_id"] != effect.source_actor_id
                    or old["task_id"] != scoped.task_id
                    or old["owner_incarnation"] != scoped.owner_incarnation
                    or native_old is None
                    or native.get(native_key) != native_old
                    or len(operations) != 1
                    or operations[0].phase not in {CodexWorkPhase.RESERVED, CodexWorkPhase.DELIVERY_UNKNOWN}
                    or operations[0].request_fingerprint != old["native_fingerprint"]
                    or operations[0].causal_baseline != native_old.pre_send_turn_id
                    or native_old.status != "indeterminate"
                    or native_old.repair_repository != target.repo_name
                    or native_old.repair_issue_number != work.issue_number
                    or native_old.repair_head_sha != target.head_sha
                    or native_old.repair_head_branch != target.head_branch
                    or native_old.repair_base_branch != target.base_branch
                    or native_old.message_fingerprint != old["native_fingerprint"]
                    or native_old.repair_owner_incarnation != scoped.owner_incarnation
                    or native_old.repair_pr_number != target.pr_number
                    or old["identity"] not in pending
                ):
                    return "held_old_snapshot"
            if any(record.status != "delivered" and record.logical_identity not in known for record in by_identity.values()):
                return "held_legacy_native"
            for operation in inventory.operations:
                if operation.task_id == scoped.task_id and operation.kind == "follow-up":
                    members = set(operation.source_request_id.split(","))
                    if source_aliases.intersection(members):
                        return "held_previous_attempt"
                    if operation.phase in {CodexWorkPhase.RESERVED, CodexWorkPhase.DELIVERY_UNKNOWN} and (not members or not members.issubset(known)):
                        return "held_legacy_operation"
            records[effect.decision_id] = {"snapshot": snapshot, "state": "reserved"}
            document["pending_feedback"] = sorted(pending | {identity})
            _write(path, document)
        try:
            ready = current_source() and _registered_obligation(target, effect) == obligation
        except Exception:
            ready = False
        if not ready:
            _settle_not_sent(path, effect.decision_id, snapshot, identity)
            return "held_source_not_sent"

        def final_authority_check() -> bool:
            return current_source() and _registered_obligation(target, effect) == obligation

        try:
            outcome = send(prompt, identity, final_authority_check)
        except CodexWorkAuthorityRefused:
            _settle_not_sent(path, effect.decision_id, snapshot, identity)
            return "held_source_not_sent"
        if outcome in {"accepted", "delivery_unknown"}:
            # Normalize the current operation's native evidence while it is
            # still this producer's new claim. A later source cannot enrich an
            # incomplete old operation merely to make separation possible.
            production_codex_fence(target.repo_name, scoped.task_id, "follow-up", identity)
        if outcome != "accepted":
            return outcome
        with review_repair_state_lock(path):
            document = _read(path)
            document["scoped_review_followups"][effect.decision_id]["state"] = "accepted"
            document["delivered_feedback"] = sorted(set(document.get("delivered_feedback", [])) | {identity})
            document["pending_feedback"] = sorted(set(document.get("pending_feedback", [])) - {identity})
            _write(path, document)
        return "accepted"
