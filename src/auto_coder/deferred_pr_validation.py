"""Durable proposals awaiting authoritative PR-validation acceptance.

A retained proposal is never acceptance, publication, or merge authority. The
controller must reconfirm its original attempt and every current input before
resuming the existing acceptance path.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from .ordinary_closure_evidence import deserialize_ordinary_result, serialize_ordinary_result
from .reviewer_session_registry import RecoveredFileEvidence, ReviewerSession, TestOracleGap
from .runtime_locks import ensure_lock_directory, lock_path

if TYPE_CHECKING:
    from .adversarial_validator import AdversarialValidationResult
    from .automation_config import AutomationConfig
    from .llm_backend_config import LLMBackendConfiguration


class DeferredPRValidationStateError(RuntimeError):
    """A retained proposal is unavailable or inconsistent; no effect is allowed."""


@dataclass(frozen=True)
class DeferredPRValidation:
    """One immutable semantic result and its original controller-owned binding."""

    repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""
    attempt_id: str = ""
    attempt_sequence: int = 0
    review_id: str = ""
    policy_identity: str = ""
    thread_snapshot: str = ""
    validation_snapshot: str = ""
    context_identity: str = ""
    input_snapshot: str = ""
    result_payload: str = ""
    reviewer_session_payload: str = ""
    source_response_id: str = ""
    retry_not_before: float = 0.0
    reason: str = ""

    @classmethod
    def from_result(
        cls,
        *,
        repository: str,
        pr_number: int,
        head_sha: str,
        base_sha: str,
        attempt_id: str,
        attempt_sequence: int,
        review_id: str,
        policy_identity: str,
        thread_snapshot: str,
        validation_snapshot: str,
        context_identity: str,
        result: AdversarialValidationResult,
        input_snapshot: str = "",
        retry_not_before: float = 0.0,
        reason: str = "",
    ) -> DeferredPRValidation:
        """Capture normalized output without any accepted projection or registry."""
        checkpoint = result.reviewer_session_checkpoint
        record = cls(
            repository=repository,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            attempt_id=attempt_id,
            attempt_sequence=attempt_sequence,
            review_id=review_id,
            policy_identity=policy_identity,
            thread_snapshot=thread_snapshot,
            validation_snapshot=validation_snapshot,
            context_identity=context_identity,
            input_snapshot=input_snapshot,
            result_payload=serialize_ordinary_result(result, include_closure=True),
            reviewer_session_payload=json.dumps(asdict(checkpoint), sort_keys=True) if checkpoint is not None else "",
            source_response_id=result.source_response_id,
            retry_not_before=retry_not_before,
            reason=reason,
        )
        record.validate()
        return record

    def restore_result(self) -> AdversarialValidationResult:
        """Restore the original proposal; owning stores supply current authority."""
        try:
            raw = json.loads(self.result_payload)
            if not isinstance(raw, dict):
                raise ValueError("normalized result is not an object")
            if any(name in raw for name in ("reviewer_session_checkpoint", "reviewer_session_registry", "accepted_finding_projection")):
                raise ValueError("normalized result contains process-local authority")
            if self.reviewer_session_payload:
                checkpoint = json.loads(self.reviewer_session_payload)
                if not isinstance(checkpoint, dict):
                    raise ValueError("reviewer checkpoint is not an object")
                raw["reviewer_session_checkpoint"] = checkpoint
            raw["source_response_id"] = self.source_response_id
            raw["review_input_context_identity"] = self.input_snapshot
            result = deserialize_ordinary_result(json.dumps(raw))
            if (result.attempt_id, result.attempt_sequence) != (self.attempt_id, self.attempt_sequence):
                raise ValueError("normalized result names a different producing attempt")
            checkpoint = result.reviewer_session_checkpoint
            if checkpoint is not None:
                if not isinstance(checkpoint, ReviewerSession):
                    raise ValueError("reviewer checkpoint could not be reconstructed")
                if (checkpoint.repository, checkpoint.pr_number, checkpoint.evidence_head_sha) != (self.repository, self.pr_number, self.head_sha):
                    raise ValueError("reviewer checkpoint names a different target")
                if checkpoint.evidence_validation_snapshot != self.validation_snapshot:
                    raise ValueError("reviewer checkpoint names a different validation snapshot")
                if not isinstance(checkpoint.test_oracle_gaps, list) or any(not isinstance(item, TestOracleGap) for item in checkpoint.test_oracle_gaps):
                    raise ValueError("reviewer checkpoint has invalid test-oracle gaps")
                if not isinstance(checkpoint.recovered_file_evidence, list) or any(not isinstance(item, RecoveredFileEvidence) for item in checkpoint.recovered_file_evidence):
                    raise ValueError("reviewer checkpoint has invalid recovered evidence")
            return result
        except DeferredPRValidationStateError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise DeferredPRValidationStateError(f"Retained PR-validation result is invalid: {exc}") from exc

    def validate(self) -> None:
        """Reject incomplete identities and malformed proposals before they are used."""
        required_strings = ("repository", "head_sha", "base_sha", "attempt_id", "policy_identity", "thread_snapshot", "validation_snapshot", "context_identity", "result_payload")
        if any(not isinstance(getattr(self, name), str) or not getattr(self, name) for name in required_strings):
            raise DeferredPRValidationStateError("Retained PR-validation binding is incomplete")
        if any(not isinstance(getattr(self, name), str) for name in ("review_id", "reviewer_session_payload", "source_response_id", "input_snapshot", "reason")):
            raise DeferredPRValidationStateError("Retained PR-validation metadata is invalid")
        if type(self.pr_number) is not int or self.pr_number <= 0 or type(self.attempt_sequence) is not int or self.attempt_sequence <= 0:
            raise DeferredPRValidationStateError("Retained PR-validation attempt identity is invalid")
        if type(self.retry_not_before) not in (int, float) or not math.isfinite(self.retry_not_before) or self.retry_not_before < 0:
            raise DeferredPRValidationStateError("Retained PR-validation retry time is invalid")
        self.restore_result()

    def semantic_identity(self) -> DeferredPRValidation:
        """Exclude mutable scheduling diagnostics from the immutable proposal."""
        return replace(self, retry_not_before=0.0, reason="")


class DeferredPRValidationStore:
    """Retain proposals by original attempt with atomic, process-safe updates."""

    def __init__(self, repo_name: str, storage_path: Path | None = None):
        self.repo_name = repo_name
        self.storage_path = storage_path or Path.home() / ".auto-coder" / repo_name / "deferred_pr_validation.json"
        self.lock_path = lock_path(repo_name, self.storage_path, "deferred-pr-validation-store")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_lock_directory(self.lock_path)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, DeferredPRValidation]:
        if not self.storage_path.exists():
            return {}
        with self.storage_path.open(encoding="utf-8") as stream:
            state = json.load(stream)
        if not isinstance(state, dict) or state.get("version") != 1 or state.get("repository") != self.repo_name or not isinstance(state.get("records"), dict):
            raise DeferredPRValidationStateError("Retained PR-validation store has invalid ownership or schema")
        records = {}
        expected = {item.name for item in fields(DeferredPRValidation)}
        for attempt_id, raw in state["records"].items():
            if not isinstance(attempt_id, str) or not isinstance(raw, dict) or set(raw) != expected:
                raise DeferredPRValidationStateError("Retained PR-validation record has an invalid schema")
            record = DeferredPRValidation(**raw)
            record.validate()
            if record.repository != self.repo_name or record.attempt_id != attempt_id:
                raise DeferredPRValidationStateError("Retained PR-validation record has inconsistent ownership")
            records[attempt_id] = record
        return records

    def _write(self, records: dict[str, DeferredPRValidation]) -> None:
        temporary = self.storage_path.with_suffix(f".tmp.{os.getpid()}.{time.monotonic_ns()}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "repository": self.repo_name, "records": {key: asdict(value) for key, value in records.items()}}, stream, sort_keys=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.storage_path)
            directory = os.open(self.storage_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _transaction(self) -> Iterator[dict[str, DeferredPRValidation]]:
        try:
            with self._locked():
                yield self._read()
        except DeferredPRValidationStateError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise DeferredPRValidationStateError(f"Retained PR-validation state is unavailable: {exc}") from exc

    def retain(self, record: DeferredPRValidation) -> DeferredPRValidation:
        """Persist the first semantic proposal; replay never replaces its output."""
        record.validate()
        if record.repository != self.repo_name:
            raise DeferredPRValidationStateError("Cannot retain another repository's PR-validation result")
        with self._transaction() as records:
            previous = records.get(record.attempt_id)
            if previous is not None:
                if previous.semantic_identity() != record.semantic_identity():
                    raise DeferredPRValidationStateError("Conflicting PR-validation result for the same producing attempt")
                return previous
            records[record.attempt_id] = record
            self._write(records)
            return record

    def load(self, pr_number: int) -> DeferredPRValidation | None:
        """Return the newest retained proposal, never interpreting it as acceptance."""
        with self._transaction() as records:
            candidates = [record for record in records.values() if record.pr_number == pr_number]
            return max(candidates, key=lambda record: record.attempt_sequence, default=None)

    def defer(self, attempt_id: str, reason: str, retry_not_before: float) -> DeferredPRValidation:
        """Update only retry metadata, preserving the original semantic bytes."""
        with self._transaction() as records:
            record = records.get(attempt_id)
            if record is None:
                raise DeferredPRValidationStateError("Cannot defer an unknown PR-validation attempt")
            updated = replace(record, reason=reason, retry_not_before=max(record.retry_not_before, retry_not_before))
            updated.validate()
            records[attempt_id] = updated
            self._write(records)
            return updated

    def remove(self, attempt_id: str) -> bool:
        """Retire exactly one proposal after acceptance handoff or supersession."""
        with self._transaction() as records:
            if attempt_id not in records:
                return False
            del records[attempt_id]
            self._write(records)
            return True


def deferred_validation_policy_identity(
    config: AutomationConfig,
    thread_gate_enabled: bool,
    *,
    repo_name: str | None = None,
    llm_config: LLMBackendConfiguration | None = None,
) -> str:
    """Fingerprint configured ordinary-review policy without reading live capacity.

    Candidate precedence is shared with production routing. Credentials, usage
    markers, quota observations, provider sessions, and selection caches are not
    review policy and cannot invalidate an already completed semantic result.
    """
    from .adversarial_validator import ADVERSARIAL_VALIDATION_CACHE_VERSION
    from .cli_helpers import _resolve_adversarial_validation_candidate_route
    from .llm_backend_config import BackendConfig, LLMBackendConfiguration, get_llm_config
    from .quota_selector import resolve_quota_selection_strategy

    try:
        effective = llm_config if llm_config is not None else get_llm_config(repo_name=repo_name)
        if not isinstance(effective, LLMBackendConfiguration):
            raise TypeError("effective backend configuration is unavailable")
        route = _resolve_adversarial_validation_candidate_route("pr", effective)
        if not isinstance(route, list) or any(not isinstance(name, str) or not name for name in route):
            raise ValueError("ordinary-review route is invalid")
        backend_policies = []
        credential_fields = {"api_key", "openai_api_key", "openrouter_api_key", "claude_code_oauth_token", "claude_code_routine_token"}
        for name in route:
            backend = effective.get_backend_config(name)
            if backend is None:
                # Production supports built-in names without a separate table.
                backend = BackendConfig(name=name)
            if not isinstance(backend, BackendConfig):
                raise TypeError("ordinary-review backend configuration is invalid")
            declared = {item.name: getattr(backend, item.name) for item in fields(backend) if item.name not in credential_fields and item.name != "usage_markers"}
            declared["effective_type"] = backend.backend_type or name
            declared["effective_model"] = effective.get_model_for_backend(name) or name
            backend_policies.append(declared)
        max_reviews = config.MAX_ADVERSARIAL_VALIDATIONS if config.MAX_ADVERSARIAL_VALIDATIONS is not None else config.MAX_ADVERSARIAL_REVIEWS
        payload = {
            "schema": "deferred-pr-validation-policy-v1",
            "validator_version": ADVERSARIAL_VALIDATION_CACHE_VERSION,
            "ordinary_route": route,
            "backends": backend_policies,
            "quota_selection_strategy": resolve_quota_selection_strategy(effective),
            "admission": {
                "adversarial_validation": config.ENABLE_ADVERSARIAL_VALIDATION,
                "thread_gate": thread_gate_enabled,
                "max_adversarial_reviews": max_reviews,
            },
            "prompt_budget": config.MAX_PROMPT_SIZE,
            "test_script_path": config.TEST_SCRIPT_PATH,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()
    except DeferredPRValidationStateError:
        raise
    except (OSError, TypeError, ValueError, AttributeError, KeyError, RuntimeError) as exc:
        # Configuration diagnostics must not expose credential-bearing values.
        raise DeferredPRValidationStateError(f"Ordinary PR-validation policy is unavailable ({type(exc).__name__})") from exc
