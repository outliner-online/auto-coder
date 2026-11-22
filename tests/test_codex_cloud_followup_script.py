"""Exercise the maintained Codex Cloud follow-up script with local HTTP/CLI doubles."""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex-cloud-followup"
TASK_ID = "task_e_test123"
ASSISTANT_ID = f"{TASK_ID}~assttrn_1"
API = "https://chatgpt.com/backend-api/wham/tasks"


@pytest.fixture
def harness(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    auth_dir = tmp_path / ".codex"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "test-token", "account_id": "test-account"}}),
        encoding="utf-8",
    )
    (bin_dir / "codex").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$MOCK_CODEX_FAIL" == 1 ]]; then exit 9; fi
if [[ "$*" == *"--cursor "* ]]; then
  printf '%s\\n' "$MOCK_PAGE2"
else
  printf '%s\\n' "$MOCK_PAGE1"
fi
""",
        encoding="utf-8",
    )
    (bin_dir / "curl").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
for url; do :; done
printf '%s\\n' "$url" >> "$MOCK_REQUESTS"
case "$url" in
  */turns) cat "$MOCK_TURNS" ;;
  */tasks)
    cat > "$MOCK_PAYLOAD"
    if [[ "$MOCK_POST_TIMEOUT" == 1 ]]; then exit 28; fi
    printf '{"accepted":true}\\n'
    ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    for name in ("codex", "curl"):
        (bin_dir / name).chmod(0o755)

    turns = tmp_path / "turns.json"
    turns.write_text(
        json.dumps(
            {
                "turn_mapping": {
                    "1": {
                        "id": ASSISTANT_ID,
                        "turn": {
                            "role": "assistant",
                            "turn_status": "completed",
                            "created_at": "2026-11-22T02:00:00Z",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    requests = tmp_path / "requests.log"
    payload = tmp_path / "payload.json"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HOME": str(tmp_path),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "MOCK_TURNS": str(turns),
            "MOCK_REQUESTS": str(requests),
            "MOCK_PAYLOAD": str(payload),
            "MOCK_POST_TIMEOUT": "0",
            "MOCK_CODEX_FAIL": "0",
            "MOCK_PAGE1": json.dumps({"tasks": [{"id": TASK_ID, "status": "paused"}], "cursor": None}),
            "MOCK_PAGE2": json.dumps({"tasks": [], "cursor": None}),
        }
    )
    return env, turns, requests, payload


def invoke(harness, *args):
    env, _, _, _ = harness
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def calls(requests):
    return requests.read_text(encoding="utf-8").splitlines() if requests.exists() else []


@pytest.mark.parametrize("status", ["pending", "queued", "running", "in_progress", "ready", "completed", "error", "failed", "unknown"])
def test_if_paused_never_posts_in_non_paused_states(harness, status):
    env, _, requests, _ = harness
    env["MOCK_PAGE1"] = json.dumps({"tasks": [{"id": TASK_ID, "status": status}], "cursor": None})
    result = invoke(harness, "--if-paused", TASK_ID)
    assert result.returncode == 0, result.stderr
    assert f"task is {status}" in result.stdout
    assert calls(requests) == []


@pytest.mark.parametrize("status", ["paused", "waiting_for_input", "idle", "stopped"])
def test_if_paused_posts_exact_followup_when_last_turn_completed(harness, status):
    env, _, requests, payload = harness
    env["MOCK_PAGE1"] = json.dumps({"tasks": [{"id": TASK_ID, "status": status}], "cursor": None})
    result = invoke(harness, "--if-paused", TASK_ID, "Continue PR #6021")
    assert result.returncode == 0, result.stderr
    assert calls(requests) == [f"{API}/{TASK_ID}/turns", API]
    request = json.loads(payload.read_text(encoding="utf-8"))
    assert request["follow_up"] == {
        "task_id": TASK_ID,
        "turn_id": ASSISTANT_ID,
        "run_environment_in_qa_mode": False,
    }
    assert request["input_items"] == [
        {"type": "message", "role": "user", "content": [{"content_type": "text", "text": "Continue PR #6021"}]}
    ]
    assert "test-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("assistant_status", ["in_progress", "queued", "", "failed", "error", "cancelled"])
def test_if_paused_refuses_incomplete_or_unusable_assistant(harness, assistant_status):
    _, turns, requests, _ = harness
    data = json.loads(turns.read_text(encoding="utf-8"))
    data["turn_mapping"]["1"]["turn"]["turn_status"] = assistant_status
    turns.write_text(json.dumps(data), encoding="utf-8")
    result = invoke(harness, "--if-paused", TASK_ID)
    assert result.returncode == 0
    assert "Skipped: latest assistant turn" in result.stdout
    assert calls(requests) == [f"{API}/{TASK_ID}/turns"]


def test_if_paused_refuses_when_newer_user_turn_exists(harness):
    _, turns, requests, _ = harness
    data = json.loads(turns.read_text(encoding="utf-8"))
    data["turn_mapping"]["2"] = {
        "id": f"{TASK_ID}~usertrn_2",
        "turn": {"role": "user", "turn_status": "completed", "created_at": "2026-11-22T03:00:00Z"},
    }
    turns.write_text(json.dumps(data), encoding="utf-8")
    result = invoke(harness, "--if-paused", TASK_ID)
    assert result.returncode == 0
    assert "latest turn is not an assistant" in result.stdout
    assert calls(requests) == [f"{API}/{TASK_ID}/turns"]


def test_if_paused_uses_pagination_and_skips_unknown_task(harness):
    env, _, requests, _ = harness
    env["MOCK_PAGE1"] = json.dumps({"tasks": [], "cursor": "next-page"})
    env["MOCK_PAGE2"] = json.dumps({"tasks": [{"id": TASK_ID, "status": "paused"}], "cursor": None})
    assert invoke(harness, "--if-paused", TASK_ID).returncode == 0
    assert calls(requests) == [f"{API}/{TASK_ID}/turns", API]

    env["MOCK_PAGE2"] = json.dumps({"tasks": [], "cursor": None})
    unknown = invoke(harness, "--if-paused", "task_e_other123")
    assert "task not found" in unknown.stdout
    assert calls(requests) == [f"{API}/{TASK_ID}/turns", API]


def test_if_paused_is_at_most_once_per_assistant_turn_even_after_timeout(harness):
    env, _, requests, _ = harness
    env["MOCK_POST_TIMEOUT"] = "1"
    first = invoke(harness, "--if-paused", TASK_ID)
    assert first.returncode != 0
    assert calls(requests) == [f"{API}/{TASK_ID}/turns", API]

    retry = invoke(harness, "--if-paused", TASK_ID)
    assert retry.returncode == 0
    assert "already attempted" in retry.stdout
    assert calls(requests) == [f"{API}/{TASK_ID}/turns", API, f"{API}/{TASK_ID}/turns"]


def test_if_paused_requires_known_status_and_available_cli(harness):
    env, _, requests, _ = harness
    env["MOCK_PAGE1"] = json.dumps({"tasks": [], "cursor": None})
    assert "task not found" in invoke(harness, "--if-paused", TASK_ID).stdout
    assert calls(requests) == []
    env["MOCK_CODEX_FAIL"] = "1"
    failed = invoke(harness, "--if-paused", TASK_ID)
    assert failed.returncode != 0
    assert calls(requests) == []


def test_explicit_send_does_not_require_pause(harness):
    _, turns, requests, payload = harness
    data = json.loads(turns.read_text(encoding="utf-8"))
    data["turn_mapping"]["1"]["turn"]["turn_status"] = "in_progress"
    turns.write_text(json.dumps(data), encoding="utf-8")
    result = invoke(harness, TASK_ID, "Manual followup")
    assert result.returncode == 0, result.stderr
    assert calls(requests) == [f"{API}/{TASK_ID}/turns", API]
    assert json.loads(payload.read_text(encoding="utf-8"))["input_items"][0]["content"][0]["text"] == "Manual followup"


def test_missing_auth_or_invalid_task_never_reaches_http(harness):
    env, _, requests, _ = harness
    assert invoke(harness, "--if-paused", "invalid").returncode == 2
    assert calls(requests) == []
    (Path(env["HOME"]) / ".codex" / "auth.json").unlink()
    result = invoke(harness, TASK_ID, "Manual followup")
    assert result.returncode != 0
    assert "Missing or invalid" in result.stderr
    assert calls(requests) == []
