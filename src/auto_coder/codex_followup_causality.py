"""Read-only correlation of the native WHAM follow-up request/response graph."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional, Sequence

from .codex_wham_client import WhamTask, WhamTurn

_TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "error", "cancelled", "canceled"})
_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}")
NON_DELIVERY_EVIDENCE_PREFIX = "wham-complete-graph-not-delivered:"


def _parents(turns: Sequence[WhamTurn], by_id: dict[str, WhamTurn]) -> Optional[dict[str, Optional[str]]]:
    parents: dict[str, Optional[str]] = {}
    for turn in turns:
        raw = turn.raw_data
        if not isinstance(raw, dict):
            return None
        inner = raw.get("turn", raw)
        if not isinstance(inner, dict) or raw.get("id", turn.id) != turn.id or inner.get("id", turn.id) != turn.id:
            return None
        values = [container[key] for container, key in ((raw, "parent"), (inner, "previous_turn_id")) if key in container]
        if not values or any(value is not None and (not isinstance(value, str) or not value) for value in values) or len(set(values)) != 1:
            return None
        parent = values[0]
        if parent is not None and parent not in by_id:
            return None
        parents[turn.id] = parent
    expected_children: dict[str, set[str]] = {identity: set() for identity in by_id}
    for identity, parent in parents.items():
        if parent is not None:
            expected_children[parent].add(identity)
    for turn in turns:
        raw = turn.raw_data
        assert isinstance(raw, dict)
        inner = raw.get("turn", raw)
        assert isinstance(inner, dict)
        for container in (raw, inner):
            if "children" not in container:
                continue
            children = container["children"]
            if not isinstance(children, list) or any(not isinstance(identity, str) for identity in children) or len(set(children)) != len(children) or set(children) != expected_children[turn.id]:
                return None
    visited: set[str] = set()
    for identity in parents:
        path: set[str] = set()
        cursor: Optional[str] = identity
        while cursor is not None and cursor not in visited:
            if cursor in path:
                return None
            path.add(cursor)
            cursor = parents[cursor]
        visited.update(path)
    return parents


def native_user_text(turn: WhamTurn) -> Optional[str]:
    raw = turn.raw_data
    if not isinstance(raw, dict):
        return None
    inner = raw.get("turn", raw)
    items = inner.get("input_items") if isinstance(inner, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    message = items[0]
    content = message.get("content")
    if message.get("type") != "message" or message.get("role") != "user" or not isinstance(content, list) or not content:
        return None
    parts = []
    for item in content:
        if not isinstance(item, dict) or item.get("content_type") != "text" or not isinstance(item.get("text"), str):
            return None
        parts.append(item["text"])
    return "".join(parts)


def followup_completion(task_id: str, baseline_id: str, fingerprint: str, turns: Sequence[WhamTurn]) -> Optional[tuple[str, str]]:
    """Return native user/assistant identities only for one coherent exact chain.

    Provider mapping order is not causality. Missing nodes, sibling requests,
    duplicate identities/text and contradictory parent/child edges stay unknown.
    No source journal or delivery observation is changed here.
    """
    by_id = {turn.id: turn for turn in turns}
    if len(by_id) != len(turns) or any(not turn.id.startswith(task_id + "~") or turn.role.lower() not in {"user", "human", "assistant", "agent", "bot", "asst"} for turn in turns):
        return None
    parents = _parents(turns, by_id)
    if parents is None:
        return None
    baseline = by_id.get(baseline_id)
    if baseline is None or baseline.role.lower() not in {"assistant", "agent", "bot", "asst"}:
        return None
    users = [turn for turn in turns if turn.role.lower() in {"user", "human"} and parents[turn.id] == baseline_id]
    if len(users) != 1:
        return None
    user = users[0]
    text = native_user_text(user)
    if text is None or hashlib.sha256(text.encode("utf-8")).hexdigest() != fingerprint:
        return None
    user_inputs = [native_user_text(turn) for turn in turns if turn.role.lower() in {"user", "human"}]
    if any(value is None for value in user_inputs) or sum(hashlib.sha256(value.encode("utf-8")).hexdigest() == fingerprint for value in user_inputs if value is not None) != 1:
        return None
    responses = [turn for turn in turns if turn.role.lower() in {"assistant", "agent", "bot", "asst"} and parents[turn.id] == user.id]
    if len(responses) != 1 or responses[0].status.lower() not in _TERMINAL_TURN_STATUSES:
        return None
    response = responses[0]
    return user.id, response.id


def settled_current_task(task_id: str, task: Optional[WhamTask], turns: Sequence[WhamTurn]) -> Optional[WhamTurn]:
    """Observe a successful current request/response in a complete idle graph.

    This says nothing about omitted historical requests. It is only a current
    task observation; delivery, correction and thread-closure authority remain
    with their existing owners.
    """
    from .codex_observation import current_execution_evidence

    evidence = current_execution_evidence(task, task_id)
    if not evidence.recovery_eligible or task is None or task.current_assistant_turn is None or task.current_user_turn is None:
        return None
    by_id = {turn.id: turn for turn in turns}
    if not turns or len(by_id) != len(turns) or any(not turn.id.startswith(task_id + "~") or turn.role.lower() not in {"user", "human", "assistant", "agent", "bot", "asst"} for turn in turns):
        return None
    parents = _parents(turns, by_id)
    if parents is None:
        return None
    assistant = by_id.get(evidence.assistant_turn_id)
    user = by_id.get(evidence.user_turn_id)
    if assistant is None or user is None or assistant.role.lower() not in {"assistant", "agent", "bot", "asst"} or user.role.lower() not in {"user", "human"}:
        return None
    if assistant.status.lower() != "completed" or assistant.status != task.current_assistant_turn.status or assistant.created_at != task.current_assistant_turn.created_at:
        return None
    if user.status != task.current_user_turn.status or user.created_at != task.current_user_turn.created_at or native_user_text(user) is None:
        return None
    if parents[assistant.id] != user.id or [turn.id for turn in turns if parents[turn.id] == user.id] != [assistant.id] or any(parent == assistant.id for parent in parents.values()):
        return None
    for turn in turns:
        if turn.role.lower() in {"assistant", "agent", "bot", "asst"}:
            if turn.status.lower() not in _TERMINAL_TURN_STATUSES:
                return None
        elif turn.status.lower() in {"pending", "queued", "in_progress", "running", "active"}:
            return None
    return assistant


def followup_non_delivery_evidence(
    task_id: str,
    baseline_id: str,
    fingerprint: str,
    turns: Sequence[WhamTurn],
    *,
    current_assistant_turn_id: Optional[str] = None,
    current_assistant_turn_status: Optional[str] = None,
) -> Optional[str]:
    """Prove definite non-delivery from a complete, coherent, idle WHAM turn graph.

    Returns a deterministic cryptographic evidence ID only when all of the
    following hold:
    - The turn set is non-empty, unique, task-scoped, and passes bidirectional
      parent/child graph verification (``_parents``).
    - ``baseline_id`` is an assistant turn in the graph and already has at least
      one later user turn whose child assistant turn reached a terminal status.
    - Every assistant turn in the graph and the live ``current_assistant_turn``
      are in a terminal status (nothing is running or queued).
    - Every user turn exposes readable input text and zero user turns match
      ``fingerprint``.
    """
    if not task_id or not baseline_id or not isinstance(fingerprint, str) or _HEX_SHA256_RE.fullmatch(fingerprint) is None or not turns:
        return None
    if not isinstance(current_assistant_turn_id, str) or not current_assistant_turn_id or not isinstance(current_assistant_turn_status, str) or current_assistant_turn_status.lower() not in _TERMINAL_TURN_STATUSES:
        return None
    by_id = {turn.id: turn for turn in turns}
    if len(by_id) != len(turns) or any(not turn.id.startswith(task_id + "~") or turn.role.lower() not in {"user", "human", "assistant", "agent", "bot", "asst"} for turn in turns):
        return None
    parents = _parents(turns, by_id)
    if parents is None:
        return None
    baseline = by_id.get(baseline_id)
    if baseline is None or baseline.role.lower() not in {"assistant", "agent", "bot", "asst"}:
        return None
    current_turn = by_id.get(current_assistant_turn_id)
    if current_turn is None or current_turn.role.lower() not in {"assistant", "agent", "bot", "asst"}:
        return None
    assistant_turns = [turn for turn in turns if turn.role.lower() in {"assistant", "agent", "bot", "asst"}]
    if any(turn.status.lower() not in _TERMINAL_TURN_STATUSES for turn in assistant_turns):
        return None
    user_turns = [turn for turn in turns if turn.role.lower() in {"user", "human"}]
    if not user_turns:
        return None
    user_fingerprints: list[tuple[str, str]] = []
    for turn in user_turns:
        text = native_user_text(turn)
        if text is None:
            return None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest == fingerprint:
            return None
        user_fingerprints.append((turn.id, digest))
    baseline_user_children = [turn for turn in user_turns if parents[turn.id] == baseline_id]
    has_completed_successor = any(any(turn.status.lower() in _TERMINAL_TURN_STATUSES and parents[turn.id] == user.id for turn in assistant_turns) for user in baseline_user_children)
    if not has_completed_successor:
        return None
    payload = {
        "version": 1,
        "task_id": task_id,
        "baseline_id": baseline_id,
        "fingerprint": fingerprint,
        "current_assistant_turn_id": current_assistant_turn_id,
        "turn_ids": sorted(by_id),
        "user_fingerprints": sorted(user_fingerprints),
    }
    evidence_digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"{NON_DELIVERY_EVIDENCE_PREFIX}{evidence_digest}"
