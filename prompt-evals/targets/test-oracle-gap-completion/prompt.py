"""Render the shipped focused gap-completion prompt with CI-only dependencies."""

from pathlib import Path
from string import Template

import yaml


def render(context):
    """Match the production completion prompt without importing the application."""
    prompts = yaml.safe_load((Path(__file__).resolve().parents[3] / "src/auto_coder/prompts.yaml").read_text(encoding="utf-8"))
    # This completion key has no prepended contract policies or conditionals.
    # The production-equivalence regression detects changes to that contract.
    values = {
        "repo_name": "owner/repo",
        "pr_number": 42,
        "head_sha": "head-h2",
        "base_sha": "base-b",
        "validation_snapshot": "snapshot-h2",
        "requirement_manifest": "Manifest mode: CURRENT\n- REQ-001: Preserve rejected candidate state.",
        "completion_gap_ids": '["TOG-example"]',
        "completion_gaps": context["vars"]["recorded_gap"],
        "prior_adjudication": context["vars"]["scenario"],
        "changed_tests": context["vars"]["changed_tests"],
        "changed_files": "src/grid.py, tests/test_grid.py",
        "ci_execution_evidence": context["vars"]["ci_evidence"],
    }
    rendered = Template(prompts["pr"]["adversarial_validation_gap_completion"]).safe_substitute(values)
    header = prompts.get("header")
    if header:
        rendered = f"{header.rstrip()}\n\n{rendered}"
    return rendered + "\n\nReturn only the complete adversarial JSON response."
