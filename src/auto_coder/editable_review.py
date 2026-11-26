"""Source-bound PR reviewer edits and existing durable correction publication.

A reviewer that changes source has produced an implementation candidate, not
review evidence for its input commit. The controller publishes that candidate
through the established correction path; a later independent review owns the
new commit's verdict.
"""

from __future__ import annotations

import contextvars
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from .local_review_repair import (
    LocalReviewRepairOutcome,
    LocalReviewRepairRequest,
    LocalReviewRepairStore,
    _resume_publication,
    execute_local_review_repair,
    get_origin_push_url,
    local_review_repair_db_path,
)
from .review_supersession import ReviewSupersessionTarget, reconcile_review_supersession
from .worktree_utils import (
    WorkspaceFileState,
    WorkspaceHandoffError,
    bind_existing_local_workspace,
    capture_workspace_source_files,
    sync_worktree_changes_back,
)

REVIEW_OUTPUT_PREFIX = "review-output:"
PublicationAuthorityCheck = Callable[[str, str], bool]


@dataclass(frozen=True)
class ReviewWorkspaceSnapshot:
    """The clean exact-head source state admitted to one reviewer."""

    root: Path
    head_sha: str
    files: tuple[WorkspaceFileState, ...]
    origin_push_url: str


class ReviewChangesProduced(RuntimeError):
    """A successful review turn changed source and cannot certify its old head."""

    def __init__(self, snapshot: ReviewWorkspaceSnapshot) -> None:
        self.snapshot = snapshot
        super().__init__("Reviewer changed source; publication and independent new-head validation are required")


_CURRENT_REVIEW_WORKSPACE: contextvars.ContextVar[Optional[ReviewWorkspaceSnapshot]] = contextvars.ContextVar("editable_pr_review_workspace", default=None)


def _verify_review_head(root: Path, expected_head: str) -> None:
    observed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    if observed.returncode != 0 or observed.stdout.strip().lower() != expected_head.lower():
        raise WorkspaceHandoffError("review workspace HEAD changed or is unavailable")


def capture_review_workspace(execution_cwd: str, expected_head: str) -> ReviewWorkspaceSnapshot:
    """Capture the source baseline before any reviewer or dynamic test runs."""
    root = Path(execution_cwd).resolve()
    _verify_review_head(root, expected_head)
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=root, capture_output=True)
    if clean.returncode != 0:
        raise WorkspaceHandoffError("review workspace must start from a clean committed source tree")
    files = capture_workspace_source_files(root)
    tracked = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=root, capture_output=True)
    if tracked.returncode != 0:
        raise WorkspaceHandoffError("review source inventory is unavailable")
    tracked_names = {part.decode("utf-8", errors="surrogateescape") for part in tracked.stdout.split(b"\0") if part}
    if any(state.relative_path not in tracked_names for state in files):
        raise WorkspaceHandoffError("review workspace contains uncommitted source before reviewer entry")
    _verify_review_head(root, expected_head)
    return ReviewWorkspaceSnapshot(root, expected_head, files, get_origin_push_url(root))


def _verify_review_snapshot(snapshot: ReviewWorkspaceSnapshot) -> None:
    _verify_review_head(snapshot.root, snapshot.head_sha)
    if get_origin_push_url(snapshot.root) != snapshot.origin_push_url:
        raise WorkspaceHandoffError("review workspace origin push destination changed")


def review_workspace_changed(snapshot: ReviewWorkspaceSnapshot) -> bool:
    """Detect source content, deletion, addition, mode or symlink changes."""
    _verify_review_snapshot(snapshot)
    preserved = tuple(state.relative_path for state in snapshot.files)
    return capture_workspace_source_files(snapshot.root, preserved) != snapshot.files


@contextmanager
def bind_editable_review(snapshot: ReviewWorkspaceSnapshot) -> Iterator[None]:
    """Bind a guard to exactly one controller-owned review workspace."""
    token = _CURRENT_REVIEW_WORKSPACE.set(snapshot)
    try:
        yield
    finally:
        _CURRENT_REVIEW_WORKSPACE.reset(token)


def editable_review_active() -> bool:
    """Return whether a source-bound editable PR review is active."""
    return _CURRENT_REVIEW_WORKSPACE.get() is not None


def assert_review_workspace_unchanged() -> None:
    """Stop verdict consumption as soon as a successful turn produces edits."""
    snapshot = _CURRENT_REVIEW_WORKSPACE.get()
    if snapshot is not None and review_workspace_changed(snapshot):
        raise ReviewChangesProduced(snapshot)


def publish_review_changes(
    snapshot: ReviewWorkspaceSnapshot,
    *,
    repository: str,
    pr_number: int,
    head_repository: str,
    head_ref: str,
    review_identity: str,
    authority_check: Optional[PublicationAuthorityCheck] = None,
    store: Optional[LocalReviewRepairStore] = None,
) -> LocalReviewRepairOutcome:
    """Copy settled reviewer edits into the existing fenced correction publisher.

    The review output identity deduplicates only the publication record. It is
    not a finding identity and grants no repair-allowance or merge authority.
    """
    if not review_identity:
        raise ValueError("review publication requires the originating review identity")
    if not review_workspace_changed(snapshot):
        return LocalReviewRepairOutcome("completed_no_change", "reviewer produced no source changes")
    preserved = tuple(state.relative_path for state in snapshot.files)
    selected_files = capture_workspace_source_files(snapshot.root, preserved)
    request = LocalReviewRepairRequest(repository, pr_number, head_repository, head_ref, snapshot.head_sha, (f"{REVIEW_OUTPUT_PREFIX}{review_identity}",), "", expected_push_url=snapshot.origin_push_url)

    def authorized() -> bool:
        _verify_review_snapshot(snapshot)
        return authority_check is None or authority_check(request.head_sha, request.head_ref)

    def copy_result(_request: LocalReviewRepairRequest, destination: str) -> str:
        _verify_review_snapshot(snapshot)
        if capture_workspace_source_files(snapshot.root, preserved) != selected_files:
            raise WorkspaceHandoffError("reviewer source changed before publication capture")
        with bind_existing_local_workspace(snapshot.root, Path(destination)) as binding:
            sync_worktree_changes_back(snapshot.root, destination, binding)
        if capture_workspace_source_files(snapshot.root, preserved) != selected_files:
            raise WorkspaceHandoffError("reviewer source changed during publication capture")
        return "ACTION_SUMMARY: retained settled PR reviewer source changes"

    return execute_local_review_repair(
        request,
        store=store,
        executor=copy_result,
        provider_entry_authorizer=authorized,
        publication_authorizer=authorized,
        allow_commit_repair=False,
        resume_other_attempts=False,
        root=snapshot.root,
    )


def resume_pending_review_publication(
    *,
    repository: str,
    pr_number: int,
    root: str | Callable[[], str],
    authority_check: Optional[PublicationAuthorityCheck] = None,
    store: Optional[LocalReviewRepairStore] = None,
    current_target: Optional[Callable[[], ReviewSupersessionTarget]] = None,
) -> Optional[LocalReviewRepairOutcome]:
    """Recover retained reviewer output before admitting another reviewer.

    Definitely-not-started publication owns no committed candidate. Such a
    refusal is neither success nor recovery work, and cannot wedge a later head.
    """
    store = store or LocalReviewRepairStore(local_review_repair_db_path(repository))
    lookup = LocalReviewRepairRequest(repository, pr_number, repository, "", "", (), "")
    record = store.get_unfinished(lookup, feedback_prefix=REVIEW_OUTPUT_PREFIX)
    if record is None:
        return None
    request = LocalReviewRepairRequest(repository, pr_number, repository, record.head_ref, record.head_sha, record.feedback_identities, "", expected_push_url=record.expected_push_url)
    if record.phase == "indeterminate":
        return reconcile_review_supersession(request, store, record, Path(root() if callable(root) else root).resolve(), current_target)
    if record.phase != "publication_pending":
        return LocalReviewRepairOutcome(record.phase, f"reviewer publication is {record.phase}; retained output requires reconciliation", executed=True)
    return _resume_publication(
        request,
        store,
        record,
        Path(root() if callable(root) else root).resolve(),
        publication_authorizer=None if authority_check is None else lambda: authority_check(record.head_sha, record.head_ref),
    )
