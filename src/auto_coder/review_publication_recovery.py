"""Resume one proven pre-push reviewer correction with Controller App identity."""

from __future__ import annotations

import fcntl
import json
import subprocess
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterator, Optional

from .controller_git_identity import ControllerGitIdentity
from .local_review_repair import (
    LocalReviewRepairOutcome,
    LocalReviewRepairRecord,
    LocalReviewRepairRequest,
    LocalReviewRepairStore,
    _resume_publication,
    get_origin_push_url,
)
from .logger_config import get_logger
from .review_supersession import (
    RetainedPathEvidence,
    RetainedReviewEvidence,
    ReviewSupersessionTarget,
    SupersessionBlocked,
    _git,
    _json,
    _retained_evidence,
    _tree,
)

logger = get_logger(__name__)


@contextmanager
def review_publication_lock(store: LocalReviewRepairStore, pr_number: int) -> Iterator[bool]:
    """Serialize commit continuation and pending push for this PR across processes."""
    lock_path = store.path.parent / f".{store.path.name}.pr-{pr_number}.review-publication.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def verified_commit_identity(request: LocalReviewRepairRequest, provider: Callable[[], ControllerGitIdentity]) -> ControllerGitIdentity:
    """Require a verified identity for this repository, without any fallback."""
    identity = provider()
    if not isinstance(identity, ControllerGitIdentity) or identity.repository.casefold() != request.repository.casefold() or not identity.login.endswith("[bot]") or identity.account_id <= 0 or identity.app_id <= 0:
        raise RuntimeError("verified Controller App Git identity does not match the repository")
    return identity


def review_commit_message(request: LocalReviewRepairRequest) -> str:
    """Retain the originating PR and exact review-output identity in the commit."""
    identities = "\n".join(f"Review-Output: {identity.removeprefix('review-output:')}" for identity in request.feedback_identities)
    return f"Retain reviewer corrections for PR #{request.pr_number}\n\nReview-Origin: https://github.com/{request.repository}/pull/{request.pr_number}\n{identities}"


def _current_target(request: LocalReviewRepairRequest, root: Path, observer: Optional[Callable[[], ReviewSupersessionTarget]], *, require_original: bool) -> ReviewSupersessionTarget:
    if observer is None:
        raise SupersessionBlocked("target_readback_unavailable", "no authoritative PR observer is available")
    try:
        target = observer()
    except Exception as exc:
        raise SupersessionBlocked("target_readback_unavailable", "fresh PR metadata could not be read") from exc
    if not isinstance(target, ReviewSupersessionTarget) or (target.repository, target.pr_number, target.head_repository, target.head_ref, target.state) != (request.repository, request.pr_number, request.repository, request.head_ref, "open"):
        raise SupersessionBlocked("target_mismatch", "the current open PR, repository or branch differs")
    if require_original and target.head_sha != request.head_sha:
        raise SupersessionBlocked("target_changed", "the PR no longer names the retained correction's exact head")
    if require_original:
        remote = _git(root, "ls-remote", "--heads", "--exit-code", "origin", f"refs/heads/{request.head_ref}").splitlines()
        if len(remote) != 1 or remote[0].split() != [request.head_sha.encode(), f"refs/heads/{request.head_ref}".encode()]:
            raise SupersessionBlocked("remote_head_mismatch", "the authoritative branch no longer names the retained head")
    return target


def _decode_retained(evidence_json: str) -> RetainedReviewEvidence:
    values = json.loads(evidence_json)
    values["control_stats"] = tuple(tuple(item) for item in values["control_stats"])
    values["paths"] = tuple(RetainedPathEvidence(**{**item, "file_stat": tuple(item["file_stat"]), "parent_stat": tuple(item["parent_stat"])}) for item in values["paths"])
    return RetainedReviewEvidence(**values)


def _candidate_evidence(request: LocalReviewRepairRequest, record: LocalReviewRepairRecord, retained: RetainedReviewEvidence, identity: ControllerGitIdentity, root: Path) -> str:
    """Recognize exactly the controller candidate after a lost SHA checkpoint."""
    workspace = Path(record.workspace_path)
    if not workspace.is_dir() or workspace.is_symlink() or str(workspace.resolve()) != retained.workspace:
        raise SupersessionBlocked("workspace_owner_mismatch", "the retained correction workspace changed")
    if get_origin_push_url(root) != record.expected_push_url or get_origin_push_url(workspace) != record.expected_push_url:
        raise SupersessionBlocked("origin_mismatch", "the admitted push destination changed")
    common = _git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
    git_dir = _git(workspace, "rev-parse", "--absolute-git-dir").decode().strip()
    if common != retained.common_dir or git_dir != retained.git_dir:
        raise SupersessionBlocked("workspace_owner_mismatch", "the retained Git registration changed")
    candidate = _git(workspace, "rev-parse", "HEAD").decode().strip()
    if candidate == request.head_sha:
        raise SupersessionBlocked("commit_entry_unresolved", "a prior commit entry has no confirmed candidate; the commit is not repeated")
    metadata = _git(workspace, "show", "-s", "--format=%P%x00%an%x00%ae%x00%cn%x00%ce%x00%B", candidate).decode().strip().split("\0")
    env = identity.env()
    expected_metadata = [request.head_sha, env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"], env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"], review_commit_message(request)]
    if metadata != expected_metadata:
        raise SupersessionBlocked("candidate_mismatch", "the retained candidate has a different parent, identity or review origin")
    reflog = (Path(git_dir) / "logs" / "HEAD").read_bytes().splitlines()
    if len(reflog) != 2 or reflog[0].split()[:2] != [b"0" * len(request.head_sha), request.head_sha.encode()] or reflog[1].split()[:2] != [request.head_sha.encode(), candidate.encode()]:
        raise SupersessionBlocked("candidate_history_conflict", "the retained candidate does not have one corroborated controller commit")
    expected_tree = _tree(root, request.head_sha)
    for entry in retained.paths:
        if entry.mode:
            expected_tree[entry.path] = (entry.mode, entry.oid)
        else:
            expected_tree.pop(entry.path, None)
    if _tree(workspace, candidate) != expected_tree:
        raise SupersessionBlocked("candidate_mismatch", "the retained candidate does not contain exactly the captured correction")
    if _git(workspace, "diff", "HEAD", "--name-only", "-z", "--") or _git(workspace, "ls-files", "--others", "--exclude-standard", "-z"):
        raise SupersessionBlocked("unsettled_output", "the retained candidate has further uncommitted source changes")
    return candidate


def recover_current_head_review(
    request: LocalReviewRepairRequest,
    store: LocalReviewRepairStore,
    record: LocalReviewRepairRecord,
    root: Path,
    *,
    current_target: Optional[Callable[[], ReviewSupersessionTarget]],
    authority_check: Optional[Callable[[str, str], bool]],
    commit_identity_provider: Callable[[], ControllerGitIdentity],
) -> Optional[LocalReviewRepairOutcome]:
    """Continue a settled local identity failure; never repeat reviewer execution."""
    try:
        target = _current_target(request, root, current_target, require_original=False)
        if target.head_sha != request.head_sha:
            return None  # A newer head remains owned by evidence-only supersession.
        if authority_check is None:
            raise SupersessionBlocked("publication_authority_unavailable", "no current publication authority is available")
        recovery = store.get_commit_recovery(request)
        identity = verified_commit_identity(request, commit_identity_provider)
        identity_json = json.dumps(asdict(identity), sort_keys=True, separators=(",", ":"))
        if recovery is None:
            if "author identity unknown" not in record.reason.casefold() and not (record.pre_push_evidence_json and record.reason.startswith("Controller Git identity unavailable:")):
                raise SupersessionBlocked("pre_push_failure_unproven", "the failure is not a definite local Git identity refusal")
            retained = _retained_evidence(request, record, root, historical=not bool(record.pre_push_evidence_json))
            evidence_json = _json(retained)
            if record.pre_push_evidence_json and evidence_json != record.pre_push_evidence_json:
                raise SupersessionBlocked("retained_output_changed", "the staged correction differs from its pre-push checkpoint")

            def confirm_entry() -> bool:
                _current_target(request, root, current_target, require_original=True)
                if not authority_check(request.head_sha, request.head_ref):
                    raise SupersessionBlocked("publication_authority_changed", "the retained correction no longer has publication authority")
                if _retained_evidence(request, record, root, historical=not bool(record.pre_push_evidence_json)) != retained:
                    raise SupersessionBlocked("retained_output_changed", "the correction changed before commit entry")
                return True

            recovery = store.begin_commit_recovery(request, record, evidence_json, identity_json, confirm_entry)
            if recovery is None:
                raise SupersessionBlocked("attempt_owner_changed", "the retained attempt or commit entry changed")
            # Entry is durable before the local Git operation. A crash cannot
            # cause a second commit merely because the SHA checkpoint is absent.
            from .local_review_repair import git_commit_with_retry

            commit = git_commit_with_retry(review_commit_message(request), cwd=record.workspace_path, allow_repair=False, commit_env=identity.env())
            if not commit.success:
                reason = f"Controller corrective commit failed: {commit.stderr or commit.stdout}"
                store.finish_commit_recovery(request, record, recovery, reason=reason, confirm=lambda: True)
                return LocalReviewRepairOutcome("indeterminate", reason, executed=True)
        else:
            if recovery.incarnation != record.incarnation or recovery.original_record_json != json.dumps(asdict(record), sort_keys=True, separators=(",", ":")) or recovery.identity_json != identity_json:
                raise SupersessionBlocked("attempt_owner_changed", "the durable commit entry no longer matches its attempt or identity")
            if recovery.phase == "failed":
                raise SupersessionBlocked("commit_failed", recovery.reason)
            retained = _decode_retained(recovery.evidence_json)
        candidate = _candidate_evidence(request, record, retained, identity, root)

        def confirm_candidate() -> bool:
            _current_target(request, root, current_target, require_original=True)
            if not authority_check(request.head_sha, request.head_ref):
                raise SupersessionBlocked("publication_authority_changed", "the candidate no longer has publication authority")
            return _candidate_evidence(request, record, retained, identity, root) == candidate

        if not store.finish_commit_recovery(request, record, recovery, result_sha=candidate, confirm=confirm_candidate):
            raise SupersessionBlocked("attempt_owner_changed", "the candidate checkpoint lost its original attempt ownership")
        pending = store.get(request)
        if pending is None or pending.result_sha != candidate or pending.phase != "publication_pending":
            raise SupersessionBlocked("candidate_checkpoint_unavailable", "the durable candidate could not be read back")
        logger.info("PR #{} retained review {} recovered as {}; independent CI and review remain required", request.pr_number, request.attempt_id, candidate)
        return _resume_publication(request, store, pending, root, publication_authorizer=lambda: authority_check(request.head_sha, request.head_ref))
    except SupersessionBlocked as exc:
        return LocalReviewRepairOutcome("indeterminate", f"reviewer commit recovery blocked [{exc}]", executed=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        logger.debug("Reviewer commit recovery is unavailable: {}", type(exc).__name__)
        return LocalReviewRepairOutcome("indeterminate", f"reviewer commit recovery blocked [evidence_or_identity_unavailable: {exc}]", executed=True)
