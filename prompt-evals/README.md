# Prompt regression evaluations

This directory is the stable home of selective Promptfoo evaluations. Live
semantic results are advisory evidence for humans and are never ordinary CI,
merge, or automatic-repair prerequisites.

## Registering a target

Add an entry to `registry.json`:

```json
{
  "id": "pull-request-review",
  "prompt_dependencies": [
    {"path": "src/auto_coder/prompts.yaml", "keys": ["pr.review", "shared.safety"]}
  ],
  "config": "prompt-evals/targets/pull-request-review/promptfooconfig.yaml",
  "cases": ["prompt-evals/targets/pull-request-review/cases/*.yaml"]
}
```

Dependencies without `keys` select the target whenever the file changes. YAML
dependencies with dot-addressable keys are compared at those keys between the
base and head revisions. Declaring the same shared key for multiple targets
therefore fans a shared change out only to those targets. Changes beneath a
target's config path or matching one of its case globs also select it.

Write JavaScript assertions containing statements as literal multiline YAML
blocks, with a newline before the top-level return. Promptfoo treats a
single-line value as an expression; a one-line statement body can therefore
produce an assertion execution error. Expression-only predicates can remain
single-line.

Selected targets with no executable cases are reported as `NOT_RUN` before
credentials are needed or Promptfoo is invoked. Selected targets with cases run
their own config using `promptfoo@latest`; a complete
semantic failure is `MISMATCH`, a complete success is `PASS`, and operational,
partial, or malformed output is `ERROR`. The CLI remains nonzero for mismatch
and error even though controller policy treats the workflow as advisory.
Provider/authentication failures, assertion execution exceptions, incomplete or
cancelled Responses API output, and missing or partial result rows are execution
errors even if other assertions passed or failed. The report retains observed
semantic failures alongside errors, and lists the assertion that actually failed.
Registry, runner, or workflow changes select every registered target, making
evaluation-infrastructure changes fail closed rather than silently omitting a
suite.

Run directly with explicit revisions:

```console
python prompt-evals/run_prompt_evals.py --base <commit> --head <commit> --report report.json
```

Actions pull-request runs bind to the event's immutable base/head. Manual runs
accept `base` and `head`; omitted base means the resolved head's first parent.
Fork PR content never receives model credentials and is reported `NOT_RUN`.
Every attempt retains a structured artifact and Actions summary. The artifact
includes `report.json` and the original per-target Promptfoo JSON files. Each
target records the actual executable version from that output's
`metadata.promptfooVersion`, so changes while following `latest` remain
traceable. A missing output/version is never represented as a successful run. `MISMATCH` and
`ERROR` attempts associated with exactly one PR may also receive an immutable
`github-actions[bot]` top-level comment. The comments state that no
acknowledgement or resolution is required.

Deploy controller advisory isolation before activating these workflows. Neither
workflow context may be configured as required. An authorized operator must
recheck branch protection/rulesets and remove legacy required contexts before
activation; this code intentionally does not modify those server-side settings.

## API credentials and execution

Create a GitHub Actions repository secret named `OPENAI_API_KEY` for the OpenAI
API project used for evaluations. The workflow passes it directly to Promptfoo;
no Codex login, ChatGPT refresh token, or local `auth.json` is used, written, or
deleted. `CODEX_AUTH_JSON` is no longer an evaluator credential. The retired
startup publisher and external-launcher migration are documented in
[`one-way-codex-authentication-publication.md`](../docs/client-features/one-way-codex-authentication-publication.md).

All registered targets call the OpenAI Responses API using
`openai:responses:gpt-6.1-terra`, `reasoning.effort: high`,
`max_output_tokens: 8192`, and `store: false`. They require API access to that
model. Assertions remain deterministic JavaScript/JSON checks; there is no
additional model-based grader. API usage is charged to the configured API
project.

For a local live run, export `OPENAI_API_KEY` in the shell and use:

```console
python prompt-evals/run_prompt_evals.py --base <commit> --head <commit> --report .agent-tmp/prompt-eval-report/report.json --require-credentials
```

The runner selects work and checks for an executable corpus before requiring the
key or installing Promptfoo. An untrusted fork never invokes the provider.
Nonempty trusted work with a missing key is `ERROR`. Both top-level YAML case
lists and the `tests` list used by generated corpora are counted. The runner
downloads `promptfoo@latest` through `npx` for selected work rather than pinning
an npm release. Evaluations disable result caching, allow at most two simultaneous
cases per target, and time out after ten minutes per target; the Actions job
retains its thirty-minute overall timeout. An incompatible future output format
is reported as `ERROR`, not a semantic regression or a false pass.

Model and transport settings are independently versioned in each target's
configuration. A dependency update can change evaluator behavior; use the
recorded Promptfoo version and raw output when comparing advisory attempts.
These records do not turn semantic evaluation into a merge or repair gate.
