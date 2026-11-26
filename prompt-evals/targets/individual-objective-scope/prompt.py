"""Render the production individual-review prompt for Promptfoo cases."""

from pathlib import Path
from string import Template

import yaml


def render(context):
    """Load the same YAML template and substitute one Promptfoo case."""
    # Match IncrementalReviewContext("FULL") for these initial-review cases.
    variables = {
        "review_mode": "FULL",
        "predecessor_decision_key": None,
        "predecessor_evidence": "",
        "assessed_delta": "",
        **context["vars"],
    }
    prompt_path = Path(__file__).resolve().parents[3] / "src/auto_coder/prompts.yaml"
    template = yaml.safe_load(prompt_path.read_text(encoding="utf-8"))["issue"]["adversarial_specification_analysis"]
    return Template(template).substitute({key: "" if value is None else str(value) for key, value in variables.items()})
