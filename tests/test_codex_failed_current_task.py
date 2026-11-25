"""Native failed-turn observation must never imply successful task recovery."""

from copy import deepcopy
from dataclasses import replace

import pytest

from auto_coder.codex_followup_causality import failed_current_task, settled_current_task
from auto_coder.codex_observation import current_execution_evidence
from auto_coder.codex_wham_client import WhamTask, WhamTurn

TASK_ID = "task_e_failed6019"
INITIAL_USER = f"{TASK_ID}~usertrn_initial"
BASELINE = f"{TASK_ID}~assttrn_baseline"
CURRENT_USER = f"{TASK_ID}~usertrn_current"
CURRENT_ASSISTANT = f"{TASK_ID}~assttrn_current"


def _turn(identity, role, status, parent, children, created_at):
    inner = {"id": identity, "previous_turn_id": parent}
    if role == "user":
        inner["input_items"] = [{"type": "message", "role": "user", "content": [{"content_type": "text", "text": "Existing PR correction"}]}]
    return WhamTurn(identity, role, status, str(created_at), {"id": identity, "parent": parent, "children": children, "turn": inner})


@pytest.fixture
def failed_evidence():
    turns = [
        _turn(INITIAL_USER, "user", "", None, [BASELINE], 100),
        _turn(BASELINE, "assistant", "completed", INITIAL_USER, [CURRENT_USER], 101),
        _turn(CURRENT_USER, "user", "", BASELINE, [CURRENT_ASSISTANT], 200),
        _turn(CURRENT_ASSISTANT, "assistant", "failed", CURRENT_USER, [], 201),
    ]
    task = WhamTask(id=TASK_ID, status="failed", current_user_turn=turns[2], current_assistant_turn=turns[3], latest_turn_status="failed")
    return task, turns


@pytest.mark.parametrize("status", ["failed", "error"])
def test_failed_current_task_observation_preserves_failure_semantics(failed_evidence, status):
    task, turns = failed_evidence
    assistant = replace(turns[-1], status=status)
    turns = [*turns[:-1], assistant]
    task = replace(task, status=status, latest_turn_status=status, current_assistant_turn=assistant)
    before = deepcopy((task, turns))

    assert failed_current_task(TASK_ID, task, turns) == assistant
    assert settled_current_task(TASK_ID, task, turns) is None
    assert current_execution_evidence(task, TASK_ID).recovery_eligible is False
    assert (task, turns) == before


@pytest.mark.parametrize("status", ["completed", "cancelled", "canceled", "running", "in_progress", "queued", "unknown", ""])
def test_only_failed_or_error_native_current_turn_qualifies(failed_evidence, status):
    task, turns = failed_evidence
    assistant = replace(turns[-1], status=status)
    task = replace(task, status=status, latest_turn_status=status, current_assistant_turn=assistant)

    assert failed_current_task(TASK_ID, task, [*turns[:-1], assistant]) is None


@pytest.mark.parametrize("latest", ["", "completed", "error", "running", "queued", "unknown"])
def test_failed_current_turn_requires_matching_latest_status(failed_evidence, latest):
    task, turns = failed_evidence

    assert failed_current_task(TASK_ID, replace(task, latest_turn_status=latest), turns) is None


@pytest.mark.parametrize(
    "damage",
    [
        "missing-task",
        "foreign-task",
        "missing-assistant",
        "missing-user",
        "different-assistant",
        "different-user",
        "assistant-time",
        "user-time",
        "user-status",
        "queued-user",
        "active-assistant",
        "incomplete-graph",
        "duplicate-turn",
        "contradictory-parent",
        "contradictory-children",
        "unreadable-current-user",
        "newer-request",
    ],
)
def test_failed_current_turn_requires_complete_idle_current_graph(failed_evidence, damage):
    task, turns = deepcopy(failed_evidence)
    if damage == "missing-task":
        task = None
    elif damage == "foreign-task":
        task = replace(task, id="task_e_other")
    elif damage == "missing-assistant":
        task = replace(task, current_assistant_turn=None)
    elif damage == "missing-user":
        task = replace(task, current_user_turn=None)
    elif damage == "different-assistant":
        task = replace(task, current_assistant_turn=replace(task.current_assistant_turn, id=f"{TASK_ID}~assttrn_unobserved"))
    elif damage == "different-user":
        task = replace(task, current_user_turn=replace(task.current_user_turn, id=f"{TASK_ID}~usertrn_unobserved"))
    elif damage == "assistant-time":
        task = replace(task, current_assistant_turn=replace(task.current_assistant_turn, created_at="202"))
    elif damage == "user-time":
        task = replace(task, current_user_turn=replace(task.current_user_turn, created_at="202"))
    elif damage == "user-status":
        task = replace(task, current_user_turn=replace(task.current_user_turn, status="completed"))
    elif damage == "queued-user":
        user = replace(task.current_user_turn, status="queued")
        task = replace(task, current_user_turn=user)
        turns[2] = user
    elif damage == "active-assistant":
        turns[1] = replace(turns[1], status="in_progress")
    elif damage == "incomplete-graph":
        turns.pop(1)
    elif damage == "duplicate-turn":
        turns.append(turns[-1])
    elif damage == "contradictory-parent":
        turns[-1].raw_data["parent"] = INITIAL_USER
    elif damage == "contradictory-children":
        turns[-2].raw_data["children"] = []
    elif damage == "unreadable-current-user":
        turns[-2].raw_data["turn"].pop("input_items")
    else:
        new_user = f"{TASK_ID}~usertrn_new"
        turns[-1].raw_data["children"] = [new_user]
        turns.append(_turn(new_user, "user", "", CURRENT_ASSISTANT, [], 202))

    assert failed_current_task(TASK_ID, task, turns) is None
