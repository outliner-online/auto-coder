"""Evidence-only reconciliation of settled, uncommitted reviewer corrections."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from .git_info import parse_github_repo_from_url
from .local_review_repair import (
    LocalReviewRepairOutcome,
    LocalReviewRepairRecord,
    LocalReviewRepairRequest,
    LocalReviewRepairStore,
    get_origin_push_url,
)
from .logger_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReviewSupersessionTarget:
    """An uncached observation of the same open PR and its branch."""

    repository: str
    pr_number: int
    head_repository: str
    head_ref: str
    head_sha: str
    state: str = "open"


@dataclass(frozen=True)
class RetainedPathEvidence:
    path: str
    mode: str
    oid: str
    content_sha256: str
    file_stat: tuple[int, ...]
    parent_path: str = ""
    parent_stat: tuple[int, ...] = ()


@dataclass(frozen=True)
class RetainedReviewEvidence:
    version: int
    repository: str
    pr_number: int
    attempt_id: str
    incarnation: int
    original_head: str
    head_ref: str
    owner_pid: int
    owner_boot_id: str
    owner_start_ticks: int
    workspace: str
    common_dir: str
    git_dir: str
    origin: str
    index_sha256: str
    reflog_sha256: str
    control_stats: tuple[tuple[int, ...], ...]
    paths: tuple[RetainedPathEvidence, ...]


@dataclass(frozen=True)
class ReviewSupersessionEvidence:
    classification: str
    retained: RetainedReviewEvidence
    target: ReviewSupersessionTarget


class SupersessionBlocked(RuntimeError):
    def __init__(self, category: str, detail: str, *, returncode: Optional[int] = None) -> None:
        self.category = category
        self.returncode = returncode
        super().__init__(f"{category}: {detail}")


def _json(value: RetainedReviewEvidence | ReviewSupersessionEvidence) -> str:
    return json.dumps(asdict(value), sort_keys=True, separators=(",", ":"))


def _git(root: Path, *args: str) -> bytes:
    # Read-only inspection must not refresh the legacy index timestamp.
    result = subprocess.run(
        ["git", "-c", "diff.autoRefreshIndex=false", *args],
        cwd=root,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise SupersessionBlocked("git_evidence_unavailable", f"git {args[0]} did not complete", returncode=result.returncode)
    return result.stdout


def _stat(path: Path) -> tuple[int, ...]:
    observed = path.lstat()
    return (observed.st_dev, observed.st_ino, observed.st_mode, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns)


def _stable_bytes(path: Path, category: str) -> tuple[bytes, tuple[int, ...]]:
    try:
        before = _stat(path)
        if not stat.S_ISREG(before[2]):
            raise SupersessionBlocked(category, "required Git evidence is no longer a regular file")
        content = path.read_bytes()
        if _stat(path) != before:
            raise SupersessionBlocked("retained_output_changed", "a retained evidence file changed while being read")
        return content, before
    except OSError as exc:
        raise SupersessionBlocked(category, "required Git evidence is missing or unreadable") from exc


def _tree(root: Path, sha: str) -> dict[str, tuple[str, str]]:
    entries = {}
    for item in _git(root, "ls-tree", "-r", "-z", sha, "--").split(b"\0"):
        if item:
            metadata, name = item.split(b"\t", 1)
            mode, _kind, oid = metadata.decode("ascii").split()
            entries[os.fsdecode(name)] = (mode, oid)
    return entries


def _index(root: Path) -> dict[str, tuple[str, str]]:
    entries = {}
    for item in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if item:
            metadata, name = item.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            if stage != "0":
                raise SupersessionBlocked("unsettled_output", "the retained index has unresolved entries")
            entries[os.fsdecode(name)] = (mode, oid)
    return entries


def _retained_evidence(
    request: LocalReviewRepairRequest,
    record: LocalReviewRepairRecord,
    root: Path,
    *,
    historical: bool,
) -> RetainedReviewEvidence:
    if (
        request.attempt_id != record.attempt_id
        or request.head_sha != record.head_sha
        or request.head_ref != record.head_ref
        or request.feedback_identities != record.feedback_identities
        or request.expected_push_url != record.expected_push_url
        or request.head_repository != request.repository
        or not request.head_ref
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record.head_sha)
        or len(record.feedback_identities) != 1
        or not record.feedback_identities[0].startswith("review-output:")
        or not record.feedback_identities[0].removeprefix("review-output:")
    ):
        raise SupersessionBlocked("attempt_binding_mismatch", "the retained output is not bound to this review target")
    if record.phase not in {"executing", "indeterminate"} or record.result_sha or record.publication_started:
        raise SupersessionBlocked("publication_may_have_started", "candidate or publication-entry evidence conflicts with pre-push recovery")
    if not record.invocation_entered or record.local_job_id or record.incarnation <= 0 or record.owner_pid is None or record.owner_pid <= 0 or not record.owner_boot_id or record.owner_start_ticks is None or record.owner_start_ticks <= 0 or record.updated_at <= 0:
        raise SupersessionBlocked("owner_evidence_missing", "the synchronous captured-output attempt has incomplete ownership evidence")
    if not record.workspace_path or not record.expected_push_url:
        raise SupersessionBlocked("workspace_evidence_missing", "the retained workspace or admitted origin is unavailable")
    workspace = Path(record.workspace_path)
    if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
        raise SupersessionBlocked("workspace_evidence_missing", "the retained workspace is missing or no longer the owned directory")
    workspace = workspace.resolve()
    if not workspace.name.startswith(f"auto_coder_review_{request.pr_number}_"):
        raise SupersessionBlocked("workspace_owner_mismatch", "the retained worktree does not belong to this PR")
    checkout_registration, checkout_stat = _stable_bytes(workspace / ".git", "workspace_registration_missing")
    common = Path(os.fsdecode(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).strip()).resolve()
    registration_text = os.fsdecode(checkout_registration).strip()
    if not registration_text.startswith("gitdir: "):
        raise SupersessionBlocked("workspace_owner_mismatch", "the checkout has no worktree registration")
    git_dir = Path(registration_text.removeprefix("gitdir: ")).resolve()
    if git_dir.parent != common / "worktrees":
        raise SupersessionBlocked("workspace_owner_mismatch", "the retained checkout is not an owned worktree of this repository")
    registration = git_dir / "gitdir"
    registration_bytes, registration_stat = _stable_bytes(registration, "workspace_registration_missing")
    head_bytes, head_stat = _stable_bytes(git_dir / "HEAD", "head_evidence_missing")
    retained_common = Path(os.fsdecode(_git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")).strip()).resolve()
    if common != retained_common:
        raise SupersessionBlocked("workspace_owner_mismatch", "the retained checkout belongs to another Git repository")
    if Path(os.fsdecode(registration_bytes).strip()).resolve() != workspace / ".git":
        raise SupersessionBlocked("workspace_owner_mismatch", "the Git worktree registration does not name the retained checkout")
    if os.fsdecode(checkout_registration).strip() != f"gitdir: {git_dir}":
        raise SupersessionBlocked("workspace_owner_mismatch", "the checkout no longer names its registered Git directory")
    if min(*registration_stat[-2:], *checkout_stat[-2:]) < record.incarnation:
        raise SupersessionBlocked("workspace_owner_mismatch", "the worktree registration predates this attempt")
    if _git(workspace, "rev-parse", "HEAD").decode().strip() != record.head_sha or head_bytes.decode().strip() != record.head_sha:
        raise SupersessionBlocked("candidate_history_conflict", "the retained checkout is not detached at the original head")
    markers = ("index.lock", "HEAD.lock", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "sequencer")
    if any((git_dir / marker).exists() for marker in markers):
        raise SupersessionBlocked("unsettled_output", "the retained checkout has an unfinished Git operation")
    try:
        origin = get_origin_push_url(root)
        retained_origin = get_origin_push_url(workspace)
    except RuntimeError as exc:
        raise SupersessionBlocked("origin_evidence_unavailable", "the admitted origin is missing or unreadable") from exc
    if origin != record.expected_push_url or retained_origin != origin:
        raise SupersessionBlocked("origin_mismatch", "the admitted repository destination changed")
    named_repository = parse_github_repo_from_url(origin)
    if named_repository and named_repository.casefold() != request.repository.casefold():
        raise SupersessionBlocked("origin_mismatch", "the admitted origin belongs to another repository")
    index_bytes, index_stat = _stable_bytes(git_dir / "index", "index_evidence_missing")
    reflog, reflog_stat = _stable_bytes(git_dir / "logs" / "HEAD", "reflog_evidence_missing")
    entries = reflog.splitlines()
    if len(entries) != 1 or entries[0].split()[:2] != [b"0" * len(record.head_sha), record.head_sha.encode()]:
        raise SupersessionBlocked("candidate_history_conflict", "the retained HEAD history cannot exclude a previously created candidate")
    controls = (
        index_stat,
        reflog_stat,
        head_stat,
        registration_stat,
        checkout_stat,
    )
    baseline = _tree(workspace, record.head_sha)
    staged = _index(workspace)
    changed = sorted(path for path in baseline.keys() | staged.keys() if baseline.get(path) != staged.get(path))
    if not changed:
        raise SupersessionBlocked("settled_output_missing", "no fully staged reviewer correction corroborates completion of the copier")
    if _git(workspace, "diff", "--name-only", "-z", "--") or _git(workspace, "ls-files", "--others", "--exclude-standard", "-z"):
        raise SupersessionBlocked("unsettled_output", "the retained output has unstaged or untracked changes")
    object_format = _git(workspace, "rev-parse", "--show-object-format").decode().strip()
    if object_format not in {"sha1", "sha256"}:
        raise SupersessionBlocked("git_evidence_unavailable", "the repository object format is unsupported")
    paths = []
    for relative in changed:
        path = workspace / relative
        if path.parent.resolve() != path.parent or not path.is_relative_to(workspace):
            raise SupersessionBlocked("workspace_owner_mismatch", "an affected path traverses a changed directory")
        mode, oid = staged.get(relative, ("", ""))
        if not mode:
            if path.exists() or path.is_symlink():
                raise SupersessionBlocked("retained_output_changed", "an affected deletion no longer matches the index")
            parent = path.parent
            while not parent.exists() and parent != workspace:
                parent = parent.parent
            paths.append(RetainedPathEvidence(relative, "", "", "", (), str(parent), _stat(parent)))
            continue
        before = _stat(path)
        if stat.S_ISLNK(before[2]):
            data = os.fsencode(os.readlink(path))
            observed_mode = "120000"
        elif stat.S_ISREG(before[2]):
            data = path.read_bytes()
            observed_mode = "100755" if before[2] & 0o111 else "100644"
        else:
            raise SupersessionBlocked("unsupported_retained_entry", "an affected entry is not a regular file or symlink")
        if _stat(path) != before:
            raise SupersessionBlocked("retained_output_changed", "an affected file changed during inspection")
        git_oid = hashlib.new(object_format, b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if (observed_mode, git_oid) != (mode, oid):
            raise SupersessionBlocked("unsettled_output", "affected content or mode differs from the settled index")
        paths.append(RetainedPathEvidence(relative, mode, oid, hashlib.sha256(data).hexdigest(), before))
    if _stable_bytes(git_dir / "index", "index_evidence_missing") != (index_bytes, index_stat) or _stable_bytes(git_dir / "logs" / "HEAD", "reflog_evidence_missing") != (reflog, reflog_stat):
        raise SupersessionBlocked("retained_output_changed", "the index or HEAD history changed during inspection")
    if historical:
        cutoff = int(record.updated_at * 1_000_000_000)
        evidence_stats = [*controls, *(item.file_stat or item.parent_stat for item in paths)]
        if any(max(item[-2:]) > cutoff for item in evidence_stats):
            raise SupersessionBlocked("retained_output_changed", "legacy retained evidence changed after the durable failure")
    return RetainedReviewEvidence(
        1,
        request.repository,
        request.pr_number,
        record.attempt_id,
        record.incarnation,
        record.head_sha,
        record.head_ref,
        record.owner_pid,
        record.owner_boot_id,
        record.owner_start_ticks,
        str(workspace),
        str(common),
        str(git_dir),
        origin,
        hashlib.sha256(index_bytes).hexdigest(),
        hashlib.sha256(reflog).hexdigest(),
        controls,
        tuple(paths),
    )


def capture_pre_push_evidence(request: LocalReviewRepairRequest, record: LocalReviewRepairRecord, root: Path) -> str:
    """Checkpoint a verified settled output after a local commit failure."""
    return _json(_retained_evidence(request, record, root, historical=False))


def _target_evidence(
    request: LocalReviewRepairRequest,
    retained: RetainedReviewEvidence,
    root: Path,
    current_target: Callable[[], ReviewSupersessionTarget],
    *,
    fetch: bool,
) -> ReviewSupersessionTarget:
    try:
        target = current_target()
    except Exception as exc:
        raise SupersessionBlocked("target_readback_unavailable", "fresh PR metadata could not be read") from exc
    if not isinstance(target, ReviewSupersessionTarget) or (target.repository, target.pr_number, target.head_repository, target.head_ref, target.state) != (request.repository, request.pr_number, request.repository, request.head_ref, "open"):
        raise SupersessionBlocked("target_mismatch", "the current open PR, repository or head branch differs")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target.head_sha):
        raise SupersessionBlocked("target_readback_unavailable", "the current head identity is incomplete")
    if target.head_sha == request.head_sha:
        raise SupersessionBlocked("unchanged_head", "the correction still belongs to the current head")
    remote = _git(root, "ls-remote", "--heads", "--exit-code", "origin", f"refs/heads/{request.head_ref}").splitlines()
    if len(remote) != 1 or remote[0].split() != [target.head_sha.encode(), f"refs/heads/{request.head_ref}".encode()]:
        raise SupersessionBlocked("remote_head_mismatch", "the PR observation and authoritative branch head disagree")
    try:
        _git(root, "cat-file", "-e", f"{target.head_sha}^{{commit}}")
    except SupersessionBlocked:
        if not fetch:
            raise
        _git(root, "fetch", "--no-tags", "--no-write-fetch-head", "origin", target.head_sha)
        _git(root, "cat-file", "-e", f"{target.head_sha}^{{commit}}")
    try:
        _git(root, "merge-base", "--is-ancestor", request.head_sha, target.head_sha)
    except SupersessionBlocked as exc:
        if exc.returncode != 1:
            raise
        raise SupersessionBlocked("unrelated_lineage", "the replacement does not descend from the original head") from exc
    replacement = _tree(root, target.head_sha)
    for entry in retained.paths:
        if replacement.get(entry.path, ("", "")) != (entry.mode, entry.oid):
            raise SupersessionBlocked("correction_not_incorporated", "the replacement does not retain every affected path's full content and mode")
    return target


def reconcile_review_supersession(
    request: LocalReviewRepairRequest,
    store: LocalReviewRepairStore,
    record: LocalReviewRepairRecord,
    root: Path,
    current_target: Optional[Callable[[], ReviewSupersessionTarget]],
) -> LocalReviewRepairOutcome:
    """Release one proven old blocker, without publishing or replaying anything."""
    try:
        if current_target is None:
            raise SupersessionBlocked("target_readback_unavailable", "no authoritative PR observer is available")
        retained = _retained_evidence(request, record, root, historical=not bool(record.pre_push_evidence_json))
        if record.pre_push_evidence_json and _json(retained) != record.pre_push_evidence_json:
            raise SupersessionBlocked("retained_output_changed", "the retained output differs from its durable pre-push checkpoint")
        target = _target_evidence(request, retained, root, current_target, fetch=True)
        provenance = _json(ReviewSupersessionEvidence("checkpointed_pre_push" if record.pre_push_evidence_json else "corroborated_legacy_pre_push", retained, target))

        def confirm() -> bool:
            # The database write lock fences attempt ownership; fresh Git and PR
            # observations fence changed output and target evidence at settlement.
            if _retained_evidence(request, record, root, historical=not bool(record.pre_push_evidence_json)) != retained:
                raise SupersessionBlocked("retained_output_changed", "the retained output changed before settlement")
            if _target_evidence(request, retained, root, current_target, fetch=False) != target:
                raise SupersessionBlocked("target_changed", "the current PR head changed before settlement")
            if _retained_evidence(request, record, root, historical=not bool(record.pre_push_evidence_json)) != retained:
                raise SupersessionBlocked("retained_output_changed", "the retained output changed during target readback")
            return True

        if not store.settle_superseded(request, record, target.head_sha, provenance, confirm):
            raise SupersessionBlocked("attempt_owner_changed", "the durable attempt or its ownership changed before settlement")
        logger.info("PR #{} retained review {} superseded by {}; independent CI and review remain required", request.pr_number, record.attempt_id, target.head_sha)
        return LocalReviewRepairOutcome("superseded", f"retained correction {record.attempt_id} is fully incorporated in {target.head_sha}", executed=True)
    except SupersessionBlocked as exc:
        return LocalReviewRepairOutcome("indeterminate", f"reviewer supersession blocked [{exc}]", executed=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        logger.debug("Retained review evidence is unavailable: {}", type(exc).__name__)
        return LocalReviewRepairOutcome("indeterminate", f"reviewer supersession blocked [evidence_unavailable: {type(exc).__name__}]", executed=True)
