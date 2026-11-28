"""Production equivalence of the lightweight advisory gap-completion renderer."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from auto_coder.prompt_loader import render_prompt

ROOT = Path(__file__).parents[1]
TARGET = ROOT / "prompt-evals/targets/test-oracle-gap-completion"
CASES = yaml.safe_load((TARGET / "cases/completion.yaml").read_text(encoding="utf-8"))
pytestmark = pytest.mark.usefixtures("_use_real_commands")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["vars"]["id"])
def test_gap_completion_renders_production_prompt_without_installed_application(case, tmp_path):
    # Disable site/.pth processing so the checkout's editable installation and
    # normal test PYTHONPATH cannot conceal a missing CI import dependency.
    script = """
import importlib.util
import json
import runpy
import sys

sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec("auto_coder") is None
module = runpy.run_path(sys.argv[2])
print(json.dumps(module["render"]({"vars": json.load(sys.stdin)})))
"""
    variables = case["vars"]
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(Path(yaml.__file__).parent.parent), str(TARGET / "prompt.py")],
        input=json.dumps(variables),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    expected = render_prompt(
        "pr.adversarial_validation_gap_completion",
        repo_name="owner/repo",
        pr_number=42,
        head_sha="head-h2",
        base_sha="base-b",
        validation_snapshot="snapshot-h2",
        requirement_manifest="Manifest mode: CURRENT\n- REQ-001: Preserve rejected candidate state.",
        completion_gap_ids='["TOG-example"]',
        completion_gaps=variables["recorded_gap"],
        prior_adjudication=variables["scenario"],
        changed_tests=variables["changed_tests"],
        changed_files="src/grid.py, tests/test_grid.py",
        ci_execution_evidence=variables["ci_evidence"],
    )
    assert json.loads(result.stdout) == expected + "\n\nReturn only the complete adversarial JSON response."


def test_initial_individual_review_uses_current_production_context_defaults():
    from dataclasses import asdict
    from runpy import run_path

    from auto_coder.specification_analyzer import IncrementalReviewContext

    target = ROOT / "prompt-evals/targets/individual-objective-scope"
    render = run_path(str(target / "prompt.py"))["render"]
    context = asdict(IncrementalReviewContext("FULL"))
    context["review_mode"] = context.pop("mode")
    for case in yaml.safe_load((target / "cases/objective-scope.yaml").read_text(encoding="utf-8")):
        expected = render_prompt("issue.adversarial_specification_analysis", include_header=False, **context, **case["vars"])
        assert render({"vars": case["vars"]}) == expected


@pytest.mark.parametrize("target", json.loads((ROOT / "prompt-evals/registry.json").read_text(encoding="utf-8"))["targets"], ids=lambda target: target["id"])
def test_every_registered_corpus_case_renders_without_missing_inputs(target):
    from runpy import run_path

    render = run_path(str((ROOT / target["config"]).with_name("prompt.py")))["render"]
    rendered_cases = 0
    for pattern in target["cases"]:
        for path in ROOT.glob(pattern):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            cases = document if isinstance(document, list) else document["tests"]
            for case in cases:
                prompt = render({"vars": case["vars"]})
                assert isinstance(prompt, str) and prompt.strip()
                rendered_cases += 1
    assert rendered_cases > 0
