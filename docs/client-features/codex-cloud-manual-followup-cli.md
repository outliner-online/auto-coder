# Manual Codex Cloud Legacy follow-up CLI

The maintained scripts/codex-cloud-followup utility posts a message to an
existing Codex Cloud Legacy task (task_e_...) through the same internal
WHAM endpoint used by CodexWhamClient. It is a manual operator utility, not a
replacement for Auto-Coder's production follow-up journal, PR ownership checks,
or CI/review orchestration. No new task is created.

## Dependencies and authentication

Run on a machine with Bash, curl, jq, flock, sha256sum, and the Codex CLI
installed. Sign in with codex login first. The script reads the
Codex-managed ~/.codex/auth.json (or CODEX_HOME/auth.json) directly and
never uses an OpenAI API key. It does not print the OAuth token. Keep auth.json
private and do not copy its contents into logs, Issues, or PRs.

WHAM (https://chatgpt.com/backend-api/wham) is an undocumented ChatGPT
backend API. It may change without notice; this utility is intentionally
separate from the publicly supported codex cloud commands.

## Commands

Send an explicit, unconditional message to an existing task:

    bash scripts/codex-cloud-followup task_e_123456 "Please review PR #123 and continue."

Send a continuation only when the CLI explicitly reports that task as paused
or waiting; the message is optional:

    bash scripts/codex-cloud-followup --if-paused task_e_123456
    bash scripts/codex-cloud-followup --if-paused task_e_123456 "Continue the existing PR."

The default guarded message asks Codex to review the current PR, repair failing
CI, push fixes to that PR, and address valid unresolved review comments.

## Guarded behavior

--if-paused looks up the exact task ID in paginated codex cloud list --json
results. It only proceeds for paused, waiting_for_input, idle, or stopped.
In particular, pending, queued, running, ready, error, failed, unknown states,
missing task IDs, and CLI errors never send.

Before sending, the script reads the task's WHAM turn history. It requires the
last chronological turn to belong to that task, be an assistant turn, and have
a completed or otherwise continuable status. A latest in_progress turn, a
newer user turn, malformed history, or unavailable credentials blocks sending.

For the guarded mode, a task-specific flock prevents competing local
invocations. A marker under XDG_STATE_HOME/codex-cloud-followup (default:
~/.local/state/codex-cloud-followup) records the assistant turn before the
POST. A repeat invocation for the same turn is skipped, even if the earlier
POST timed out or returned an ambiguous HTTP error. Only an observed new
assistant turn permits another guarded send. These local markers are not a
distributed/idempotent provider contract.

This is a one-shot check, not a polling daemon. No force override is offered.
If status disagrees between CLI and WHAM or changes during the request, a
provider-atomic pause check is not available; the guarded path rejects known
contradictory evidence but cannot eliminate all races. An HTTP acceptance
response means the message was accepted, not that Codex finished the work
or that a PR is ready to merge.

The unconditional form is deliberately user-directed and does not impose
the paused-state or local deduplication guard. It should not be called
automatically in a retry loop. In either mode, never resend automatically
after timeouts, 409/429 errors, or server errors: a response can be lost
after the provider accepted a non-idempotent request.
