import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine


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
    (prompts / "legacy.md").write_text("---\ntags: [jules, recurrent]\n---\nlegacy\n", encoding="utf-8")
    (tmp_path / ".auto-coder" / "jules_session_state.json").write_text('{"accepted-session": {"state": "accepted"}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _forbid_jules_maintenance(monkeypatch)

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
