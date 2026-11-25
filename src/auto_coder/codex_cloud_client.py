"""
Codex Cloud client for Auto-Coder.

Manages asynchronous Codex Cloud task execution and lifecycle management via the Codex CLI.
Reference: https://github.com/openai/codex
"""

import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .cloud_task_client_base import CloudTask, CloudTaskClientBase, CloudTaskState
from .codex_cloud_task import canonical_codex_cloud_task_url, extract_codex_cloud_task_id, is_valid_codex_cloud_task_id
from .codex_usage_checker import codex_cloud_quota_allows_task
from .codex_wham_client import CodexWhamClient, FollowUpDeliveryOutcome
from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .logger_config import get_logger
from .pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
from .prompt_loader import render_prompt
from .utils import CommandExecutor

logger = get_logger(__name__)

_followup_state_lock = threading.Lock()


class CodexSubmissionOutcome(str, Enum):
    ACCEPTED = "accepted"
    DEFINITELY_NOT_SUBMITTED = "definitely-not-submitted"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class CodexSubmissionResult:
    """Evidence returned at the non-idempotent CLI submission boundary."""

    outcome: CodexSubmissionOutcome
    task_id: str = ""
    task_url: str = ""
    diagnostic: str = ""


class CodexReviewFeedbackReadiness(str, Enum):
    """Read-only evidence for admitting the first review of the current PR head."""

    WAITING = "waiting"
    COMPLETED = "completed"
    CURRENT_TASK_SETTLED = "current_task_settled"


@dataclass
class PendingCodexFollowUp:
    """Durable evidence needed to reconcile an ambiguous WHAM POST."""

    task_id: str = ""
    message_fingerprint: str = ""
    pre_send_turn_id: str = ""
    status: str = "indeterminate"
    logical_identity: str = ""
    accepted_at: float = 0.0
    completed_turn_id: str = ""
    repair_repository: str = ""
    repair_issue_number: int = 0
    repair_owner_incarnation: str = ""
    repair_pr_number: int = 0
    repair_head_sha: str = ""
    repair_head_branch: str = ""
    repair_base_branch: str = ""


def _codex_followup_state_path(repo_name: Optional[str]) -> Path:
    repository = repo_name or "default"
    return Path.home() / ".auto-coder" / repository / "codex_followups.json"


def _load_pending_followups(path: Path, *, required_key: Optional[str] = None) -> dict[str, PendingCodexFollowUp]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Codex follow-up state is not an object")
    if required_key in data and not isinstance(data[required_key], dict):
        raise ValueError("Exact Codex follow-up entry is not an object")
    return {key: PendingCodexFollowUp(**value) for key, value in data.items() if isinstance(key, str) and isinstance(value, dict)}


def _save_pending_followups(path: Path, records: dict[str, PendingCodexFollowUp]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({key: asdict(value) for key, value in records.items()}, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class NonDeliveryReconciliationEntry:
    """Result of evaluating one ambiguous Codex follow-up against WHAM."""

    operation_id: str
    task_id: str
    causal_baseline: str
    request_fingerprint: str
    member_count: int
    proven_not_delivered: bool
    evidence_id: str = ""
    applied: bool = False
    reason: str = ""


def reconcile_proven_non_delivered_followups(
    repository: str,
    task_id: str,
    wham: CodexWhamClient,
    *,
    apply: bool = True,
) -> list[NonDeliveryReconciliationEntry]:
    """Evaluate and optionally settle ambiguous follow-ups proven non-delivered by WHAM.

    Queries complete WHAM turn history outside the owner lock, and only settles
    operations when ``wham.prove_follow_up_not_delivered`` returns a valid
    ``wham-complete-graph-not-delivered:...`` cryptographic evidence ID and no
    native journal member for that operation is marked ``delivered``.
    """
    from .cloud_run import CloudRunRepository
    from .codex_followup_causality import NON_DELIVERY_EVIDENCE_PREFIX
    from .codex_work_accounting import CodexWorkAccounting, CodexWorkPhase, WorkAccountingStatus
    from .codex_work_reconstruction import production_codex_reconstructor
    from .implementation_slots import ImplementationOwner, ImplementationSlotRepository

    if not repository or not task_id or not callable(getattr(wham, "prove_follow_up_not_delivered", None)):
        return []
    matches = [run for run in CloudRunRepository(repository).list_all() if run.provider == "codex-cloud" and run.task_id == task_id]
    if len(matches) != 1:
        return []
    run = matches[0]
    slots = ImplementationSlotRepository(repository, 1)
    owner = ImplementationOwner("issue", run.issue_number)
    incarnation = slots.owner_incarnation(owner)
    if not incarnation:
        return []
    reconstructor = production_codex_reconstructor(repository, run.issue_number)
    accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
    if apply:
        accounting.reconcile_from_receipt(owner, incarnation, reconstructor.reconstruct())
        snapshot = accounting.snapshot(owner, incarnation)
        if snapshot.accounting_status is not WorkAccountingStatus.COMPLETE:
            return []
        operations = snapshot.operations
    else:
        snapshot = accounting.snapshot(owner, incarnation)
        operations = snapshot.operations if snapshot.accounting_status is WorkAccountingStatus.COMPLETE else reconstructor.reconstruct().operations

    candidates = [
        op for op in operations if op.kind == "follow-up" and op.task_id == task_id and op.causal_baseline and op.request_fingerprint and op.phase in {CodexWorkPhase.RESERVED, CodexWorkPhase.DELIVERY_UNKNOWN} and (op.send_provenance is None or op.send_provenance.outcome in {"reserved", "unknown"})
    ]
    if not candidates:
        return []

    state_path = _codex_followup_state_path(repository)
    results: list[NonDeliveryReconciliationEntry] = []
    for op in candidates:
        assert op.task_id is not None and op.causal_baseline is not None and op.request_fingerprint is not None
        members = tuple(sorted(item for item in op.source_request_id.split(",") if item))
        evidence_id = wham.prove_follow_up_not_delivered(op.task_id, op.causal_baseline, op.request_fingerprint)
        if not isinstance(evidence_id, str) or not evidence_id.startswith(NON_DELIVERY_EVIDENCE_PREFIX) or len(evidence_id) <= len(NON_DELIVERY_EVIDENCE_PREFIX):
            results.append(
                NonDeliveryReconciliationEntry(
                    operation_id=op.logical_operation_id,
                    task_id=op.task_id,
                    causal_baseline=op.causal_baseline,
                    request_fingerprint=op.request_fingerprint,
                    member_count=len(members),
                    proven_not_delivered=False,
                    reason="complete WHAM turn graph did not prove non-delivery",
                )
            )
            continue

        if not apply:
            with _followup_state_lock:
                records = _load_pending_followups(state_path)
            matched_keys = [key for key, record in records.items() if record.task_id == op.task_id and record.message_fingerprint == op.request_fingerprint and record.pre_send_turn_id == op.causal_baseline]
            if any(records[key].status == FollowUpDeliveryOutcome.DELIVERED.value for key in matched_keys):
                results.append(
                    NonDeliveryReconciliationEntry(
                        operation_id=op.logical_operation_id,
                        task_id=op.task_id,
                        causal_baseline=op.causal_baseline,
                        request_fingerprint=op.request_fingerprint,
                        member_count=len(members),
                        proven_not_delivered=False,
                        reason="native journal already marks a member delivered",
                    )
                )
                continue
            results.append(
                NonDeliveryReconciliationEntry(
                    operation_id=op.logical_operation_id,
                    task_id=op.task_id,
                    causal_baseline=op.causal_baseline,
                    request_fingerprint=op.request_fingerprint,
                    member_count=len(members),
                    proven_not_delivered=True,
                    evidence_id=evidence_id,
                    applied=False,
                    reason="dry-run",
                )
            )
            continue

        with slots.serialize(owner):
            with _followup_state_lock:
                records = _load_pending_followups(state_path)
                matched_keys = [key for key, record in records.items() if record.task_id == op.task_id and record.message_fingerprint == op.request_fingerprint and record.pre_send_turn_id == op.causal_baseline]
                if any(records[key].status == FollowUpDeliveryOutcome.DELIVERED.value for key in matched_keys):
                    results.append(
                        NonDeliveryReconciliationEntry(
                            operation_id=op.logical_operation_id,
                            task_id=op.task_id,
                            causal_baseline=op.causal_baseline,
                            request_fingerprint=op.request_fingerprint,
                            member_count=len(members),
                            proven_not_delivered=False,
                            reason="native journal already marks a member delivered",
                        )
                    )
                    continue
                if matched_keys:
                    for key in matched_keys:
                        records.pop(key, None)
                    _save_pending_followups(state_path, records)

            if members:
                from . import pr_processor as _pr_processor

                review_state_path = _pr_processor._cloud_review_repair_state_path(repository)
                if review_state_path.exists():
                    with _pr_processor._cloud_review_delivery_lock:
                        _pr_processor._record_review_feedback_state(review_state_path, set(), set(), remove_pending=set(members))

            accounting.settle_proven_non_delivery(
                owner,
                incarnation,
                op.logical_operation_id,
                evidence_id=evidence_id,
                source_request_id=op.source_request_id,
                causal_baseline=op.causal_baseline,
                task_id=op.task_id,
                request_fingerprint=op.request_fingerprint,
            )
            accounting.reconcile_from_receipt(owner, incarnation, reconstructor.reconstruct())

        logger.info(
            "Settled proven non-delivered Codex follow-up operation={} task={} members={} evidence={}",
            op.logical_operation_id,
            op.task_id,
            len(members),
            evidence_id,
        )
        results.append(
            NonDeliveryReconciliationEntry(
                operation_id=op.logical_operation_id,
                task_id=op.task_id,
                causal_baseline=op.causal_baseline,
                request_fingerprint=op.request_fingerprint,
                member_count=len(members),
                proven_not_delivered=True,
                evidence_id=evidence_id,
                applied=True,
                reason="settled",
            )
        )
    return results


class CodexCloudClient(CloudTaskClientBase):
    """Codex Cloud client for asynchronous cloud task execution and lifecycle management."""

    def __init__(self, backend_name: Optional[str] = None, repo_name: Optional[str] = None) -> None:
        """Initialize Codex Cloud client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
            repo_name: Repository name to resolve repository-specific overrides (optional).
        """
        super().__init__()
        self.backend_name = backend_name or "codex-cloud"
        self.repo_name = repo_name
        self.active_tasks: Dict[str, str] = {}  # task_id -> prompt
        self.task_urls: Dict[str, str] = {}  # task_id -> url
        self._warned_unsupported_models: set[str] = set()

        config = get_llm_config(repo_name=self.repo_name)
        self.quota_selection_strategy = config.quota_selection_strategy
        self.config_backend = config.get_backend_config(self.backend_name)
        self._warn_unsupported_model(self.config_backend.model if self.config_backend else None)
        self.options = (self.config_backend and self.config_backend.options) or []
        self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
        self.api_key = self.config_backend and self.config_backend.api_key
        self.base_url = self.config_backend and self.config_backend.base_url
        self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []

        # Optional environment ID for Codex Cloud executions
        self.environment_id: Optional[str] = (self.config_backend and (self.config_backend.environment_id or getattr(self.config_backend, "environment", None))) or os.environ.get("CODEX_CLOUD_ENV_ID") or os.environ.get("CODEX_ENVIRONMENT_ID")
        self.attempts = (self.config_backend and self.config_backend.attempts) or 1
        self.wham_client: Optional[CodexWhamClient] = None
        self._followup_rejection: Optional[tuple[str, tuple[str, ...]]] = None
        self._followup_duplicate_guard: Optional[tuple[str, tuple[str, ...]]] = None
        self.last_continued_at: Dict[str, float] = {}

    def _warn_unsupported_model(self, model_name: Optional[str]) -> None:
        """Warn when configuration requests unsupported Cloud model selection."""
        if model_name and model_name not in self._warned_unsupported_models:
            logger.warning(f"Codex Cloud does not support user-selected models; configured model " f"'{model_name}' for backend '{self.backend_name}' is being ignored.")
            self._warned_unsupported_models.add(model_name)

    def _extract_task_id(self, output: str) -> Optional[str]:
        """Extract a task ID or task URL from Codex Cloud CLI output.

        Task IDs typically follow the shape: `task_e_6a26c19ac8a88326af83ebfb44b89fe2`
        Task URLs use ``/remote/``; the historical ``/codex/tasks/`` and
        ``/codex/cloud/tasks/`` routes are also accepted.
        """
        if not output:
            return None

        # Check for JSON output first
        try:
            data = json.loads(output)
            if isinstance(data, dict):
                tid = data.get("task_id") or data.get("taskId") or data.get("id")
                if is_valid_codex_cloud_task_id(tid):
                    return str(tid).strip()
                url = data.get("url") or data.get("task_url")
                if url:
                    task_id = extract_codex_cloud_task_id(url)
                    if task_id:
                        return task_id
        except Exception:
            pass

        # Check for task URL in text output
        task_id = extract_codex_cloud_task_id(output)
        if task_id:
            return task_id

        # Generic task_id pattern
        generic_match = re.search(r"(?:task\s*id|task_id)[:=]\s*([a-zA-Z0-9_-]+)", output, re.IGNORECASE)
        if generic_match and is_valid_codex_cloud_task_id(generic_match.group(1)):
            return generic_match.group(1)

        return None

    def _extract_task_url(self, output: str) -> Optional[str]:
        """Extract a task URL from Codex Cloud CLI output."""
        if not output:
            return None

        url_match = re.search(r"(https?://[^\s]+/(?:remote|codex/(?:cloud/)?tasks)/[a-zA-Z0-9_-]+)", output)
        if url_match:
            return url_match.group(1)
        return None

    def start_task(
        self,
        prompt: str,
        repo_name: str = "",
        base_branch: str = "",
        title: Optional[str] = None,
        is_noedit: bool = False,
    ) -> str:
        """Start a new asynchronous cloud task on Codex Cloud.

        Executes:
            codex cloud exec [--env <ENVIRONMENT_ID>] [--branch <BRANCH>] "<PROMPT>"

        Args:
            prompt: The task prompt / instruction.
            repo_name: Repository name (optional).
            base_branch: Base branch name (optional).
            title: Optional task title (for logging).
            is_noedit: Whether this is a no-edit run.

        Returns:
            The created Task ID.
        """
        result = self.submit_task(prompt, repo_name, base_branch, title, is_noedit)
        if result.outcome is not CodexSubmissionOutcome.ACCEPTED:
            if result.outcome is CodexSubmissionOutcome.DEFINITELY_NOT_SUBMITTED:
                raise ValueError(result.diagnostic)
            raise RuntimeError(result.diagnostic)
        return result.task_id

    def submit_task(
        self,
        prompt: str,
        repo_name: str = "",
        base_branch: str = "",
        title: Optional[str] = None,
        is_noedit: bool = False,
    ) -> CodexSubmissionResult:
        """Submit once and retain evidence from both output streams."""
        from .cloud_provider_instructions import CloudTaskOperation, prepare_cloud_task

        # Composition happens before any CLI invocation: a configuration
        # error here (REQ-002) must fail closed with zero external sends.
        prepared = prepare_cloud_task(prompt, recipient="codex-cloud", operation=CloudTaskOperation.NEW_TASK, no_edit=is_noedit)

        logger.info(f"Starting Codex Cloud task (title={title or 'N/A'}, branch={base_branch or 'N/A'})")

        effective_repo = repo_name or self.repo_name
        if effective_repo:
            config = get_llm_config(repo_name=effective_repo)
            self.quota_selection_strategy = config.quota_selection_strategy
            backend_cfg = config.get_backend_config(self.backend_name)
            if backend_cfg:
                env_id = backend_cfg.environment_id or getattr(backend_cfg, "environment", None)
                if env_id:
                    self.environment_id = env_id
                self._warn_unsupported_model(backend_cfg.model)
                if backend_cfg.options:
                    self.options = backend_cfg.options
                if backend_cfg.api_key:
                    self.api_key = backend_cfg.api_key
                if backend_cfg.base_url:
                    self.base_url = backend_cfg.base_url
                if backend_cfg.attempts:
                    self.attempts = backend_cfg.attempts

        if not codex_cloud_quota_allows_task(strategy=self.quota_selection_strategy):
            raise AutoCoderUsageLimitError("Codex weekly quota is unavailable under the configured quota strategy")

        cmd = ["codex", "cloud", "exec"]

        if not self.environment_id:
            return CodexSubmissionResult(
                CodexSubmissionOutcome.DEFINITELY_NOT_SUBMITTED,
                diagnostic=f"No environment_id configured for Codex Cloud backend '{self.backend_name}'. Set environment_id in llm_config.toml or CODEX_CLOUD_ENV_ID.",
            )
        cmd.extend(["--env", self.environment_id])

        if self.attempts != 1:
            cmd.extend(["--attempts", str(self.attempts)])

        if base_branch:
            cmd.extend(["--branch", base_branch])

        if self.options:
            cmd.extend(self.options)

        cmd.append(prepared.prepared_task)

        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        logger.info(f"🤖 Running: {' '.join(cmd)}")
        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
        except (FileNotFoundError, PermissionError) as exc:
            return CodexSubmissionResult(CodexSubmissionOutcome.DEFINITELY_NOT_SUBMITTED, diagnostic=f"Codex Cloud CLI did not start: {exc}")
        except Exception as exc:
            return CodexSubmissionResult(CodexSubmissionOutcome.INDETERMINATE, diagnostic=f"Codex Cloud CLI execution became indeterminate: {exc}")
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()

        task_id = self._extract_task_id(output)
        task_url = canonical_codex_cloud_task_url(task_id)

        if task_id:
            if task_url:
                self.task_urls[task_id] = task_url
            self.active_tasks[task_id] = prompt

            from .managed_prompts import save_managed_prompt

            save_managed_prompt(effective_repo or self.repo_name, task_id, prepared)

            logger.info(f"Codex Cloud returned task: {task_id} (url={task_url or 'N/A'})")
            return CodexSubmissionResult(CodexSubmissionOutcome.ACCEPTED, task_id, task_url or "", output)

        diagnostic = f"Codex Cloud did not return a task ID: {output or 'empty output'}"
        return CodexSubmissionResult(CodexSubmissionOutcome.INDETERMINATE, diagnostic=diagnostic)

    def list_tasks(self, repo_name: Optional[str] = None) -> List[CloudTask]:
        """List active or recent Codex Cloud tasks.

        Executes:
            codex cloud list --json [--env <ENVIRONMENT_ID>]

        Args:
            repo_name: Optional repository name filter (unused directly by CLI list).

        Returns:
            List of CloudTask instances.
        """
        cmd = ["codex", "cloud", "list", "--json"]
        if self.environment_id:
            cmd.extend(["--env", self.environment_id])

        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        tasks: List[CloudTask] = []
        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
            output = (result.stdout or "").strip()
            if result.returncode == 0 and output:
                data = json.loads(output)
                task_list = data if isinstance(data, list) else data.get("tasks", [])
                for item in task_list:
                    if isinstance(item, dict):
                        tid = item.get("task_id") or item.get("id")
                        if tid:
                            raw_state = (item.get("status") or item.get("state") or "").upper()
                            # Normalize state: READY means completed/ready in Codex Cloud list
                            if raw_state in ("READY", "COMPLETED", "FINISHED", "SUCCESS"):
                                state = CloudTaskState.COMPLETED
                            elif raw_state in ("RUNNING", "IN_PROGRESS", "ACTIVE"):
                                state = CloudTaskState.RUNNING
                            elif raw_state in ("FAILED", "ERROR", "CANCELLED"):
                                state = CloudTaskState.FAILED
                            elif raw_state in ("PAUSED", "WAITING_FOR_INPUT", "IDLE", "STOPPED"):
                                state = CloudTaskState.PAUSED
                            elif raw_state in ("QUEUED", "PENDING"):
                                state = CloudTaskState.QUEUED
                            else:
                                state = CloudTaskState.UNKNOWN

                            tasks.append(
                                CloudTask(
                                    task_id=str(tid),
                                    state=state,
                                    raw_state=raw_state,
                                    title=item.get("title"),
                                    url=canonical_codex_cloud_task_url(tid),
                                    error=item.get("error"),
                                    raw_data=item,
                                )
                            )
                if tasks:
                    return tasks
        except Exception as e:
            logger.debug(f"Failed to list Codex Cloud tasks: {e}")

        # Fallback to locally tracked tasks
        for task_id, prompt in self.active_tasks.items():
            tasks.append(
                CloudTask(
                    task_id=task_id,
                    prompt=prompt,
                    state=CloudTaskState.UNKNOWN,
                    url=canonical_codex_cloud_task_url(task_id),
                )
            )
        return tasks

    def get_task(self, task_id: str) -> Optional[CloudTask]:
        """Retrieve current status of a Codex Cloud task.

        Executes:
            codex cloud status <TASK_ID>

        Args:
            task_id: The cloud task ID.

        Returns:
            CloudTask instance with normalized state.
        """
        cmd = ["codex", "cloud", "status", task_id]
        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
            output = (result.stdout or result.stderr or "").strip()

            if result.returncode == 0 and output:
                # Try parsing as JSON if JSON output is returned
                try:
                    data = json.loads(output)
                    if isinstance(data, dict):
                        raw_state = (data.get("status") or data.get("state") or "").upper()
                        if raw_state in ("READY", "COMPLETED", "FINISHED", "SUCCESS"):
                            state = CloudTaskState.COMPLETED
                        elif raw_state in ("RUNNING", "IN_PROGRESS", "ACTIVE"):
                            state = CloudTaskState.RUNNING
                        elif raw_state in ("FAILED", "ERROR", "CANCELLED"):
                            state = CloudTaskState.FAILED
                        elif raw_state in ("PAUSED", "WAITING_FOR_INPUT", "IDLE", "STOPPED"):
                            state = CloudTaskState.PAUSED
                        elif raw_state in ("QUEUED", "PENDING"):
                            state = CloudTaskState.QUEUED
                        else:
                            state = CloudTaskState.UNKNOWN

                        return CloudTask(
                            task_id=task_id,
                            state=state,
                            raw_state=raw_state,
                            title=data.get("title"),
                            url=canonical_codex_cloud_task_url(task_id),
                            error=data.get("error"),
                            raw_data=data,
                        )
                except json.JSONDecodeError:
                    pass

                # Parse plain text output
                raw_upper = output.upper()
                if "READY" in raw_upper or "COMPLETED" in raw_upper or "SUCCESS" in raw_upper:
                    state = CloudTaskState.COMPLETED
                elif "RUNNING" in raw_upper or "IN_PROGRESS" in raw_upper:
                    state = CloudTaskState.RUNNING
                elif "FAILED" in raw_upper or "ERROR" in raw_upper:
                    state = CloudTaskState.FAILED
                elif "PAUSED" in raw_upper or "WAITING" in raw_upper:
                    state = CloudTaskState.PAUSED
                else:
                    state = CloudTaskState.UNKNOWN

                raw_data = None
                if state in {CloudTaskState.COMPLETED, CloudTaskState.FAILED, CloudTaskState.PAUSED}:
                    latest_turn_id = (self.wham_client or CodexWhamClient()).resolve_latest_assistant_turn(task_id)
                    if latest_turn_id:
                        raw_data = {"latest_turn_id": latest_turn_id}

                return CloudTask(
                    task_id=task_id,
                    state=state,
                    raw_state=output,
                    prompt=self.active_tasks.get(task_id),
                    url=canonical_codex_cloud_task_url(task_id),
                    raw_data=raw_data,
                )
        except Exception as e:
            logger.debug(f"Failed to check Codex Cloud task status for {task_id}: {e}")

        if task_id in self.active_tasks:
            return CloudTask(
                task_id=task_id,
                prompt=self.active_tasks[task_id],
                state=CloudTaskState.UNKNOWN,
                url=canonical_codex_cloud_task_url(task_id),
            )

        return None

    def get_diff(self, task_id: str) -> str:
        """Inspect the changes/diff produced by a Codex Cloud task.

        Executes:
            codex cloud diff <TASK_ID>

        Args:
            task_id: The cloud task ID.

        Returns:
            Diff output string.
        """
        cmd = ["codex", "cloud", "diff", task_id]
        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
            return (result.stdout or "").strip()
        except Exception as e:
            logger.warning(f"Failed to get diff for Codex Cloud task {task_id}: {e}")
            return ""

    def apply_changes(self, task_id: str) -> bool:
        """Apply changes from a Codex Cloud task locally.

        Executes:
            codex cloud apply <TASK_ID>

        Args:
            task_id: The cloud task ID.

        Returns:
            True if applied successfully, False otherwise.
        """
        cmd = ["codex", "cloud", "apply", task_id]
        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"Failed to apply changes for Codex Cloud task {task_id}: {e}")
            return False

    def continue_if_paused(self, task_id: str, prompt: Optional[str] = None, force: bool = False, *, repair_target: Optional[ExistingPrRepairTarget] = None) -> bool:
        """Attempt to continue a paused or waiting Codex Cloud task via WHAM follow-up.

        Resolves the latest usable assistant turn ID and sends a continuation prompt
        using the internal WHAM tasks backend API.

        Args:
            task_id: The Codex Cloud task ID.
            prompt: Optional custom continuation prompt. If None, renders the default
                    prompt from prompts.yaml (codex_cloud.continuation).
            force: If True, bypass cooldown and duplicate/indeterminate checks.

        Returns:
            True if the continuation request was accepted, False otherwise.
        """
        if not is_valid_codex_cloud_task_id(task_id):
            logger.warning("continue_if_paused called with invalid task_id")
            return False

        # Anti-tight-loop cooldown (60 seconds)
        now = time.time()
        last_time = self.last_continued_at.get(task_id, 0.0)
        if not force and now - last_time < 60.0:
            logger.info(f"Codex Cloud task '{task_id}' was continued {int(now - last_time)}s ago; skipping to avoid tight loops")
            return False

        if not prompt:
            continuation_prompt = render_prompt("codex_cloud.continuation")
        else:
            continuation_prompt = prompt

        sent = self.send_followup(task_id, continuation_prompt, force=force, repair_target=repair_target) if repair_target is not None else self.send_followup(task_id, continuation_prompt, force=force)
        if sent:
            self.last_continued_at[task_id] = now
            logger.info(f"Successfully sent continuation follow-up to Codex Cloud task '{task_id}'")
            return True

        logger.warning(f"Failed to send continuation follow-up to Codex Cloud task '{task_id}'")
        return False

    def get_followup_delivery(self, task_id: str, logical_identity: str) -> FollowUpDeliveryOutcome:
        """Return durable delivery state, reconciling an ambiguous record."""
        state_path = _codex_followup_state_path(self.repo_name)
        key = hashlib.sha256(f"{task_id}\0{logical_identity}".encode("utf-8")).hexdigest()
        with _followup_state_lock:
            try:
                records = _load_pending_followups(state_path, required_key=key)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(f"Cannot read Codex follow-up delivery state; failing closed: {exc}")
                return FollowUpDeliveryOutcome.INDETERMINATE
            record = records.get(key)
        if record is None:
            return FollowUpDeliveryOutcome.NOT_DELIVERED
        if record.status == FollowUpDeliveryOutcome.DELIVERED.value:
            return FollowUpDeliveryOutcome.DELIVERED

        wham = self.wham_client or CodexWhamClient()
        if wham.reconcile_follow_up(task_id, record.pre_send_turn_id, record.message_fingerprint) is not True:
            return FollowUpDeliveryOutcome.INDETERMINATE
        record.status = FollowUpDeliveryOutcome.DELIVERED.value
        record.accepted_at = record.accepted_at or time.time()
        with _followup_state_lock:
            records = _load_pending_followups(state_path, required_key=key)
            records[key] = record
            _save_pending_followups(state_path, records)
        logger.info(f"Reconciled previously ambiguous Codex follow-up '{logical_identity}' as delivered")
        return FollowUpDeliveryOutcome.DELIVERED

    def observe_review_feedback_readiness(self, task_id: str, logical_identities: tuple[str, ...], repair_target: ExistingPrRepairTarget) -> CodexReviewFeedbackReadiness:
        """Observe readiness without completing historical requests or changing receipts.

        Retained baseline evidence can prove ordinary feedback completed. When
        the provider has omitted a historical baseline, separately observe a
        settled current task for first-current-head review admission only. The
        caller owns current-head, review-count, closure and correction gates.
        """
        if (
            not is_valid_codex_cloud_task_id(task_id)
            or not logical_identities
            or any(not isinstance(identity, str) or not identity for identity in logical_identities)
            or len(set(logical_identities)) != len(logical_identities)
            or self.repo_name != repair_target.repo_name
            or type(repair_target.pr_number) is not int
            or repair_target.pr_number <= 0
            or not all(isinstance(value, str) and value for value in (repair_target.repo_name, repair_target.head_branch, repair_target.base_branch, repair_target.head_sha))
        ):
            return CodexReviewFeedbackReadiness.WAITING
        state_path = _codex_followup_state_path(self.repo_name)
        selected_keys = {hashlib.sha256(f"{task_id}\0{identity}".encode("utf-8")).hexdigest(): identity for identity in logical_identities}
        turn_pattern = re.compile(rf"{re.escape(task_id)}~[A-Za-z0-9_-]+")

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            values: dict[str, object] = {}
            for key, value in pairs:
                if key in values:
                    raise ValueError("Duplicate native follow-up field")
                values[key] = value
            return values

        def read_owned_receipts() -> dict[str, PendingCodexFollowUp]:
            with _followup_state_lock:
                document = json.loads(state_path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
            if not isinstance(document, dict):
                raise ValueError("Native follow-up journal is not an object")
            owned: dict[str, PendingCodexFollowUp] = {}
            for key, value in document.items():
                if not isinstance(value, dict) or not isinstance(value.get("task_id"), str) or not value["task_id"]:
                    raise ValueError("Native follow-up ownership is unavailable")
                if value["task_id"] != task_id:
                    if key in selected_keys:
                        raise ValueError("Selected follow-up has a different owner")
                    continue
                record = PendingCodexFollowUp(**value)
                if not isinstance(record.logical_identity, str) or not record.logical_identity:
                    raise ValueError("Native follow-up identity is unavailable")
                expected_key = hashlib.sha256(f"{task_id}\0{record.logical_identity}".encode("utf-8")).hexdigest()
                if key != expected_key or record.status != FollowUpDeliveryOutcome.DELIVERED.value:
                    raise ValueError("Native follow-up delivery is ambiguous")
                if not isinstance(record.pre_send_turn_id, str) or not turn_pattern.fullmatch(record.pre_send_turn_id):
                    raise ValueError("Native follow-up baseline is unavailable")
                owned[key] = record
            return owned

        try:
            initial = read_owned_receipts()
            baselines: set[str] = set()
            for key, identity in selected_keys.items():
                record = initial.get(key)
                if (
                    record is None
                    or record.logical_identity != identity
                    or record.repair_repository != repair_target.repo_name
                    or type(record.repair_pr_number) is not int
                    or record.repair_pr_number != repair_target.pr_number
                    or record.repair_head_branch != repair_target.head_branch
                    or record.repair_base_branch != repair_target.base_branch
                    or not isinstance(record.repair_head_sha, str)
                    or not record.repair_head_sha
                ):
                    return CodexReviewFeedbackReadiness.WAITING
                baselines.add(record.pre_send_turn_id)
            task = self.get_task(task_id)
            if not isinstance(task, CloudTask) or task.task_id != task_id or task.state is not CloudTaskState.COMPLETED:
                return CodexReviewFeedbackReadiness.WAITING
            wham = self.wham_client or CodexWhamClient()
            unresolved_baselines = set()
            for baseline in sorted(baselines):
                completed = wham.resolve_completed_assistant_turn_after(task_id, baseline)
                if not isinstance(completed, str) or not turn_pattern.fullmatch(completed) or completed == baseline:
                    unresolved_baselines.add(baseline)
            readiness = CodexReviewFeedbackReadiness.COMPLETED
            if unresolved_baselines:
                from .codex_followup_causality import settled_current_task
                from .codex_wham_client import _turn_created_at_key

                current = wham.get_task(task_id)
                if current is None:
                    return CodexReviewFeedbackReadiness.WAITING
                turns = wham.get_task_turns(task_id, require_complete=True)
                # A present baseline whose work has not completed remains a
                # wait. Recovery concerns omitted historical evidence only.
                if unresolved_baselines.intersection(turn.id for turn in turns):
                    return CodexReviewFeedbackReadiness.WAITING
                assistant = settled_current_task(task_id, current, turns)
                created_at = _turn_created_at_key(assistant) if assistant is not None else None
                if created_at is None or not math.isfinite(created_at):
                    return CodexReviewFeedbackReadiness.WAITING
                for key in selected_keys:
                    record = initial[key]
                    if record.pre_send_turn_id not in unresolved_baselines:
                        continue
                    # This timestamp is an ordering guard, not proof that the
                    # omitted request ran. A stale pre-send task cannot qualify.
                    accepted_at = record.accepted_at
                    if type(accepted_at) not in (int, float) or not math.isfinite(accepted_at) or accepted_at <= 0 or accepted_at >= created_at:
                        return CodexReviewFeedbackReadiness.WAITING
                if settled_current_task(task_id, wham.get_task(task_id), turns) != assistant:
                    return CodexReviewFeedbackReadiness.WAITING
                readiness = CodexReviewFeedbackReadiness.CURRENT_TASK_SETTLED
            # Another task may update this shared journal while provider reads
            # run. Only changes to this owning task invalidate its observation.
            return readiness if read_owned_receipts() == initial else CodexReviewFeedbackReadiness.WAITING
        except Exception as exc:
            logger.warning("Completed review feedback could not be established for task {}: {}", task_id, type(exc).__name__)
            return CodexReviewFeedbackReadiness.WAITING

    def get_followup_remediation_baseline(self, task_id: str, logical_identities: tuple[str, ...]) -> str:
        """Return the pre-send assistant turn durably captured for a follow-up."""
        state_path = _codex_followup_state_path(self.repo_name)
        keys = [hashlib.sha256(f"{task_id}\0{identity}".encode("utf-8")).hexdigest() for identity in logical_identities]
        with _followup_state_lock:
            try:
                records = _load_pending_followups(state_path)
            except (OSError, ValueError, TypeError):
                return ""
        turns = {records[key].pre_send_turn_id for key in keys if key in records and records[key].pre_send_turn_id}
        return f"latest_turn_id:{turns.pop()}" if len(turns) == 1 else ""

    def observe_completed_followup_remediation_turns(self, task_id: str) -> dict[str, str]:
        """Persist and return each finding's current completed remediation turn."""
        state_path = _codex_followup_state_path(self.repo_name)
        with _followup_state_lock:
            records = _load_pending_followups(state_path)
        latest_by_feedback: dict[str, PendingCodexFollowUp] = {}
        for record in records.values():
            if record.task_id != task_id or record.status != FollowUpDeliveryOutcome.DELIVERED.value:
                continue
            feedback_identity, separator, _generation = record.logical_identity.partition(":remediation:")
            if not separator:
                continue
            current = latest_by_feedback.get(feedback_identity)
            if current is None or record.accepted_at > current.accepted_at:
                latest_by_feedback[feedback_identity] = record
        changed = False
        wham = self.wham_client or CodexWhamClient()
        for latest in latest_by_feedback.values():
            if latest.completed_turn_id:
                continue
            completed_turn = wham.resolve_completed_assistant_turn_after(task_id, latest.pre_send_turn_id)
            if completed_turn:
                latest.completed_turn_id = completed_turn
                changed = True
        if changed:
            with _followup_state_lock:
                current_records = _load_pending_followups(state_path)
                selected = {(record.task_id, record.logical_identity): record.completed_turn_id for record in latest_by_feedback.values() if record.completed_turn_id}
                for record in current_records.values():
                    completed_turn = selected.get((record.task_id, record.logical_identity))
                    if completed_turn and not record.completed_turn_id:
                        record.completed_turn_id = completed_turn
                _save_pending_followups(state_path, current_records)
        return {feedback_identity: record.completed_turn_id for feedback_identity, record in latest_by_feedback.items()}

    def get_completed_followup_remediation_turn(self, task_id: str, feedback_identity: str) -> str:
        """Read the completed generation observed before validation acceptance.

        The durable follow-up record supplies the causal pre-send baseline. WHAM
        supplies the production assistant-turn identity. Observation is deliberately
        separate so replaying an already accepted validation cannot discover newer
        activity and associate it retroactively.
        """
        state_path = _codex_followup_state_path(self.repo_name)
        with _followup_state_lock:
            try:
                records = _load_pending_followups(state_path)
            except (OSError, ValueError, TypeError):
                return ""
        prefix = f"{feedback_identity}:remediation:"
        accepted = [record for record in records.values() if record.task_id == task_id and record.status == FollowUpDeliveryOutcome.DELIVERED.value and record.logical_identity.startswith(prefix)]
        if not accepted:
            return ""
        latest = max(accepted, key=lambda record: record.accepted_at)
        return latest.completed_turn_id

    def send_existing_pr_followup(self, task_id: str, message: str, target: ExistingPrRepairTarget, logical_identities: tuple[str, ...] = (), *, final_authority_check: Optional[Callable[[], bool]] = None, strong_source: Optional[str] = None, strong_revision: Optional[str] = None) -> bool:
        """Carry the existing-PR producer contract into the durable sender journal."""
        if final_authority_check is not None:
            return self.send_followup(task_id, message, logical_identities, repair_target=target, final_authority_check=final_authority_check, strong_source=strong_source, strong_revision=strong_revision)
        return self.send_followup(task_id, message, logical_identities, repair_target=target)

    def consume_followup_duplicate_guard(self, task_id: str, logical_identities: tuple[str, ...]) -> bool:
        """Consume a local no-POST outcome separately from remote rejection."""
        guarded = self._followup_duplicate_guard
        self._followup_duplicate_guard = None
        return guarded == (task_id, tuple(sorted(logical_identities)))

    def consume_followup_rejection(self, task_id: str, logical_identities: tuple[str, ...]) -> bool:
        """Consume positive rejection evidence from this client's latest send only.

        Journal absence is never evidence for releasing a caller's reservation.
        """
        rejection = self._followup_rejection
        self._followup_rejection = None
        return rejection == (task_id, tuple(sorted(logical_identities)))

    def send_followup(
        self, task_id: str, message: str, logical_identities: tuple[str, ...] = (), force: bool = False, *, repair_target: Optional[ExistingPrRepairTarget] = None, final_authority_check: Optional[Callable[[], bool]] = None, strong_source: Optional[str] = None, strong_revision: Optional[str] = None
    ) -> bool:
        """Send work once, reconciling any prior ambiguous POST before retrying."""
        self._followup_rejection = None
        self._followup_duplicate_guard = None
        if not is_valid_codex_cloud_task_id(task_id) or not message:
            logger.warning("Codex Cloud follow-up requires a task ID and message")
            return False

        from .cloud_provider_instructions import CloudTaskOperation, prepare_cloud_task

        if repair_target is not None and (
            repair_target.repo_name != self.repo_name
            or type(repair_target.pr_number) is not int
            or repair_target.pr_number <= 0
            or not isinstance(repair_target.head_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", repair_target.head_sha) is None
            or not repair_target.head_branch
            or not repair_target.base_branch
            or not message.startswith(build_existing_pr_repair_prompt(repair_target, "").rstrip())
        ):
            logger.warning("Existing-PR follow-up does not match its exact producer contract; not sent")
            return False

        # An existing-session continuation is never eligible for the initial
        # component (REQ-007); routed through the same boundary for parity
        # with new-task dispatch rather than skipping it implicitly.
        message = prepare_cloud_task(message, recipient="codex-cloud", operation=CloudTaskOperation.CONTINUATION, no_edit=False).prepared_task

        wham = self.wham_client or CodexWhamClient()
        message_fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()
        identities = logical_identities or (message_fingerprint,)
        keys = {hashlib.sha256(f"{task_id}\0{identity}".encode("utf-8")).hexdigest() for identity in identities}
        state_path = _codex_followup_state_path(self.repo_name)
        states = [self.get_followup_delivery(task_id, identity) for identity in identities]
        if not force:
            if states and all(state is FollowUpDeliveryOutcome.DELIVERED for state in states):
                return True
            if any(state is not FollowUpDeliveryOutcome.NOT_DELIVERED for state in states):
                self._followup_duplicate_guard = (task_id, tuple(sorted(identities)))
                logger.warning("Codex follow-up contains an indeterminate or partially delivered logical identity; duplicate POST deferred")
                return False

        turn_id = wham.resolve_latest_assistant_turn(task_id)
        if not turn_id:
            logger.warning(f"Codex Cloud task '{task_id}' has no usable assistant turn for follow-up")
            return False

        # Resolve scope from durable task ownership rather than backend alias
        # spelling or a caller-provided eligibility flag. Registration occurs
        # before the follow-up journal can retain responsibility or WHAM can
        # observe a POST.
        from .codex_work_accounting import CodexSendProvenance
        from .codex_work_fence import CodexWorkAuthorityRefused, CodexWorkOutsideScope, CodexWorkOverlap, production_codex_fence

        source_request_id = ",".join(sorted(identities))
        try:
            fence, work_identity = production_codex_fence(
                self.repo_name or "",
                task_id,
                "follow-up",
                source_request_id,
                turn_id,
            )
        except CodexWorkOutsideScope:
            from .strong_correction_scope import has_registered_task

            try:
                adopted = has_registered_task(self.repo_name or "", task_id)
            except Exception:
                adopted = True
            if adopted:
                logger.warning("Registered Strong task has missing owner evidence; no untracked fallback send")
                return False
            fence = None
            work_identity = None
        except Exception as exc:
            logger.warning(f"Cannot establish Codex retirement fence; request was not sent: {exc}")
            return False

        if fence is not None and work_identity is not None:
            try:
                reconcile_proven_non_delivered_followups(self.repo_name or "", task_id, wham, apply=True)
            except Exception as exc:
                logger.warning(f"Could not reconcile ambiguous Codex follow-ups for task '{task_id}': {exc}")

        def send_registered_followup():
            repair_binding = {}
            if repair_target is not None and fence is not None and work_identity is not None:
                from .implementation_slots import ImplementationOwner, ImplementationSlotSnapshot

                snapshot = fence.accounting.slots.snapshot()
                bound = next((item for item in snapshot.owners if item.owner == ImplementationOwner("issue", work_identity.issue_number)), None) if isinstance(snapshot, ImplementationSlotSnapshot) else None
                if bound is None or bound.incarnation != work_identity.incarnation or repair_target.pr_number not in bound.implementation_prs:
                    raise ValueError("Existing-PR repair target is not bound to this current reservation")
                repair_binding = {
                    "repair_repository": work_identity.repository,
                    "repair_issue_number": work_identity.issue_number,
                    "repair_owner_incarnation": work_identity.incarnation,
                    "repair_pr_number": repair_target.pr_number,
                    "repair_head_sha": repair_target.head_sha,
                    "repair_head_branch": repair_target.head_branch,
                    "repair_base_branch": repair_target.base_branch,
                }
            with _followup_state_lock:
                pending = _load_pending_followups(state_path)
                for identity in identities:
                    key = hashlib.sha256(f"{task_id}\0{identity}".encode("utf-8")).hexdigest()
                    pending[key] = PendingCodexFollowUp(
                        task_id=task_id,
                        message_fingerprint=message_fingerprint,
                        pre_send_turn_id=turn_id,
                        logical_identity=identity,
                        **repair_binding,
                    )
                _save_pending_followups(state_path, pending)
            return wham.send_follow_up(task_id=task_id, turn_id=turn_id, prompt=message)

        try:
            if final_authority_check is not None and (fence is None or work_identity is None):
                raise CodexWorkAuthorityRefused("Scoped final authority requires the existing tracked owner fence")
            provenance = CodexSendProvenance(
                message_fingerprint,
                tuple(sorted(identities)),
                repair_target.pr_number if repair_target is not None else None,
                repair_target.head_sha if repair_target is not None else None,
                repair_target.head_branch if repair_target is not None else None,
                repair_target.base_branch if repair_target is not None else None,
                strong_source=strong_source,
                strong_revision=strong_revision,
            )
            if fence is not None and work_identity is not None:
                if final_authority_check is not None:
                    result = fence.execute(work_identity, send_registered_followup, force=force, send_provenance=provenance, final_authority_check=final_authority_check)
                else:
                    result = fence.execute(work_identity, send_registered_followup, force=force, send_provenance=provenance)
            else:
                result = send_registered_followup()
        except CodexWorkOverlap as exc:
            self._followup_duplicate_guard = (task_id, tuple(sorted(identities)))
            logger.warning(f"Codex follow-up held by local wildcard overlap guard; no provider request: {exc}")
            return False
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(f"Cannot durably reserve Codex follow-up; request was not sent: {exc}")
            return False
        with _followup_state_lock:
            pending = _load_pending_followups(state_path)
            if result.delivered:
                accepted_at = time.time()
                for identity in identities:
                    key = hashlib.sha256(f"{task_id}\0{identity}".encode("utf-8")).hexdigest()
                    pending[key] = PendingCodexFollowUp(
                        task_id=task_id,
                        message_fingerprint=message_fingerprint,
                        pre_send_turn_id=turn_id,
                        status=FollowUpDeliveryOutcome.DELIVERED.value,
                        logical_identity=identity,
                        accepted_at=accepted_at,
                        repair_repository=pending[key].repair_repository,
                        repair_issue_number=pending[key].repair_issue_number,
                        repair_owner_incarnation=pending[key].repair_owner_incarnation,
                        repair_pr_number=pending[key].repair_pr_number,
                        repair_head_sha=pending[key].repair_head_sha,
                        repair_head_branch=pending[key].repair_head_branch,
                        repair_base_branch=pending[key].repair_base_branch,
                    )
            elif result.outcome is FollowUpDeliveryOutcome.NOT_DELIVERED:
                self._followup_rejection = (task_id, tuple(sorted(identities)))
                for key in keys:
                    pending.pop(key, None)
            _save_pending_followups(state_path, pending)
        if fence is not None and work_identity is not None:
            from .implementation_slots import ImplementationOwner

            # Preserve native certainty before later lifecycle accounting can
            # raise. This does not settle execution/publication responsibility.
            fence.accounting.record_send_outcome(
                ImplementationOwner("issue", work_identity.issue_number),
                work_identity.incarnation,
                work_identity.operation_id,
                source_request_id=work_identity.source_request_id,
                causal_baseline=work_identity.causal_baseline,
                task_id=work_identity.task_id,
                outcome="accepted" if result.delivered else "not_delivered" if result.outcome is FollowUpDeliveryOutcome.NOT_DELIVERED else "unknown",
            )
            fence.record_delivery(
                work_identity,
                accepted=result.delivered,
                indeterminate=result.outcome is FollowUpDeliveryOutcome.INDETERMINATE,
                evidence_id=f"wham:{result.status_code or result.outcome.value}",
            )
        if result.delivered:
            self.active_tasks[task_id] = message
            logger.info(f"Assigned follow-up work to Codex Cloud task '{task_id}'")
            return True
        if result.outcome is FollowUpDeliveryOutcome.INDETERMINATE:
            logger.warning(f"Codex follow-up delivery for task '{task_id}' is indeterminate; automatic retry is deferred")
        return False

    def stop_task(self, task_id: str) -> bool:
        """Stop or clean up a Codex Cloud task tracking."""
        if task_id in self.active_tasks:
            del self.active_tasks[task_id]
            return True
        return False

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Execute LLM with prompt via Codex Cloud."""
        task_id = self.start_task(prompt, is_noedit=is_noedit)
        return f"Codex Cloud task started successfully. Task ID: {task_id}"

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if MCP server is configured."""
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration."""
        return False
