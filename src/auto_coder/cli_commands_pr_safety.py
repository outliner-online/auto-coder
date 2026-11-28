"""Inspect and explicitly acknowledge persistent PR automation stops."""

from __future__ import annotations

import json
from dataclasses import asdict

import click

from .github_controller_auth import select_controller_auth
from .pr_automation_guard import PRGuardStore
from .util.gh_cache import GitHubClient


@click.group(name="pr-safety")
def pr_safety_group() -> None:
    """Inspect PR automation stops and grant an operator-reviewed continuation."""


@pr_safety_group.command(name="status")
@click.option("--repo", required=True)
@click.option("--pr", required=True, type=click.IntRange(min=1))
def pr_safety_status(repo: str, pr: int) -> None:
    """Print retained stop evidence and its acknowledgement history."""
    try:
        store = PRGuardStore()
        click.echo(json.dumps({"repository": repo, "pr_number": pr, "decision": asdict(store.status(repo, pr)), "events": store.events(repo, pr)}, indent=2, sort_keys=True))
    except Exception as exc:
        raise click.ClickException(f"PR safety state is unavailable: {exc}") from exc


@pr_safety_group.command(name="resume")
@click.option("--repo", required=True)
@click.option("--pr", required=True, type=click.IntRange(min=1))
@click.option("--expected-head", required=True)
@click.option("--reason", required=True)
@click.option("--github-token", envvar="GITHUB_TOKEN", hidden=True)
def pr_safety_resume(repo: str, pr: int, expected_head: str, reason: str, github_token: str | None) -> None:
    """Acknowledge this stop and allow thirty additional observed commits.

    This does not start a stopped worker, bypass other PR gates, or invoke a
    coding agent. Ordinary polling or explicit processing may evaluate it next.
    """
    if not reason.strip():
        raise click.UsageError("--reason must explain the external intervention")
    try:
        repository = PRGuardStore._identity(repo, pr)
        store = PRGuardStore()
        stop_generation = store.stop_generation(repository, pr)
        auth = select_controller_auth(repository, provided_token=github_token)
        github = GitHubClient(auth_provider=auth, repository=repository)
        history = github.get_pr_automation_history_strict(repository, pr)
        decision = store.resume(repository, pr, history, expected_head=expected_head, reason=reason, expected_stop_generation=stop_generation)
        click.echo(json.dumps({"repository": repository, "pr_number": pr, "decision": asdict(decision)}, indent=2, sort_keys=True))
    except Exception as exc:
        raise click.ClickException(f"PR automation was not resumed: {exc}") from exc
