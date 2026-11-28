import glob
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

RUNNER = Path(__file__).parents[1] / "prompt-evals/run_prompt_evals.py"
REPOSITORY_ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.usefixtures("_use_real_commands")


def commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def setup_repo(tmp_path: Path, targets: list[dict[str, object]]) -> tuple[Path, str]:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "prompt-evals").mkdir()
    (tmp_path / "prompt-evals/registry.json").write_text(json.dumps({"schema_version": 1, "targets": targets}))
    (tmp_path / "src").mkdir()
    (tmp_path / "src/prompts.yaml").write_text("a: {prompt: original}\nb: {prompt: original}\nshared: original\nshared.safety: original\n")
    return tmp_path, commit(tmp_path, "base")


def invoke(repo: Path, base: str, fake_npx: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return subprocess.run(["python", str(RUNNER), "--repo", str(repo), "--base", base, "--head", head, "--npx", str(fake_npx), *extra_args], text=True, capture_output=True)


def target(name: str, keys: list[str]) -> dict[str, object]:
    root = f"prompt-evals/targets/{name}"
    return {"id": name, "prompt_dependencies": [{"path": "src/prompts.yaml", "keys": keys}], "config": f"{root}/promptfooconfig.yaml", "cases": [f"{root}/cases/*.yaml"]}


def completed_row(case_id: str = "semantic-case", passed: bool = True) -> dict[str, object]:
    # This is the provider/evaluator JSON boundary, not a copy of runner logic.
    assertion = {"type": "javascript", "value": "JSON.parse(output).ok === true"}
    reason = "Assertion passed" if passed else "Custom function returned false"
    row = {
        "success": passed,
        "failureReason": 0 if passed else 1,
        "testCase": {"vars": {"id": case_id}, "assert": [{"type": "is-json"}, assertion]},
        "response": {"output": '{"ok":true}' if passed else '{"ok":false}', "raw": {"object": "response", "status": "completed", "incomplete_details": None}},
        "gradingResult": {
            "pass": passed,
            "reason": reason,
            "componentResults": [{"pass": True, "reason": "Assertion passed", "assertion": {"type": "is-json"}}, {"pass": passed, "reason": reason, "assertion": assertion}],
        },
    }
    if not passed:
        # Promptfoo also puts ordinary assertion failures in the top-level error.
        row["error"] = reason
    return row


def provider_error_row() -> dict[str, object]:
    message = "401 Unauthorized: API credential rejected"
    return {
        "success": False,
        "failureReason": 2,
        "error": message,
        "testCase": {"vars": {"id": "provider-error"}},
        "response": {"error": message},
        "gradingResult": None,
    }


def fake_npx(tmp_path: Path, document: object = None, exit_code: int | None = None, *, emit_output: bool = True) -> Path:
    script = tmp_path / "npx"
    if document is None:
        document = result_document([completed_row()])
    script.write_text(
        "#!/usr/bin/env python\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if os.environ.get('NPX_LOG'):\n"
        "    with open(os.environ['NPX_LOG'], 'a') as log: log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"if {emit_output!r}:\n"
        f"    Path(sys.argv[sys.argv.index('--output') + 1]).write_text({json.dumps(document)!r})\n"
        f"raise SystemExit({exit_code!r} if {exit_code!r} is not None else int(os.environ.get('NPX_EXIT', '0')))\n"
    )
    script.chmod(0o755)
    return script


def test_empty_registry_and_unrelated_change_do_not_invoke_provider(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path, [])
    (repo / "unrelated.py").write_text("value = 1\n")
    commit(repo, "unrelated")
    result = invoke(repo, base, repo / "missing-npx")
    assert result.returncode == 0
    assert "No prompt-evaluation targets affected" in result.stdout


def test_prompt_regression_workflow_is_active_and_advisory() -> None:
    assert not (REPOSITORY_ROOT / ".github/workflows/prompt-regression.yml.disabled").exists()
    workflow = yaml.safe_load((REPOSITORY_ROOT / ".github/workflows/prompt-regression.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["selective-prompt-evals"]
    assert "if" not in job
    assert workflow["on"]["pull_request"]["types"] == ["opened", "synchronize", "reopened"]
    steps = job["steps"]
    evaluation_step = next(step for step in steps if step.get("name") == "Run selective advisory evaluation")
    assert evaluation_step["env"]["OPENAI_API_KEY"] == "${{ secrets.OPENAI_API_KEY }}"
    assert "CODEX_AUTH_JSON" not in evaluation_step["env"]
    assert "--require-credentials" in evaluation_step["run"]
    assert "--untrusted" in evaluation_step["run"]

    reporting = yaml.safe_load((REPOSITORY_ROOT / ".github/workflows/prompt-regression-report.yml").read_text())
    assert reporting["permissions"]["pull-requests"] == "write"
    script = reporting["jobs"]["publish"]["steps"][-1]["with"]["script"]
    assert "github-actions[bot]" in script
    assert "<!-- auto-coder:prompt-regression-advisory:v1 -->" in script
    assert "observed.pull_requests.length !== 1" in script

    registry = json.loads((REPOSITORY_ROOT / "prompt-evals/registry.json").read_text())
    for registered in registry["targets"]:
        config = yaml.safe_load((REPOSITORY_ROOT / registered["config"]).read_text())
        assert config["providers"] == [
            {
                "id": "openai:responses:gpt-6.1-terra",
                "config": {"reasoning": {"effort": "high"}, "max_output_tokens": 8192, "store": False},
            }
        ], registered["id"]


def test_incremental_rereview_semantic_cases_use_production_prompt_and_record_execution() -> None:
    from auto_coder.prompt_loader import get_prompt_template

    target = REPOSITORY_ROOT / "prompt-evals/targets/incremental-pr-rereview"
    registry = json.loads((REPOSITORY_ROOT / "prompt-evals/registry.json").read_text())
    registered = next(item for item in registry["targets"] if item["id"] == "incremental-pr-rereview")
    assert registered["config"] == "prompt-evals/targets/incremental-pr-rereview/promptfooconfig.yaml"
    assert registered["cases"] == ["prompt-evals/targets/incremental-pr-rereview/cases/*.yaml"]
    assert registered["prompt_dependencies"] == [
        {
            "path": "src/auto_coder/prompts.yaml",
            "keys": [
                "pr.adversarial_validation",
                "pr.adversarial_validation_rereview",
                "policies.objective_requirements_boundary",
                "policies.parent_child_contract_boundary",
                "policies.pr_validation_contract_boundary",
                "policies.bounded_correction_contract",
                "policies.task_scoped_test_execution",
            ],
        }
    ]
    config = yaml.safe_load((target / "promptfooconfig.yaml").read_text())
    assert config["tests"] == ["file://cases.py:generate_tests"]
    spec = importlib.util.spec_from_file_location("incremental_pr_rereview_prompt", target / "prompt.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    prompts = yaml.safe_load((REPOSITORY_ROOT / "src/auto_coder/prompts.yaml").read_text())
    assert module._production_template(prompts, prompts["pr"]["adversarial_validation"]) == get_prompt_template("pr.adversarial_validation")
    assert module._production_template(prompts, prompts["pr"]["adversarial_validation_rereview"]) == get_prompt_template("pr.adversarial_validation_rereview")

    case_document = yaml.safe_load((target / "cases/policy.yaml").read_text())
    cases = case_document["tests"]
    assert len(cases) == 4
    case_spec = importlib.util.spec_from_file_location("incremental_pr_rereview_cases", target / "cases.py")
    assert case_spec is not None and case_spec.loader is not None
    case_module = importlib.util.module_from_spec(case_spec)
    case_spec.loader.exec_module(case_module)
    assert case_module.generate_tests() == cases
    assert [case["vars"]["id"] for case in cases] == [
        "fixed-original-oracle-converges",
        "unrelated-variant-and-parent-demand-are-not-blockers",
        "correction-induced-material-defect-blocks",
        "local-required-regression-fix-retains-input-head-finding",
    ]
    rendered = module.render({"vars": cases[0]["vars"]})
    assert "Previous reviewed head: accepted-head" in rendered
    assert "Current head: current-head" in rendered
    assert "TOG-original-oracle" in rendered
    assert "Stable Issue Requirement Manifest:\nManifest mode: explicit\n- REQ-001:" in rendered
    assert "Do not rely on access to a previous provider conversation" in " ".join(rendered.split())
    for case in cases:
        case_prompt = module.render({"vars": case["vars"]})
        assert "CONTROLLER CORRECTION REFERENCES:\nnull\n" in case_prompt
        assert "$correction_references" not in case_prompt

    record = json.loads((target / "execution-record.json").read_text())
    assert record["local_real_model_executed"] is False
    assert record["local_outcome"] == "NOT_RUN"
    assert "did not execute a real semantic model" in record["local_reason"]
    assert record["observed_ci_execution"]["real_model_executed"] is True
    # The immutable historical run predates the correction-eligibility case.
    assert record["observed_ci_execution"]["executed_cases"] == 3
    assert set(record["observed_ci_execution"]["observations"]) == {
        "fixed-original-oracle-converges",
        "unrelated-variant-and-parent-demand-are-not-blockers",
        "correction-induced-material-defect-blocks",
    }
    assert "local-required-regression-fix-retains-input-head-finding" not in record["observed_ci_execution"]["observations"]


def test_prompt_key_and_shared_dependencies_select_only_affected_targets(tmp_path: Path) -> None:
    targets = [target("a", ["a.prompt", "shared"]), target("b", ["b.prompt", "shared"]), target("c", ["b.prompt"])]
    repo, base = setup_repo(tmp_path, targets)
    for name in ("a", "b", "c"):
        directory = repo / f"prompt-evals/targets/{name}"
        (directory / "cases").mkdir(parents=True)
        (directory / "promptfooconfig.yaml").write_text("description: test\n")
        (directory / "cases/case.yaml").write_text("tests: [{vars: {id: smoke}}]\n")
    commit(repo, "register fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "src/prompts.yaml").write_text("a: {prompt: changed}\nb: {prompt: original}\nshared: original\n")
    commit(repo, "change a")
    log = repo / "npx.log"
    os.environ["NPX_LOG"] = str(log)
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout
    assert "target: b" not in result.stdout
    assert "target: c" not in result.stdout
    assert log.read_text().count("promptfoo@latest") == 1

    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "src/prompts.yaml").write_text("a: {prompt: changed}\nb: {prompt: original}\nshared: changed\n")
    commit(repo, "change shared")
    log.unlink()
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout and "target: b" in result.stdout
    assert "target: c" not in result.stdout
    assert log.read_text().count("promptfoo@latest") == 2


def test_flat_dotted_shared_dependency_selects_only_consumers(tmp_path: Path) -> None:
    targets = [target("a", ["shared.safety"]), target("b", ["shared.safety"]), target("independent", ["a.prompt"])]
    repo, base = setup_repo(tmp_path, targets)
    for name in ("a", "b", "independent"):
        directory = repo / f"prompt-evals/targets/{name}"
        (directory / "cases").mkdir(parents=True)
        (directory / "promptfooconfig.yaml").write_text("description: test\n")
        (directory / "cases/case.yaml").write_text("tests: [{vars: {id: smoke}}]\n")
    commit(repo, "evaluation fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "src/prompts.yaml").write_text("a: {prompt: original}\nb: {prompt: original}\nshared: original\nshared.safety: changed\n")
    commit(repo, "change flat shared prompt")
    log = repo / "npx.log"
    os.environ["NPX_LOG"] = str(log)
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout and "target: b" in result.stdout
    assert "target: independent" not in result.stdout
    assert log.read_text().count("promptfoo@latest") == 2


def test_renamed_dependency_is_not_silently_ignored(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path, [target("a", ["a.prompt"])])
    directory = repo / "prompt-evals/targets/a"
    (directory / "cases").mkdir(parents=True)
    (directory / "promptfooconfig.yaml").write_text("description: test\n")
    (directory / "cases/case.yaml").write_text("tests: [{vars: {id: smoke}}]\n")
    commit(repo, "evaluation fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "src/prompts.yaml").rename(repo / "src/renamed.yaml")
    commit(repo, "rename prompt dependency")
    log = repo / "npx.log"
    os.environ["NPX_LOG"] = str(log)
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout
    assert log.read_text().count("promptfoo@latest") == 1


def test_recursive_corpus_change_selects_owning_target(tmp_path: Path) -> None:
    recursive = target("a", ["a.prompt"])
    recursive["cases"] = ["prompt-evals/targets/a/cases/**/*.yaml"]
    repo, base = setup_repo(tmp_path, [recursive, target("b", ["b.prompt"])])
    for name in ("a", "b"):
        directory = repo / f"prompt-evals/targets/{name}"
        (directory / "cases/deep/nested").mkdir(parents=True)
        (directory / "promptfooconfig.yaml").write_text("description: test\n")
        (directory / "cases/deep/nested/case.yaml").write_text("tests: [{vars: {id: smoke}}]\n")
    commit(repo, "evaluation fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "prompt-evals/targets/a/cases/deep/nested/case.yaml").write_text("tests: [{vars: {changed: true}}]\n")
    commit(repo, "change nested case")
    log = repo / "npx.log"
    os.environ["NPX_LOG"] = str(log)
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout
    assert "target: b" not in result.stdout
    assert log.read_text().count("promptfoo@latest") == 1


@pytest.mark.parametrize(
    ("pattern", "hidden_path"),
    [
        ("prompt-evals/targets/a/cases/*.yaml", ".notes.yaml"),
        ("prompt-evals/targets/a/cases/**/*.yaml", ".draft/note.yaml"),
    ],
)
def test_hidden_files_excluded_by_corpus_glob_do_not_select_target(tmp_path: Path, pattern: str, hidden_path: str) -> None:
    registered = target("a", ["a.prompt"])
    registered["cases"] = [pattern]
    repo, base = setup_repo(tmp_path, [registered])
    directory = repo / "prompt-evals/targets/a"
    (directory / "cases").mkdir(parents=True)
    (directory / "promptfooconfig.yaml").write_text("tests: cases/case.yaml\n")
    (directory / "cases/case.yaml").write_text("tests: [{vars: {id: smoke}}]\n")
    hidden = directory / "cases" / hidden_path
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text("draft: original\n")
    commit(repo, "evaluation fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    hidden.write_text("draft: changed\n")
    commit(repo, "change excluded draft")
    result = invoke(repo, base, repo / "missing-npx")
    assert result.returncode == 0
    assert "No prompt-evaluation targets affected" in result.stdout
    assert "Evaluating affected target" not in result.stdout


def test_corpus_change_runs_promptfoo_and_propagates_failure(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path, [target("a", ["a.prompt"])])
    directory = repo / "prompt-evals/targets/a"
    (directory / "cases").mkdir(parents=True)
    (directory / "promptfooconfig.yaml").write_text("description: test\n")
    commit(repo, "config")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (directory / "cases/new.yaml").write_text("tests: [{vars: {id: smoke}}]\n")
    commit(repo, "case")
    os.environ["NPX_LOG"] = str(repo / "npx.log")
    os.environ["NPX_EXIT"] = "7"
    result = invoke(repo, base, fake_npx(repo))
    os.environ.pop("NPX_EXIT")
    assert result.returncode == 7
    assert "target: a" in result.stdout


def test_selected_empty_executable_set_is_not_run_without_credentials(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path, [target("a", ["a.prompt"])])
    directory = repo / "prompt-evals/targets/a"
    (directory / "cases").mkdir(parents=True)
    (directory / "promptfooconfig.yaml").write_text("description: test\n")
    commit(repo, "config")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (directory / "cases/empty.yaml").write_text("tests: []\n")
    commit(repo, "empty corpus")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    report = repo / "report.json"

    result = subprocess.run(
        [
            "python",
            str(RUNNER),
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--npx",
            str(repo / "missing-npx"),
            "--report",
            str(report),
            "--require-credentials",
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    record = json.loads(report.read_text())
    assert record["base_sha"] == base
    assert record["head_sha"] == head
    assert record["selected_targets"] == ["a"]
    assert record["targets"] == [
        {
            "executed_cases": 0,
            "expected_cases": 0,
            "failures": [],
            "errors": [],
            "promptfoo_version": "",
            "id": "a",
            "outcome": "NOT_RUN",
            "reason": "Selected corpus has no executable cases; no provider was invoked.",
        }
    ]


def test_malformed_affected_dependency_fails_closed(tmp_path: Path) -> None:
    broken = target("a", ["a.prompt"])
    broken["prompt_dependencies"] = [{"path": "src/prompts.yaml", "keys": "a.prompt"}]
    repo, base = setup_repo(tmp_path, [broken])
    (repo / "src/prompts.yaml").write_text("a: {prompt: changed}\n")
    commit(repo, "prompt")
    result = invoke(repo, base, repo / "missing-npx")
    assert result.returncode == 2
    assert "failed closed" in result.stderr


def selected_repo(tmp_path: Path, cases: int = 1) -> tuple[Path, str]:
    repo, _ = setup_repo(tmp_path, [target("a", ["a.prompt"])])
    directory = repo / "prompt-evals/targets/a"
    (directory / "cases").mkdir(parents=True)
    (directory / "promptfooconfig.yaml").write_text("description: fixture\n")
    (directory / "cases/case.yaml").write_text(yaml.safe_dump({"tests": [{"vars": {"id": f"case-{index}"}} for index in range(cases)]}))
    base = commit(repo, "register evaluation cases")
    (repo / "src/prompts.yaml").write_text("a: {prompt: changed}\n")
    commit(repo, "change selected prompt")
    return repo, base


def result_document(rows: list[dict[str, object]]) -> dict[str, object]:
    # Some real error rows are absent from Promptfoo's aggregate error count.
    return {"metadata": {"promptfooVersion": "0.126.0"}, "results": {"results": rows, "stats": {"errors": 0}}}


def malformed_grading_row(component_results: object) -> dict[str, object]:
    row = completed_row(passed=False)
    row["gradingResult"] = {"pass": False, "componentResults": component_results}
    return row


def incomplete_response_row(passed: bool, status: str = "incomplete") -> dict[str, object]:
    row = completed_row(passed=passed)
    row["response"]["raw"] = {"object": "response", "status": status, "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None}
    return row


def assertion_execution_error_row() -> dict[str, object]:
    row = completed_row()
    row["success"] = False
    row["failureReason"] = 1
    reason = "Custom function threw error: fixture failure\nStack Trace: fixture"
    row["error"] = reason
    row["gradingResult"]["pass"] = False
    row["gradingResult"]["reason"] = reason
    row["gradingResult"]["componentResults"][-1].update({"pass": False, "reason": reason})
    return row


@pytest.mark.parametrize(
    ("rows", "expected_cases", "evaluator_exit", "outcome", "failure_ids"),
    [
        pytest.param([completed_row()], 1, 0, "PASS", [], id="complete-pass"),
        pytest.param([completed_row(passed=False)], 1, 100, "MISMATCH", ["semantic-case"], id="semantic-error-field-is-mismatch"),
        pytest.param([provider_error_row()], 1, 100, "ERROR", [], id="authentication-error-is-not-mismatch"),
        pytest.param([completed_row(passed=False), provider_error_row()], 2, 100, "ERROR", ["semantic-case"], id="mixed-error-retains-mismatch"),
        pytest.param([completed_row(passed=False)], 2, 100, "ERROR", ["semantic-case"], id="partial-run-retains-mismatch"),
        pytest.param([incomplete_response_row(True)], 1, 0, "ERROR", [], id="incomplete-response-cannot-pass"),
        pytest.param([incomplete_response_row(True, "cancelled")], 1, 0, "ERROR", [], id="cancelled-response-cannot-pass"),
        pytest.param([incomplete_response_row(False)], 1, 100, "ERROR", ["semantic-case"], id="incomplete-response-retains-evidence"),
        pytest.param([assertion_execution_error_row()], 1, 100, "ERROR", [], id="javascript-exception-is-error"),
        pytest.param([malformed_grading_row([])], 1, 100, "ERROR", [], id="empty-grading-is-error"),
        pytest.param([malformed_grading_row(None)], 1, 100, "ERROR", [], id="null-grading-is-error"),
        pytest.param([completed_row(passed=False)], 1, 7, "ERROR", ["semantic-case"], id="abnormal-exit-outranks-mismatch"),
        pytest.param([completed_row()], 1, 100, "ERROR", [], id="failed-process-cannot-pass"),
        pytest.param([{"success": False, "failureReason": 0, "error": "No output", "response": {"output": ""}, "gradingResult": None}], 1, 100, "ERROR", [], id="empty-provider-output-is-error"),
    ],
)
def test_advisory_outcomes_distinguish_execution_errors_from_semantic_mismatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]], expected_cases: int, evaluator_exit: int, outcome: str, failure_ids: list[str]) -> None:
    repo, base = selected_repo(tmp_path, expected_cases)
    log = repo / "npx.log"
    monkeypatch.setenv("NPX_LOG", str(log))
    report = repo / "advisory/report.json"

    result = invoke(repo, base, fake_npx(repo, result_document(rows), evaluator_exit), "--report", str(report))

    assert (result.returncode == 0) == (outcome == "PASS"), result.stderr
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == outcome
    assert record["executed_cases"] == len(rows)
    assert record["expected_cases"] == expected_cases
    assert record["promptfoo_version"] == "0.126.0"
    assert [failure["case_id"] for failure in record["failures"]] == failure_ids
    assert bool(record["errors"]) == (outcome == "ERROR")
    if failure_ids:
        failure = record["failures"][0]
        assert failure["assertion_id"] == "javascript"
        assert failure["expected"] == "JSON.parse(output).ok === true"
        assert failure["actual"] == '{"ok":false}'
    invocation = log.read_text()
    assert "promptfoo@latest" in invocation
    assert "--no-cache" in invocation
    assert "--max-concurrency 2" in invocation


@pytest.mark.parametrize("document", [{"results": None}, {"results": {"results": None}}, {"results": {"results": [None]}}, {"results": {"results": [{"success": True}]}}])
def test_malformed_evaluator_results_are_recorded_as_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: object) -> None:
    repo, base = selected_repo(tmp_path)
    monkeypatch.delenv("NPX_LOG", raising=False)
    report = repo / "advisory/report.json"

    result = invoke(repo, base, fake_npx(repo, document, 0), "--report", str(report))

    assert result.returncode != 0
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == "ERROR"
    assert record["errors"]
    assert record["failures"] == []
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("api_key", "untrusted", "cases", "outcome", "invoked"),
    [
        pytest.param("fixture-api-key", False, 1, "PASS", True, id="api-key-is-accepted"),
        pytest.param("", False, 1, "ERROR", False, id="codex-auth-is-not-api-credential"),
        pytest.param("   ", False, 1, "ERROR", False, id="blank-api-key-is-missing"),
        pytest.param("", True, 1, "NOT_RUN", False, id="fork-never-needs-credentials"),
        pytest.param("", False, 0, "NOT_RUN", False, id="empty-corpus-never-needs-credentials"),
    ],
)
def test_api_credentials_and_noop_paths_never_touch_local_codex_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api_key: str, untrusted: bool, cases: int, outcome: str, invoked: bool) -> None:
    repo, base = selected_repo(tmp_path, cases)
    local_home = tmp_path / "operator-home"
    auth = local_home / ".codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"local_session":"operator-owned"}\n')
    auth.chmod(0o600)
    before = auth.stat()
    monkeypatch.setenv("HOME", str(local_home))
    monkeypatch.setenv("OPENAI_API_KEY", api_key)
    monkeypatch.setenv("CODEX_AUTH_JSON", '{"legacy_session":"must-not-be-used"}')
    log = repo / "npx.log"
    monkeypatch.setenv("NPX_LOG", str(log))
    report = repo / "advisory/report.json"
    args = ["--require-credentials", "--report", str(report)]
    if untrusted:
        args.append("--untrusted")

    result = invoke(repo, base, fake_npx(repo, exit_code=0), *args)

    assert (result.returncode == 0) == (outcome != "ERROR"), result.stderr
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == outcome
    assert log.exists() == invoked
    assert auth.read_text() == '{"local_session":"operator-owned"}\n'
    assert auth.stat().st_mtime_ns == before.st_mtime_ns
    assert auth.stat().st_mode == before.st_mode


def test_api_evaluation_does_not_create_codex_auth_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base = selected_repo(tmp_path)
    local_home = tmp_path / "operator-home"
    local_home.mkdir()
    monkeypatch.setenv("HOME", str(local_home))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-api-key")
    monkeypatch.setenv("CODEX_AUTH_JSON", '{"legacy_session":"unused"}')
    monkeypatch.delenv("NPX_LOG", raising=False)

    result = invoke(repo, base, fake_npx(repo, exit_code=0), "--require-credentials")

    assert result.returncode == 0, result.stderr
    assert not (local_home / ".codex").exists()


def test_unselected_run_preserves_auth_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base = setup_repo(tmp_path, [])
    local_home = tmp_path / "operator-home"
    auth = local_home / ".codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"local_session":"keep"}\n')
    monkeypatch.setenv("HOME", str(local_home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_AUTH_JSON", '{"legacy_session":"unused"}')
    report = repo / "report.json"

    result = invoke(repo, base, repo / "missing-npx", "--require-credentials", "--report", str(report))

    assert result.returncode == 0, result.stderr
    record = json.loads(report.read_text())
    assert record["selected_targets"] == []
    assert record["targets"] == []
    assert "error" not in record
    assert auth.read_text() == '{"local_session":"keep"}\n'


def test_failed_evaluator_cannot_reuse_stale_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base = selected_repo(tmp_path)
    monkeypatch.delenv("NPX_LOG", raising=False)
    report = repo / "advisory/report.json"
    report.parent.mkdir()
    old_result = report.parent / "promptfoo-a.json"
    old_result.write_text(json.dumps(result_document([completed_row(passed=False)])))

    result = invoke(repo, base, fake_npx(repo, exit_code=100, emit_output=False), "--report", str(report))

    assert result.returncode != 0
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == "ERROR"
    assert record["executed_cases"] == 0
    assert record["failures"] == []
    assert record["errors"]
    assert not old_result.exists()


def test_missing_evaluator_is_reported_without_traceback(tmp_path: Path) -> None:
    repo, base = selected_repo(tmp_path)
    report = repo / "advisory/report.json"

    result = invoke(repo, base, repo / "missing-npx", "--report", str(report))

    assert result.returncode != 0
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == "ERROR"
    assert record["executed_cases"] == 0
    assert record["errors"]
    assert "Traceback" not in result.stderr


def test_preparation_failure_does_not_erase_or_skip_other_target_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _ = setup_repo(tmp_path, [target("broken", ["shared"]), target("healthy", ["shared"])])
    for name in ("broken", "healthy"):
        directory = repo / f"prompt-evals/targets/{name}"
        (directory / "cases").mkdir(parents=True)
        (directory / "cases/case.yaml").write_text("tests: [{vars: {id: semantic-case}}]\n")
    (repo / "prompt-evals/targets/healthy/promptfooconfig.yaml").write_text("description: fixture\n")
    base = commit(repo, "register targets with missing config")
    (repo / "src/prompts.yaml").write_text("shared: changed\n")
    commit(repo, "change shared prompt")
    log = repo / "npx.log"
    monkeypatch.setenv("NPX_LOG", str(log))
    report = repo / "advisory/report.json"

    result = invoke(repo, base, fake_npx(repo, exit_code=0), "--report", str(report))

    assert result.returncode != 0
    records = json.loads(report.read_text())["targets"]
    assert [(record["id"], record["outcome"]) for record in records] == [("broken", "ERROR"), ("healthy", "PASS")]
    assert records[0]["errors"]
    assert records[1]["executed_cases"] == 1
    assert log.read_text().count("promptfoo@latest") == 1


@pytest.fixture
def prompt_eval_runner(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("prompt_eval_runner_test", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_evaluator_timeout_produces_error_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prompt_eval_runner: ModuleType) -> None:
    repo, base = selected_repo(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    module = prompt_eval_runner
    original_run = subprocess.run
    evaluator = str(repo / "timeout-npx")

    def expire_evaluator(command: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == evaluator:
            assert kwargs["timeout"] == 600
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", expire_evaluator)
    report = repo / "advisory/report.json"

    result = module.run(repo, base, head, evaluator, report)

    assert result != 0
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == "ERROR"
    assert record["errors"]
    assert record["executed_cases"] == 0


def test_missing_executed_version_cannot_claim_semantic_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base = selected_repo(tmp_path)
    monkeypatch.delenv("NPX_LOG", raising=False)
    document = {"results": {"results": [completed_row()]}}
    report = repo / "advisory/report.json"

    result = invoke(repo, base, fake_npx(repo, document, 0), "--report", str(report))

    assert result.returncode != 0
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == "ERROR"
    assert record["promptfoo_version"] == ""
    assert "Promptfoo version" in record["reason"]


def test_registered_corpora_count_every_executable_case(prompt_eval_runner: ModuleType) -> None:
    registry = json.loads((REPOSITORY_ROOT / "prompt-evals/registry.json").read_text())
    counts = {}
    for registered in registry["targets"]:
        files = sorted({path for pattern in registered["cases"] for path in glob.glob(str(REPOSITORY_ROOT / pattern), recursive=True)})
        counts[registered["id"]] = prompt_eval_runner._case_count(files)

    assert counts == {
        "task-scoped-test-execution": 3,
        "chatgpt-review-adjudication": 6,
        "individual-objective-scope": 2,
        "decomposition-objective-scope": 5,
        "short-objective-contract": 5,
        "parent-child-contract-boundary": 5,
        "jules-autonomy-checkpoint-recovery": 7,
        "test-oracle-gap-completion": 4,
        "incremental-pr-rereview": 4,
    }


def test_top_level_yaml_case_list_is_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base = selected_repo(tmp_path)
    cases = repo / "prompt-evals/targets/a/cases/case.yaml"
    cases.write_text("- vars: {id: semantic-case}\n")
    commit(repo, "use supported top-level case list")
    log = repo / "npx.log"
    monkeypatch.setenv("NPX_LOG", str(log))
    report = repo / "advisory/report.json"

    result = invoke(repo, base, fake_npx(repo, exit_code=0), "--report", str(report))

    assert result.returncode == 0, result.stderr
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == "PASS"
    assert record["expected_cases"] == 1
    assert record["executed_cases"] == 1
    assert log.read_text().count("promptfoo@latest") == 1


@pytest.mark.parametrize("corpus", ["description: no test list\n", "tests: invalid\n", "null\n"])
def test_unrecognized_corpus_shape_is_error_not_empty_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus: str) -> None:
    repo, base = selected_repo(tmp_path)
    (repo / "prompt-evals/targets/a/cases/case.yaml").write_text(corpus)
    commit(repo, "malformed selected corpus")
    log = repo / "npx.log"
    monkeypatch.setenv("NPX_LOG", str(log))
    report = repo / "advisory/report.json"

    result = invoke(repo, base, fake_npx(repo, exit_code=0), "--report", str(report))

    assert result.returncode != 0
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == "ERROR"
    assert record["errors"]
    assert not log.exists()


def test_invalid_utf8_evaluator_artifact_retains_error_report(tmp_path: Path) -> None:
    repo, base = selected_repo(tmp_path)
    evaluator = repo / "invalid-utf8-npx"
    evaluator.write_text("#!/usr/bin/env python\n" "import sys\n" "from pathlib import Path\n" "Path(sys.argv[sys.argv.index('--output') + 1]).write_bytes(bytes([255, 254]))\n")
    evaluator.chmod(0o755)
    report = repo / "advisory/report.json"

    result = invoke(repo, base, evaluator, "--report", str(report))

    assert result.returncode != 0
    record = json.loads(report.read_text())["targets"][0]
    assert record["outcome"] == "ERROR"
    assert record["errors"]
    assert record["executed_cases"] == 0
    assert record["failures"] == []
    assert "Traceback" not in result.stderr
