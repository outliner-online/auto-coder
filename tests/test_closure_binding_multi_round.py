"""A closure covering findings from several Strong rounds keeps its newer discussion binding."""

from types import SimpleNamespace

import pytest

from auto_coder import pr_processor as processor
from auto_coder.effective_decision_application import PUBLICATION_CONFIRMED


def _fixture(monkeypatch, accepted_sources):
    original = SimpleNamespace(name="original", thread_dispositions=[])
    newer_result = SimpleNamespace(name="newer", thread_dispositions=[], allows_auto_merge=True)
    key = ("repo", 1, "h", "b", "c", "p", 0)
    source = SimpleNamespace(
        repository="repo",
        pr_number=1,
        head_sha="h",
        base_sha="b",
        contract_identity="c",
        policy_identity="p",
        open_epoch=0,
        round_id="round-2",
        audited_head_sha="a",
        finding_set_revision=2,
        attempt_sequence=232,
        dispositions=[SimpleNamespace(finding_id="f1"), SimpleNamespace(finding_id="f2")],
    )
    newer = SimpleNamespace(
        repository="repo",
        pr_number=1,
        head_sha="h",
        base_sha="b",
        contract_identity="c",
        policy_identity="p",
        open_epoch=0,
        strong_round_id="round-2",
        audited_head_sha="a",
        finding_set_revision=2,
        attempt_sequence=233,
        attempt_id="n",
        accepted_sources=accepted_sources,
        restore=lambda: newer_result,
    )
    retained = SimpleNamespace(status="PASS", publication=PUBLICATION_CONFIRMED, ordinary_correction=newer)

    class Store:
        def __init__(self, repo):
            pass

        def load(self, pr):
            return retained

    attempts = SimpleNamespace(latest_sequence=lambda pr, head: 233, get=lambda attempt_id: SimpleNamespace(sequence=233, pr_number=1, head_sha="h", status="PASS"))
    cycle = SimpleNamespace(
        strong_rounds=(SimpleNamespace(claim_id="claim-1", round_id="round-1"), SimpleNamespace(claim_id="claim-2", round_id="round-2")),
        findings=(SimpleNamespace(finding_id="f1", origin_round_id="claim-1"), SimpleNamespace(finding_id="f2", origin_round_id="claim-2")),
    )
    inputs = SimpleNamespace(gate=SimpleNamespace(closure_evidence=lambda: SimpleNamespace(attempts=attempts), state=SimpleNamespace(snapshot=lambda pr: cycle)))
    monkeypatch.setattr(processor, "EffectiveDecisionStore", Store)
    monkeypatch.setattr(processor, "restore_ordinary_result", lambda record: original)
    monkeypatch.setattr(processor, "replace", lambda obj, **kw: obj)
    return inputs, source, original, newer_result


@pytest.mark.parametrize("complete", [True, False])
def test_newer_binding_requires_each_finding_source_from_its_own_round(monkeypatch, complete):
    sources = ("round-1:f1", "round-2:f2") if complete else ("round-2:f1", "round-2:f2")
    inputs, source, original, newer_result = _fixture(monkeypatch, sources)
    assert processor._closure_discussion_binding("repo", 1, inputs, source) is (newer_result if complete else original)
