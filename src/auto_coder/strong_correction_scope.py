"""Accepted Strong scope authority and independent, owner-serialized birth claims.

A reviewer attests semantics. Only this producer's durable birth, authenticated
root binding and common native reservation can establish an unattempted source.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager, nullcontext
from typing import Iterator

from .cloud_run import CloudRunRepository
from .codex_work_accounting import CodexSendProvenance, CodexWorkAccounting, CodexWorkPhase, WorkAccountingStatus
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository

FIELD = "strong_correction_sources"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_attestation(value: object) -> str:
    fields = {"version", "exhaustive", "independently_actionable", "requirements", "older_operations", "independence_evidence"}
    if not isinstance(value, dict) or set(value) != fields or type(value["version"]) is not int or value["version"] != 1 or value["exhaustive"] is not True or value["independently_actionable"] is not True:
        raise ValueError("Strong scope requires explicit versioned exhaustive independent authority")
    requirements = value["requirements"]
    if not isinstance(requirements, list) or len(requirements) != 1 or not isinstance(requirements[0], str) or not requirements[0].startswith("#"):
        raise ValueError("Initial Strong scope supports one complete qualified requirement finding")
    if not isinstance(value["independence_evidence"], str) or not value["independence_evidence"].strip() or not isinstance(value["older_operations"], list):
        raise ValueError("Strong scope requires explicit independence evidence and complete older inventory")
    seen = set()
    for old in value["older_operations"]:
        if not isinstance(old, dict) or set(old) != {"operation_id", "snapshot_sha256", "native_membership"} or not isinstance(old["operation_id"], str) or not old["operation_id"] or old["operation_id"] in seen:
            raise ValueError("Invalid or duplicate frozen older operation")
        if not isinstance(old["snapshot_sha256"], str) or len(old["snapshot_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in old["snapshot_sha256"]):
            raise ValueError("Invalid older scope digest")
        members = old["native_membership"]
        if not isinstance(members, list) or not members or any(not isinstance(member, str) or not member for member in members) or members != sorted(set(members)):
            raise ValueError("Older native membership must be exact and exhaustive")
        seen.add(old["operation_id"])
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def finding_payload(finding) -> dict:
    return {key: getattr(finding, key) for key in ("finding_id", "origin_round_id", "requirement_ids", "requirement_texts", "counterexample", "expected_behavior", "actual_behavior", "evidence", "affected_boundary", "focused_regression_scenario", "is_regression_gap", "correction_scope")}


def owner_binding(repository: str, pr_number: int):
    runs = [run for run in CloudRunRepository(repository).list_all() if run.provider == "codex-cloud" and pr_number in run.pull_request_numbers and run.task_id and run.launch_identity]
    if len(runs) != 1:
        raise ValueError("Strong scope requires one existing Codex PR owner")
    run = runs[0]
    slots = ImplementationSlotRepository(repository, 1)
    owner = ImplementationOwner("issue", run.issue_number)
    incarnation = slots.owner_incarnation(owner)
    if not incarnation:
        raise ValueError("Strong scope has no current owner incarnation")
    return slots, owner, incarnation, run


@contextmanager
def acceptance_lock(repository: str, pr_number: int, findings) -> Iterator[None]:
    # Lock order is owner -> review-cycle transition/store, matching the final
    # native authority reader. Never wait for owner while holding review state.
    lock = owner_binding(repository, pr_number)[0:3] if any(item.correction_scope for item in findings or ()) else None
    with lock[0].serialize(lock[1]) if lock else nullcontext():
        yield


def _rows(slots, owner, incarnation):
    records = slots._read()
    record = CodexWorkAccounting._active_record(records, owner, incarnation)
    accounting = record.get(CodexWorkAccounting._FIELD)
    if not isinstance(accounting, dict) or not isinstance(accounting.get("operations"), dict):
        raise ValueError("Strong scope requires existing complete owner accounting")
    sources = accounting.setdefault(FIELD, {})
    if not isinstance(sources, dict):
        raise ValueError("Malformed Strong birth inventory")
    return records, record, accounting, sources


def seal_revision(row: dict) -> str:
    return digest({"source_revision": row["revision"], "native_membership": row["native_membership"], "canonical_blocker": row["canonical_blocker"], "root_bodies": row.get("root_bodies", {})})


def strict_native_evidence(repository: str) -> dict:
    """Read every raw row; scoped exhaustiveness cannot use legacy filtering."""
    from .codex_cloud_client import PendingCodexFollowUp, _codex_followup_state_path
    from .codex_wham_client import FollowUpDeliveryOutcome

    path = _codex_followup_state_path(repository)
    if not path.exists():
        return {}

    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate native journal fields prevent exhaustive Strong scope")
        return result

    raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if not isinstance(raw, dict) or any(not isinstance(key, str) or not isinstance(row, dict) for key, row in raw.items()):
        raise ValueError("Unidentifiable native journal entry prevents exhaustive Strong scope")
    for key, row in raw.items():
        if any(not isinstance(row.get(field), str) or not row[field] for field in ("task_id", "logical_identity", "message_fingerprint", "pre_send_turn_id", "status")):
            raise ValueError("Incomplete native journal row prevents exhaustive Strong scope")
        if key != hashlib.sha256(f"{row['task_id']}\0{row['logical_identity']}".encode("utf-8")).hexdigest() or len(row["message_fingerprint"]) != 64 or any(c not in "0123456789abcdef" for c in row["message_fingerprint"]):
            raise ValueError("Malformed native journal identity prevents exhaustive Strong scope")
        if row["status"] not in {FollowUpDeliveryOutcome.INDETERMINATE.value, FollowUpDeliveryOutcome.DELIVERED.value}:
            raise ValueError("Unsupported native journal status prevents exhaustive Strong scope")
    try:
        return {key: PendingCodexFollowUp(**row) for key, row in raw.items()}
    except TypeError as exc:
        raise ValueError("Malformed native journal record prevents exhaustive Strong scope") from exc


def _frozen(operation, sources) -> dict:
    provenance = operation.send_provenance
    if provenance is None or not provenance.strong_source or provenance.strong_source not in sources:
        raise ValueError("Older wildcard has no exhaustive accepted Strong scope")
    source = sources[provenance.strong_source]
    if digest(source["snapshot"]) != source["revision"]:
        raise ValueError("Older scope revision is corrupt")
    if not isinstance(source, dict) or not source.get("native_membership") or tuple(source["native_membership"]) != provenance.logical_identities or source.get("sealed_revision") != provenance.strong_revision or source.get("sealed_revision") != seal_revision(source):
        raise ValueError("Older operation has deficient source/native binding")
    if (source["snapshot"]["task_id"], source["snapshot"]["pr_number"], source["snapshot"]["head_sha"]) != (operation.task_id, provenance.pr_number, provenance.head_sha):
        raise ValueError("Older source and native target/task disagree")
    attempt = source.get("attempt")
    if not isinstance(attempt, dict) or attempt.get("operation_id") != operation.logical_operation_id or attempt.get("scoped") is not True or tuple(attempt.get("native_membership", ())) != provenance.logical_identities:
        raise ValueError("Older operation has no exact atomic source attempt binding")
    from .codex_wham_client import FollowUpDeliveryOutcome
    from .codex_work_fence import CodexWorkFence, CodexWorkIdentity, stable_codex_operation_id

    if operation.kind != "follow-up" or not operation.causal_baseline or operation.source_request_id != ",".join(provenance.logical_identities) or operation.logical_operation_id != stable_codex_operation_id(operation.kind, operation.source_request_id):
        raise ValueError("Older operation identity does not match its exact native request")
    snapshot = source["snapshot"]
    slots, owner, incarnation, run = owner_binding(snapshot["repository"], snapshot["pr_number"])
    if (owner.number, incarnation, run.task_id, run.launch_identity, run.publication_head_repository, run.publication_head_ref, run.base_branch or "main") != (
        snapshot["owner_issue"],
        snapshot["owner_incarnation"],
        operation.task_id,
        snapshot["launch_identity"],
        snapshot["head_repository"],
        snapshot["head_ref"],
        snapshot["base_branch"],
    ) or (provenance.head_branch, provenance.base_branch) != (snapshot["head_ref"], snapshot["base_branch"]):
        raise ValueError("Older immutable repair target/owner binding changed")
    identity = CodexWorkIdentity(snapshot["repository"], owner.number, incarnation, operation.logical_operation_id, operation.kind, operation.source_request_id, operation.task_id, operation.causal_baseline)
    subject = CodexWorkFence(CodexWorkAccounting(slots))._subject(identity, provenance)
    # Native rows do not carry a head repository: the existing authenticated
    # publication attribution must positively bind it to this frozen launch.
    if subject is None or not subject.head_repository or (subject.head_repository, subject.head_ref) != (snapshot["head_repository"], snapshot["head_ref"]):
        raise ValueError("Older writable head has no exact authenticated binding")
    native = strict_native_evidence(snapshot["repository"])
    group = [(key, item) for key, item in native.items() if item.task_id == operation.task_id and item.message_fingerprint == provenance.fingerprint and item.pre_send_turn_id == operation.causal_baseline]
    if sorted(item.logical_identity for _key, item in group) != list(provenance.logical_identities):
        raise ValueError("Older native membership is absent or incomplete")
    expected_target = (snapshot["repository"], snapshot["owner_issue"], snapshot["owner_incarnation"], snapshot["pr_number"], snapshot["head_sha"], snapshot["head_ref"], snapshot["base_branch"])
    for key, item in group:
        target = (item.repair_repository, item.repair_issue_number, item.repair_owner_incarnation, item.repair_pr_number, item.repair_head_sha, item.repair_head_branch, item.repair_base_branch)
        if key != hashlib.sha256(f"{operation.task_id}\0{item.logical_identity}".encode("utf-8")).hexdigest() or target != expected_target:
            raise ValueError("Older native lookup key/immutable repair binding disagrees")
        if item.status not in {FollowUpDeliveryOutcome.INDETERMINATE.value, FollowUpDeliveryOutcome.DELIVERED.value} or (provenance.outcome == "accepted" and item.status != FollowUpDeliveryOutcome.DELIVERED.value):
            raise ValueError("Older native status contradicts durable send evidence")
    return {
        "operation_id": operation.logical_operation_id,
        "source": source["snapshot"],
        "source_revision": source["revision"],
        "sealed_revision": source["sealed_revision"],
        "native_membership": list(provenance.logical_identities),
        "fingerprint": provenance.fingerprint,
        "task_id": operation.task_id,
        "baseline": operation.causal_baseline,
    }


def separate_source_subject(slots, owner, incarnation, task_id, provenance, row) -> bool:
    from .codex_work_fence import CodexWorkFence, CodexWorkIdentity

    snapshot = row["snapshot"]
    if provenance is None or provenance.pr_number is None or not snapshot.get("head_ref"):
        return False
    fence = CodexWorkFence(CodexWorkAccounting(slots))
    current = CodexWorkIdentity(slots.repo_name, owner.number, incarnation, "subject-observation", "follow-up", "subject-observation", task_id)
    previous = CodexWorkIdentity(slots.repo_name, owner.number, incarnation, "source-observation", "follow-up", "source-observation", snapshot["task_id"])
    source_target = CodexSendProvenance("0" * 64, ("source-observation",), snapshot["pr_number"], snapshot["head_sha"], snapshot["head_ref"], snapshot["base_branch"])
    current_subject, old_subject = fence._subject(current, provenance), fence._subject(previous, source_target)
    return current_subject is not None and old_subject is not None and current_subject.separate_from(old_subject)


def older_inventory(slots, owner, incarnation, pr_number: int, sources, *, current_source_key=None) -> list[dict]:
    from .codex_work_reconstruction import production_codex_reconstructor

    strict_native_evidence(slots.repo_name)  # Before a legacy reader can omit unknown raw rows.
    reconstructor = production_codex_reconstructor(slots.repo_name, owner.number)
    accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
    inventory = accounting.reconcile_from_receipt(owner, incarnation, reconstructor.reconstruct())
    if inventory.accounting_status is not WorkAccountingStatus.COMPLETE:
        raise ValueError("Strong scope requires a complete reconciled native inventory")
    result = []
    _slots, _owner, _incarnation, run = owner_binding(slots.repo_name, pr_number)
    target = CodexSendProvenance("0" * 64, ("subject-observation",), pr_number, "0" * 40, run.publication_head_ref, run.base_branch or "main")
    for source_key, source_row in sources.items():
        if source_key == current_source_key:
            continue
        if source_row["attempt"] is None and not separate_source_subject(slots, owner, incarnation, run.task_id, target, source_row):
            raise ValueError("An earlier Strong birth has no completed native claim; scope remains held")
    for operation in inventory.operations:
        provenance = operation.send_provenance
        ambiguous = provenance.outcome in {"reserved", "unknown"} if provenance else operation.phase in {CodexWorkPhase.RESERVED, CodexWorkPhase.DELIVERY_UNKNOWN}
        if not ambiguous and (operation.kind not in {"follow-up", "repair", "publication"} or operation.settled or provenance and provenance.outcome == "not_delivered"):
            continue
        # A known different PR/branch is still governed by the existing native
        # subject fence. Unknown/unbounded scope cannot be omitted here.
        if provenance and provenance.pr_number is not None and provenance.pr_number != pr_number:
            from .codex_work_fence import CodexWorkFence, CodexWorkIdentity

            _slots, _owner, _incarnation, run = owner_binding(slots.repo_name, pr_number)
            fence = CodexWorkFence(accounting)
            current_identity = CodexWorkIdentity(slots.repo_name, owner.number, incarnation, "scope-observation", "follow-up", "scope-observation", run.task_id)
            observed = CodexSendProvenance("0" * 64, ("scope-observation",), pr_number, "0" * 40, run.publication_head_ref, run.base_branch or "main")
            current_subject = fence._subject(current_identity, observed)
            old_identity = CodexWorkIdentity(slots.repo_name, owner.number, incarnation, operation.logical_operation_id, operation.kind, operation.source_request_id, operation.task_id, operation.causal_baseline)
            old_subject = fence._subject(old_identity, provenance)
            if current_subject is not None and old_subject is not None and current_subject.separate_from(old_subject):
                continue
        frozen = _frozen(operation, sources)
        if (frozen["source"]["repository"], frozen["source"]["owner_issue"], frozen["source"]["owner_incarnation"]) != (slots.repo_name, owner.number, incarnation):
            raise ValueError("Older scope belongs to another owner incarnation")
        result.append({"operation_id": operation.logical_operation_id, "snapshot_sha256": digest(frozen), "native_membership": frozen["native_membership"], "scope": frozen})
    return sorted(result, key=lambda item: item["operation_id"])


def review_context(repository: str, pr_number: int) -> str:
    try:
        slots, owner, incarnation, _run = owner_binding(repository, pr_number)
        with slots.serialize(owner), slots._state_lock():
            _records, _record, _accounting, sources = _rows(slots, owner, incarnation)
            older = older_inventory(slots, owner, incarnation, pr_number, sources)
        return json.dumps({"version": 1, "older_operations": older}, sort_keys=True)
    except Exception as exc:
        return json.dumps({"version": 1, "unavailable": type(exc).__name__})


def register_births(repository: str, pr_number: int, round_record, findings, acceptance_revision: int) -> None:
    opted = [finding for finding in findings if finding.correction_scope]
    if not opted:
        return
    if len(opted) != 1:
        raise ValueError("Initial Strong scope accepts one complete scoped finding per result")
    slots, owner, incarnation, run = owner_binding(repository, pr_number)
    with slots.serialize(owner), slots._state_lock():
        _records, _record, _accounting, sources = _rows(slots, owner, incarnation)
        older = older_inventory(slots, owner, incarnation, pr_number, sources)
        records, record, _accounting, sources = _rows(slots, owner, incarnation)
        expected = [{key: item[key] for key in ("operation_id", "snapshot_sha256", "native_membership")} for item in older]
        proposals = {}
        for finding in opted:
            attestation = json.loads(parse_attestation(json.loads(finding.correction_scope)))
            if finding.is_regression_gap or list(finding.requirement_ids) != attestation["requirements"] or len(finding.requirement_texts) != 1 or attestation["older_operations"] != expected:
                raise ValueError("Strong attestation does not cover the exact registered finding/older operations")
            # Explicit existing Requirement evidence, not Objective prose or a
            # guessed identifier. The accepted reviewer remains semantic authority.
            requirement = finding.requirement_ids[0].split("/", 1)[-1]
            lines = [line.strip() for line in round_record.contract_snapshot.requirements_text.splitlines()]
            issue = finding.requirement_ids[0].split("/", 1)[0]
            declaration = f"{requirement}: {finding.requirement_texts[0]}"
            if issue not in round_record.contract_snapshot.issue_ids or not ({declaration, f"Issue {issue} {declaration}"} & set(lines)):
                raise ValueError("Strong scope requirement is not in the authoritative contract")
            # The first protocol version has no trusted sub-Requirement
            # partition. Shared Requirement scope remains overlapping even if
            # the finding is paraphrased or explicitly claims independence.
            if any(set(finding.requirement_ids) & set(old["scope"]["source"]["finding"]["requirement_ids"]) for old in older):
                raise ValueError("Shared registered Requirement remains overlapping")
            source = f"{round_record.round_id}:{finding.finding_id}"
            snapshot = {
                "version": 1,
                "repository": repository,
                "pr_number": pr_number,
                "head_sha": round_record.head_sha,
                "base_sha": round_record.base_sha,
                "contract_identity": round_record.contract_identity,
                "open_epoch": round_record.open_epoch,
                "round_id": round_record.round_id,
                "finding_set_revision": round_record.finding_set_revision,
                "policy_identity": round_record.policy_identity,
                "acceptance_revision": acceptance_revision,
                "reviewer_provenance": round_record.reviewer_provenance,
                "owner_issue": owner.number,
                "owner_incarnation": incarnation,
                "task_id": run.task_id,
                "head_repository": run.publication_head_repository,
                "head_ref": run.publication_head_ref,
                "base_branch": run.base_branch or "main",
                "launch_identity": run.launch_identity,
                "finding": finding_payload(finding),
                "attestation": attestation,
            }
            revision = digest(snapshot)
            proposal: dict[str, object] = {"snapshot": snapshot, "revision": revision, "native_membership": [], "canonical_blocker": "", "attempt": None}
            if source in sources:
                if sources[source]["snapshot"] != snapshot or sources[source]["revision"] != revision:
                    raise ValueError("Strong source birth collision")
            else:
                proposals[source] = proposal
        sources.update(proposals)
        if proposals:
            CodexWorkAccounting._advance_revision(record)
            slots._write(records)  # Before the accepted result is made visible.


def seal_roots(repository: str, pr_number: int, cycle, ledger_snapshot, observation) -> None:
    if not any(finding.correction_scope for finding in cycle.findings):
        return
    slots, owner, incarnation, run = owner_binding(repository, pr_number)
    with slots.serialize(owner), slots._state_lock():
        records, record, _accounting, sources = _rows(slots, owner, incarnation)
        changed = False
        for source, row in sources.items():
            if row["snapshot"]["pr_number"] != pr_number:
                continue
            finding = next((item for item in cycle.findings if source.endswith(":" + item.finding_id) and item.origin_round_id == row["snapshot"]["finding"]["origin_round_id"]), None)
            if finding is None or digest(finding_payload(finding)) != digest(row["snapshot"]["finding"]):
                raise ValueError("Accepted source changed or birth is not committed")
            blockers = ledger_snapshot.get_blockers_for_alias("strong_audit_finding", source)
            if len(blockers) != 1:
                continue
            blocker = blockers[0]
            roots = sorted(blocker.get_root_comment_ids())
            if not roots:
                continue
            observed = {root.comment_id: root.body for root in observation.roots if root.authenticated} if observation is not None and observation.complete else {}
            if not row["native_membership"] and any(root not in observed for root in roots):
                continue
            prefix = f"{repository}#{pr_number}:codex-cloud:{run.task_id}:"
            members = sorted(prefix + hashlib.sha256(f"comment:{root}".encode()).hexdigest() for root in roots)
            if row["native_membership"] and (row["native_membership"] != members or row["canonical_blocker"] != blocker.blocker_id):
                raise ValueError("Canonical/native membership changed after sealing")
            if not row["native_membership"]:
                from .pr_processor import _render_two_tier_finding
                from .pr_review_effects import AcceptedReviewPayload

                accepted = next(item for item in cycle.strong_rounds if item.round_id == row["snapshot"]["round_id"])
                payload = AcceptedReviewPayload.strong(repository, pr_number, accepted, cycle.findings)
                expected_body = f"<!-- auto-coder-two-tier-finding:v1:{payload.identity}:{hashlib.sha256(finding.finding_id.encode()).hexdigest()} -->\n" + _render_two_tier_finding(finding)
                if any(observed[root] != expected_body for root in roots):
                    raise ValueError("Authenticated root body differs from the exact accepted publication")
                row["native_membership"], row["canonical_blocker"] = members, blocker.blocker_id
                row["root_bodies"] = {str(root): digest(observed[root]) for root in roots}
                row["sealed_revision"] = seal_revision(row)
                changed = True
        if changed:
            CodexWorkAccounting._advance_revision(record)
            slots._write(records)


def source_for_members(repository: str, pr_number: int, head_sha: str, task_id: str, members: set[str]):
    slots, owner, incarnation, run = owner_binding(repository, pr_number)
    if run.task_id != task_id:
        return None
    with slots.serialize(owner), slots._state_lock():
        _records, _record, _accounting, sources = _rows(slots, owner, incarnation)
        eligible = [
            (key, row)
            for key, row in sources.items()
            if row["snapshot"]["pr_number"] == pr_number and row["snapshot"]["head_sha"] == head_sha and row["snapshot"]["owner_incarnation"] == incarnation and row["native_membership"] and set(row["native_membership"]).issubset(members) and row["attempt"] is None
        ]
        return eligible[0] if len(eligible) == 1 else None


def validate_send(accounting, identity, provenance, *, old_operation=None) -> None:
    if provenance is None or not provenance.strong_source:
        raise ValueError("No accepted Strong source on this native request")
    slots = accounting.slots
    owner = ImplementationOwner("issue", identity.issue_number)
    with slots._state_lock():
        _records, _record, _accounting, sources = _rows(slots, owner, identity.incarnation)
        row = sources.get(provenance.strong_source)
        if row is None or row.get("sealed_revision") != provenance.strong_revision or row.get("sealed_revision") != seal_revision(row) or row["attempt"] is not None or tuple(row["native_membership"]) != provenance.logical_identities:
            raise ValueError("Strong request has no exact unattempted birth/native receipt")
        snapshot = row["snapshot"]
        if (snapshot["repository"], snapshot["owner_issue"], snapshot["owner_incarnation"], snapshot["task_id"], snapshot["pr_number"], snapshot["head_sha"]) != (identity.repository, identity.issue_number, identity.incarnation, identity.task_id, provenance.pr_number, provenance.head_sha):
            raise ValueError("Strong source owner/target revision changed")
        if digest(snapshot) != row["revision"]:
            raise ValueError("Strong source snapshot digest is stale")
        if old_operation is not None:
            if old_operation.send_provenance is None or old_operation.send_provenance.pr_number != provenance.pr_number:
                raise ValueError("A Strong partition cannot waive shared-head protection across different PRs")
            frozen = _frozen(old_operation, sources)
            if (frozen["source"]["repository"], frozen["source"]["owner_issue"], frozen["source"]["owner_incarnation"]) != (identity.repository, identity.issue_number, identity.incarnation):
                raise ValueError("Older scope belongs to another owner incarnation")
            ref = {"operation_id": old_operation.logical_operation_id, "snapshot_sha256": digest(frozen), "native_membership": frozen["native_membership"]}
            if ref not in snapshot["attestation"]["older_operations"]:
                raise ValueError("Strong authority does not cover this exact older operation")


def claim_sources(accounting, slots, owner, incarnation, operation_id, provenance, task_id) -> None:
    """Called inside register's owner+state transaction; every lane participates."""
    sources = accounting.get(FIELD, {})
    if provenance and provenance.strong_source:
        selected = sources.get(provenance.strong_source)
        if selected is None or selected["attempt"] is not None:
            raise ValueError("Accepted Strong source already attempted or has no producer birth")
    for source, row in sources.items():
        if row["attempt"] is not None:
            continue
        # Exact known different PRs retain existing subject protection. Missing
        # scope is deliberately a wildcard and consumes every possible source.
        if separate_source_subject(slots, owner, incarnation, task_id, provenance, row):
            continue
        if provenance and provenance.strong_source:
            if source != provenance.strong_source:
                continue
            if row.get("sealed_revision") != provenance.strong_revision or row.get("sealed_revision") != seal_revision(row) or tuple(row["native_membership"]) != provenance.logical_identities:
                raise ValueError("Strong source claim collision")
        row["attempt"] = {"operation_id": operation_id, "native_membership": list(provenance.logical_identities) if provenance else [], "scoped": bool(provenance and provenance.strong_source)}


def current_source(repository: str, source: str, row: dict) -> bool:
    from .canonical_pr_blocker_ledger import BlockerDisposition, CanonicalPRBlockerLedger
    from .pr_review_cycle import OPEN, PrReviewCycleRepository

    snapshot = row["snapshot"]
    if row.get("sealed_revision") != seal_revision(row):
        return False
    cycle = PrReviewCycleRepository(repository).snapshot(snapshot["pr_number"])
    finding = next((item for item in cycle.findings if item.finding_id == snapshot["finding"]["finding_id"] and item.origin_round_id == snapshot["finding"]["origin_round_id"]), None)
    rounds = [
        item
        for item in cycle.strong_rounds
        if source == f"{item.round_id}:{snapshot['finding']['finding_id']}"
        and item.reviewer_provenance == snapshot["reviewer_provenance"]
        and item.contract_identity == snapshot["contract_identity"]
        and item.head_sha == snapshot["head_sha"]
        and item.policy_identity == snapshot["policy_identity"]
        and item.finding_set_revision == snapshot["finding_set_revision"]
    ]
    if finding is None or finding.status != OPEN or len(rounds) != 1 or cycle.transition_version < snapshot["acceptance_revision"] or cycle.open_epoch != snapshot["open_epoch"] or digest(finding_payload(finding)) != digest(snapshot["finding"]) or digest(snapshot) != row["revision"]:
        return False
    ledger = CanonicalPRBlockerLedger().get_snapshot("https://api.github.com", repository, snapshot["pr_number"])
    blockers = ledger.get_blockers_for_alias("strong_audit_finding", source)
    if len(blockers) != 1 or blockers[0].blocker_id != row["canonical_blocker"]:
        return False
    blocker = blockers[0]
    if blocker.disposition not in {BlockerDisposition.OPEN, BlockerDisposition.RECURRENCE} or blocker.reconciliation_needs:
        return False
    if tuple(alias.alias_value for alias in blocker.aliases if alias.alias_type == "strong_audit_finding") != (source,) or blocker.authoritative_boundary != finding.affected_boundary or blocker.required_correction_outcome != finding.expected_behavior:
        return False
    if len(blocker.qualified_requirements) != 1 or f"#{blocker.qualified_requirements[0].issue_number}/{blocker.qualified_requirements[0].requirement_id}" != finding.requirement_ids[0]:
        return False
    prefix = f"{repository}#{snapshot['pr_number']}:codex-cloud:{snapshot['task_id']}:"
    members = sorted(prefix + hashlib.sha256(f"comment:{root}".encode()).hexdigest() for root in blockers[0].get_root_comment_ids())
    return bool(members) and members == row["native_membership"]


def instruction(repository: str, source: str, row: dict) -> str:
    from .pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
    from .prompt_loader import render_prompt

    snapshot, finding = row["snapshot"], row["snapshot"]["finding"]
    _slots, _owner, _incarnation, run = owner_binding(repository, snapshot["pr_number"])
    details = render_prompt(
        "codex_cloud.scoped_registered_correction",
        include_header=False,
        issue_number=finding["requirement_ids"][0].split("/", 1)[0].lstrip("#"),
        requirement_id=finding["requirement_ids"][0].split("/", 1)[1],
        requirement_text=finding["requirement_texts"][0],
        boundary=finding["affected_boundary"],
        actual_behavior=finding["actual_behavior"],
        expected_behavior=finding["expected_behavior"],
        counterexample=finding["counterexample"],
        regression_scenario=finding["focused_regression_scenario"],
    )
    target = ExistingPrRepairTarget(repository, snapshot["pr_number"], run.publication_head_ref, run.base_branch or "main", snapshot["head_sha"])
    return build_existing_pr_repair_prompt(target, details)


def validate_request(accounting, identity, provenance) -> None:
    from .cloud_provider_instructions import CloudTaskOperation, prepare_cloud_task

    validate_send(accounting, identity, provenance)
    with accounting.slots._state_lock():
        _records, _record, _accounting, sources = _rows(accounting.slots, ImplementationOwner("issue", identity.issue_number), identity.incarnation)
        row = json.loads(json.dumps(sources[provenance.strong_source]))
    live_older = older_inventory(accounting.slots, ImplementationOwner("issue", identity.issue_number), identity.incarnation, provenance.pr_number, sources, current_source_key=provenance.strong_source)
    attested = row["snapshot"]["attestation"]["older_operations"]
    if any({key: item[key] for key in ("operation_id", "snapshot_sha256", "native_membership")} not in attested for item in live_older):
        raise ValueError("A later or edited operation is outside the accepted exhaustive scope")
    if not current_source(identity.repository, provenance.strong_source, row):
        raise ValueError("Accepted Strong source/canonical revision no longer current")
    prompt = instruction(identity.repository, provenance.strong_source, row)
    prepared = prepare_cloud_task(prompt, recipient="codex-cloud", operation=CloudTaskOperation.CONTINUATION, no_edit=False).prepared_task
    if hashlib.sha256(prepared.encode()).hexdigest() != provenance.fingerprint:
        raise ValueError("Native payload is not the exact accepted bounded correction")


def deliver_ordinary(repository, pr_data, github, origin, pending, state_path, delivered):
    """One accepted source through the existing ordinary owner delivery path."""
    from . import pr_processor as processor
    from .codex_work_fence import CodexWorkAuthorityRefused
    from .pr_repair import resolve_existing_pr_repair_target

    if origin.provider != "codex-cloud":
        return None
    pr_number = pr_data["number"]
    head = str(pr_data.get("head", {}).get("sha", ""))
    try:
        selected = source_for_members(repository, pr_number, head, origin.task_id, {identity for _thread, _comment, identity in pending})
    except Exception:
        return None
    if selected is None:
        return None
    source, row = selected
    members = set(row["native_membership"])
    metadata = github.get_pull_request_repair_metadata_strict(repository, pr_number)
    target = resolve_existing_pr_repair_target(repository, {**pr_data, "head": {"ref": metadata.head_ref, "sha": metadata.head_sha}, "base": {"ref": metadata.base_ref}})
    if target is None or target.head_sha != row["snapshot"]["head_sha"]:
        return processor.CloudReviewRepairResult(["Accepted Strong correction held: exact target changed"], deferred=True, wait_reason=processor.WAIT_INDETERMINATE, diagnostic_reason="unconfirmed_delivery_receipt")
    prompt = instruction(repository, source, row)

    def current() -> bool:
        if processor._revalidate_cloud_origin(repository, pr_number, origin, github_client=github) or not processor.new_work_allowed():
            return False
        if processor._get_adjudication_effect_store().force_revalidation_needed(repository, pr_number):
            return False
        exhaustion = processor.check_pr_repair_exhaustion(repository, pr_number)
        if exhaustion and exhaustion.is_exhausted:
            return False
        live = github.get_pull_request_metadata_strict(repository, pr_number)
        if not isinstance(live, dict) or live.get("state") != "open" or live.get("number") != pr_number or live.get("merged") is True:
            return False
        if github.get_pull_request_repair_metadata_strict(repository, pr_number) != metadata:
            return False
        inputs = processor._two_tier_gate_inputs(github, repository, live)
        if inputs is None or inputs.contract.identity != row["snapshot"]["contract_identity"] or inputs.policy.identity != row["snapshot"]["policy_identity"] or inputs.head_sha != target.head_sha or inputs.base_sha != row["snapshot"]["base_sha"]:
            return False
        observed = {str(thread.comments[0].database_id): thread for thread in github.get_pr_review_threads_strict(repository, pr_number) if thread.comments and not thread.is_resolved}
        return current_source(repository, source, row) and bool(row.get("root_bodies")) and all(root in observed and digest(observed[root].comments[0].body) == body_digest for root, body_digest in row["root_bodies"].items())

    with processor._cloud_review_delivery_lock:
        processor._record_review_feedback_state(state_path, delivered, members)
    try:
        accepted = processor._send_followup_with_quota_admission(origin.client, repository, origin.task_id, prompt, tuple(sorted(members)), repair_target=target, recovery_provider="codex-cloud", final_authority_check=current, strong_source=source, strong_revision=row["sealed_revision"])
    except CodexWorkAuthorityRefused:
        processor._record_review_feedback_state(state_path, delivered, set(), remove_pending=members)
        return processor.CloudReviewRepairResult(["Accepted Strong correction held: final authority refused before wire"], deferred=True, wait_reason=processor.WAIT_INDETERMINATE, diagnostic_reason="unconfirmed_delivery_receipt")
    except Exception as exc:
        if origin.client.consume_followup_rejection(origin.task_id, tuple(sorted(members))):
            processor._record_review_feedback_state(state_path, delivered, set(), remove_pending=members)
        return processor.CloudReviewRepairResult([f"Accepted Strong correction remains fenced: {type(exc).__name__}"], deferred=True, wait_reason=processor.WAIT_INDETERMINATE, diagnostic_reason="unconfirmed_delivery_receipt")
    if not accepted:
        if origin.client.consume_followup_rejection(origin.task_id, tuple(sorted(members))):
            processor._record_review_feedback_state(state_path, delivered, set(), remove_pending=members)
        return processor.CloudReviewRepairResult(["Accepted Strong correction remains fenced: native delivery not confirmed"], deferred=True, wait_reason=processor.WAIT_INDETERMINATE, diagnostic_reason="unconfirmed_delivery_receipt")
    processor._record_review_feedback_state(state_path, delivered | members, set(), remove_pending=members)
    processor._record_pr_delivered_review_feedback(github, repository, pr_number, sorted(members), "Auto-Coder delivered the exact accepted Strong correction to its existing owner.")
    return processor.CloudReviewRepairResult([f"Delivered accepted Strong correction for PR #{pr_number}"], delivered=True)


def has_registered_task(repository: str, task_id: str) -> bool:
    """Missing CloudRun evidence cannot turn an adopted task into untracked work."""
    if not repository:
        return False  # Existing positively untracked client has no owner namespace.
    slots = ImplementationSlotRepository(repository, 1)
    with slots._state_lock():
        for record in slots._read().values():
            accounting = record.get(CodexWorkAccounting._FIELD, {})
            if not isinstance(accounting, dict):
                raise ValueError("Registered task accounting is unreadable")
            for row in accounting.get(FIELD, {}).values():
                if row["snapshot"]["task_id"] == task_id:
                    return True
    return False
