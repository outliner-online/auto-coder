import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.cloud_manager import CloudManager


def _forbid_jules_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every retired provider operation fail loudly if it is reached."""
    forbidden = AssertionError("retired Jules maintenance was invoked")
    monkeypatch.setattr("auto_coder.jules_client.JulesClient", MagicMock(side_effect=forbidden))
    monkeypatch.setattr("auto_coder.jules_client.invalidate_jules_sessions_cache", MagicMock(side_effect=forbidden))
    monkeypatch.setattr("auto_coder.jules_engine.check_and_resume_or_archive_sessions", MagicMock(side_effect=forbidden))
    monkeypatch.setattr("auto_coder.jules_engine.check_and_start_recurrent_jules_tasks", MagicMock(side_effect=forbidden))
    monkeypatch.setattr("auto_coder.issue_processor.handle_stale_jules_issue_sessions", MagicMock(side_effect=forbidden))
    monkeypatch.setattr("auto_coder.speculative_jules_lifecycle.consume_due_speculative_work", MagicMock(side_effect=forbidden))


def test_producer_wake_keeps_neutral_maintenance_without_jules_replay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A legacy Jules state cannot gate, replay, or spin the production producer."""
    prompts = tmp_path / ".auto-coder" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "legacy.md").write_text("---\ntags: [jules, recurrent]\nname: [maintenance]\n---\nlegacy\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    history = CloudManager("owner/repo")
    assert history.add_session(42, "accepted-session", provider="jules")
    before = history.cloud_file_path.read_bytes()
    monkeypatch.chdir(tmp_path)
    _forbid_jules_maintenance(monkeypatch)
    codex_send = MagicMock(side_effect=AssertionError("legacy prompt converted to Codex"))
    claude_send = MagicMock(side_effect=AssertionError("legacy prompt converted to Claude"))
    monkeypatch.setattr("auto_coder.codex_cloud_client.CodexCloudClient.submit_task", codex_send)
    monkeypatch.setattr("auto_coder.claude_routine_client.ClaudeRoutineClient.fire_routine", claude_send)

    engine = AutomationEngine(MagicMock(), AutomationConfig())
    monkeypatch.setattr(engine, "_check_and_handle_closed_branch", lambda *_: True)
    engine._next_local_job_workspace_reclamation = float("inf")
    updates: list[str] = []
    monkeypatch.setattr(
        "auto_coder.automation_engine.check_for_updates_and_restart",
        lambda: updates.append("update"),
    )
    wake_results = iter((True, False))
    sleeps: list[float] = []

    async def sleep_or_wake(seconds: float) -> bool:
        sleeps.append(seconds)
        try:
            return next(wake_results)
        except StopIteration:
            raise KeyboardInterrupt("bounded producer test")

    monkeypatch.setattr(engine, "_sleep_or_wake", sleep_or_wake)
    with pytest.raises(KeyboardInterrupt, match="bounded producer test"):
        asyncio.run(engine._producer_loop("owner/repo"))

    assert updates == ["update", "update", "update"]
    assert sleeps == [60, 60, 60]
    assert CloudManager("owner/repo").cloud_file_path.read_bytes() == before
    codex_send.assert_not_called()
    claude_send.assert_not_called()


def test_explicit_batch_loop_does_not_run_jules_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_jules_maintenance(monkeypatch)
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    monkeypatch.setattr(engine, "_check_and_handle_closed_branch", lambda *_: True)
    monkeypatch.setattr(
        engine,
        "_get_llm_backend_info",
        lambda: {"backend": "codex", "provider": "codex", "model": "test"},
    )
    monkeypatch.setattr(engine, "_get_candidates", lambda *_: [])
    monkeypatch.setattr(engine, "_save_report", MagicMock())
    monkeypatch.setattr("auto_coder.automation_engine.check_for_updates_and_restart", lambda: None)
    monkeypatch.setattr("auto_coder.automation_engine.git_pull", lambda: MagicMock(success=True))

    result = engine.run("owner/repo")

    assert result["issues_processed"] == []
    assert result["errors"] == []


def test_pr_notification_wakes_producer_once_without_spinning(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_jules_maintenance(monkeypatch)
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine._next_local_job_workspace_reclamation = float("inf")
    monkeypatch.setattr(engine, "_check_and_handle_closed_branch", lambda *_: True)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        engine._loop = loop
        checks: list[int] = []
        checked = asyncio.Event()

        def update() -> None:
            checks.append(1)
            loop.call_soon_threadsafe(checked.set)

        monkeypatch.setattr("auto_coder.automation_engine.check_for_updates_and_restart", update)
        producer = asyncio.create_task(engine._producer_loop("owner/repo"))
        try:
            await asyncio.wait_for(checked.wait(), 5)
            checked.clear()
            # Allow the producer to enter its real interruptible wait.
            while engine._wake_up_event is None:
                await asyncio.sleep(0.001)
            engine.notify_pr_merged_or_closed()
            await asyncio.wait_for(checked.wait(), 5)
            await asyncio.sleep(0.1)
            assert len(checks) == 2
            assert not producer.done()
        finally:
            producer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await producer

    asyncio.run(scenario())
