"""Controller-validated evidence for bounded reviewer source corrections.

This evidence permits publication of a correction candidate only. It never
approves the changed candidate or replaces independent review and CI.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ReviewCorrectionBlocker:
    """A demonstrated input-head defect tied to explicit Requirements."""

    requirement_ids: tuple[str, ...]
    requirement_texts: tuple[str, ...]
    boundary: str
    evidence: str
    counterexample: str
    required_behavior: str
    actual_behavior: str
    regression: str
    kind: str = "IMPLEMENTATION"


@dataclass(frozen=True)
class ReviewCorrectionBasis:
    """The validated pre-edit decision retained with its correction attempt."""

    head_sha: str
    contract_identity: str
    verdict: str
    blockers: tuple[ReviewCorrectionBlocker, ...]
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def validate_review_correction_basis_json(value: str, expected_head_sha: str) -> str:
    """Return a refusal reason, or an empty string for complete bound evidence.

    The production response consumers validate requirement membership, scope,
    and finding semantics before creating this controller-owned record. Recovery
    checks the retained record rather than inventing authority from a dirty tree.
    """
    if not value:
        return "no validated pre-edit correction basis was retained"
    try:
        raw = json.loads(value)
    except (TypeError, ValueError):
        return "retained correction basis is not valid JSON"
    expected_keys = {"version", "head_sha", "contract_identity", "verdict", "blockers"}
    if not isinstance(raw, dict) or set(raw) != expected_keys or type(raw.get("version")) is not int or raw["version"] != 1:
        return "retained correction basis has an unsupported schema"
    head = raw.get("head_sha")
    if not isinstance(head, str) or not expected_head_sha or head.lower() != expected_head_sha.lower():
        return "correction basis does not belong to the reviewed head"
    contract = raw.get("contract_identity")
    if not isinstance(contract, str) or not contract.strip():
        return "correction basis has no authoritative Requirements snapshot"
    if not isinstance(raw.get("verdict"), str) or raw["verdict"] not in {"NEEDS_FIX", "NEEDS_TESTS", "FINDINGS"}:
        return "the pre-edit review decision does not authorize a correction"
    blockers = raw.get("blockers")
    if not isinstance(blockers, list) or not blockers:
        return "correction basis has no demonstrated blocker"
    fields = {"requirement_ids", "requirement_texts", "boundary", "evidence", "counterexample", "required_behavior", "actual_behavior", "regression", "kind"}
    for blocker in blockers:
        if not isinstance(blocker, dict) or set(blocker) != fields:
            return "correction blocker has an unsupported schema"
        ids, texts = blocker.get("requirement_ids"), blocker.get("requirement_texts")
        if not isinstance(ids, list) or not isinstance(texts, list) or not ids or len(ids) != len(texts):
            return "correction blocker lacks explicit Requirement bindings"
        if any(not isinstance(part, str) or not part.strip() for part in [*ids, *texts]):
            return "correction blocker contains an empty Requirement binding"
        if len(ids) != len(set(ids)):
            return "correction blocker repeats a Requirement binding"
        if not isinstance(blocker.get("kind"), str) or blocker["kind"] not in {"IMPLEMENTATION", "TEST_ORACLE"}:
            return "correction blocker has an unsupported kind"
        for key in ("boundary", "evidence", "counterexample", "required_behavior", "actual_behavior", "regression"):
            if not isinstance(blocker.get(key), str) or not blocker[key].strip():
                return "correction blocker lacks concrete pre-edit evidence"
    return ""
