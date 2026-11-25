"""New discussion evidence preserves Strong findings from every owning round."""

from dataclasses import replace

import pytest

from auto_coder.accepted_finding_bridge import AcceptedFindingBridge, ProjectionTarget
from auto_coder.adversarial_validator import ReviewThreadDisposition
from auto_coder.canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
from auto_coder.effective_decision_application import EffectiveDecisionStore, RetainedDecision, retain_ordinary_correction
from auto_coder.ordinary_closure_evidence import ApplicationStatus
from auto_coder.pr_processor import TwoTierGateInputs, _capture_ordinary_correction_authority, _closure_discussion_binding
from auto_coder.pr_review_cycle import VERDICT_FINDINGS, RoundProvenance
from auto_coder.two_tier_pr_gate import TwoTierPrGate
from tests.test_ordinary_closure_evidence import CONTRACT, POLICY, PR, REPO, _commit, _finding, _produce, world  # noqa: F401


@pytest.fixture
def origin_binding(world, monkeypatch):
    cycle = world.cycle()
    first_round = cycle.snapshot(PR).accepted_strong_round
    provenance = RoundProvenance(world.h2, world.base)
    cycle.record_ordinary_pass(PR, provenance, CONTRACT)
    claim = cycle.claim_strong_audit(PR, provenance, CONTRACT, POLICY)
    second_round = cycle.record_strong_result(PR, claim.claim_id, VERDICT_FINDINGS, "codex/strong", [_finding("f3", claim.claim_id)])
    cycle.acknowledge_publication(PR, second_round.round_id)
    world.round_id, world.h0 = second_round.round_id, world.h2
    world.h2 = _commit(world.git_dir, "h3.txt")
    producing_result = _produce(world, statuses={"f1": "FIXED", "f2": "FIXED", "f3": "FIXED"})
    source = world.service().retain(producing_result).record
    assert source is not None
    assert world.service().apply(source.source_id, world.observe()).status is ApplicationStatus.ACCEPTED
    source = world.store().inspect(source.source_id).record
    assert source is not None
    assert source.round_id == second_round.round_id
    assert first_round is not None and first_round.round_id != second_round.round_id

    # Reconstruct the owners before retaining a later real registered result.
    gate = TwoTierPrGate(REPO, world.cycle())
    monkeypatch.setattr(gate, "closure_evidence", world.service)
    inputs = TwoTierGateInputs(gate, CONTRACT, POLICY, world.h2, world.base)
    attempt = world.attempts().start(PR, world.h2)
    current_result = replace(
        producing_result,
        attempt_id=attempt.attempt_id,
        attempt_sequence=attempt.sequence,
        closure_assessment=None,
        thread_dispositions=[ReviewThreadDisposition(thread_id="thread-f3", status="ADDRESSED", rationale="The exact invariant holds.", evidence="tests/test_state.py covers the current transition.", discussion_evidence_digest="current-discussion")],
    )
    world.attempts().finish(attempt.attempt_id, "PASS")
    world.attempts().mark_published(attempt.attempt_id)
    bridge = AcceptedFindingBridge(world.cycle(), CanonicalPRBlockerLedger(world.root / "ledger.db"))
    projection = bridge.project(ProjectionTarget(repository=REPO, pr_number=PR, head_sha=world.h2, base_sha=world.base, contract_identity=CONTRACT.identity, policy_identity=POLICY.identity))
    authority = _capture_ordinary_correction_authority(REPO, PR, inputs, attempt)
    ordinary = retain_ordinary_correction(current_result, projection, authority=authority)
    assert set(ordinary.accepted_sources) == {f"{first_round.round_id}:f1", f"{first_round.round_id}:f2", f"{second_round.round_id}:f3"}
    store = EffectiveDecisionStore(REPO, world.root / "effective.json")
    store.save(RetainedDecision(repository=REPO, pr_number=PR, head_sha=world.h2, base_sha=world.base, status="PASS", publication="CONFIRMED", attempt_id=attempt.attempt_id, attempt_sequence=attempt.sequence, ordinary_correction=ordinary))
    monkeypatch.setattr("auto_coder.pr_processor.EffectiveDecisionStore", lambda _repository: EffectiveDecisionStore(REPO, store.storage_path))
    return inputs, source, ordinary, store


def test_discussion_binding_uses_each_findings_accepted_origin(origin_binding):
    inputs, source, ordinary, store = origin_binding
    before = store.storage_path.read_bytes()
    current = _closure_discussion_binding(REPO, PR, inputs, source)
    assert current.attempt_id == ordinary.attempt_id
    assert current.thread_dispositions[0].discussion_evidence_digest == "current-discussion"
    assert store.storage_path.read_bytes() == before
    assert inputs.gate.state.snapshot(PR).accepted_closure.source_identity == source.source_id


@pytest.mark.parametrize("invalid_sources", ["missing-earlier", "rebound-to-latest"])
def test_discussion_binding_rejects_missing_or_reassigned_producers(origin_binding, invalid_sources):
    inputs, source, ordinary, store = origin_binding
    sources = (f"{source.round_id}:f3",) if invalid_sources == "missing-earlier" else tuple(f"{source.round_id}:{finding_id}" for finding_id in ("f1", "f2", "f3"))
    store.update(PR, ordinary_correction=replace(ordinary, accepted_sources=sources))
    selected = _closure_discussion_binding(REPO, PR, inputs, source)
    assert selected.attempt_id == source.attempt_id
    assert selected.attempt_sequence == source.attempt_sequence


@pytest.mark.parametrize("missing_owner", ["finding", "round", "membership", "duplicate-finding", "duplicate-round"])
def test_discussion_binding_requires_unique_retained_producer(origin_binding, monkeypatch, missing_owner):
    inputs, source, _ordinary, _store = origin_binding
    snapshot = inputs.gate.state.snapshot(PR)
    first = next(finding for finding in snapshot.findings if finding.finding_id == "f1")
    if missing_owner == "finding":
        invalid = replace(snapshot, findings=tuple(finding for finding in snapshot.findings if finding.finding_id != "f1"))
    elif missing_owner == "round":
        invalid = replace(snapshot, strong_rounds=tuple(record for record in snapshot.strong_rounds if record.claim_id != first.origin_round_id))
    elif missing_owner == "duplicate-finding":
        invalid = replace(snapshot, findings=(*snapshot.findings, first))
    elif missing_owner == "duplicate-round":
        owner = next(record for record in snapshot.strong_rounds if record.claim_id == first.origin_round_id)
        invalid = replace(snapshot, strong_rounds=(*snapshot.strong_rounds, replace(owner, round_id="another-owner")))
    else:
        invalid = replace(snapshot, strong_rounds=tuple(replace(record, finding_ids=tuple(identifier for identifier in record.finding_ids if identifier != "f1")) for record in snapshot.strong_rounds))
    monkeypatch.setattr(inputs.gate.state, "snapshot", lambda _pr: invalid)
    selected = _closure_discussion_binding(REPO, PR, inputs, source)
    assert selected.attempt_id == source.attempt_id
    assert selected.attempt_sequence == source.attempt_sequence
