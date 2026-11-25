# Required local test execution

Commands that use local validation verify the configured `TEST_SCRIPT_PATH` at
startup and fail clearly when it is absent. Shared local test and fix-to-pass
workflows always use the configured script's real observations; the retired
`AUTOCODER_JULES_ONLY_MODE` setting cannot skip execution, synthesize progress,
or remove the finite `MAX_FIX_ATTEMPTS` bound.

Task-only Codex Cloud and Claude Routine issue startup remains eligible without
a local test script. That remote admission path does not enter the shared local
test or fix-to-pass workflows.
