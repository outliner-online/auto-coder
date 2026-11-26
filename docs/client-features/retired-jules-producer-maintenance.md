# Retired Jules producer maintenance

The production producer and explicit batch loop no longer enumerate, resume,
stop, archive, or create Jules sessions. They also do not consume speculative
Jules work or repository-local recurrent Jules prompt files. Historical session,
competition, and recurrent-task records remain available for inspection; their
presence is not fresh dispatch authority and is never converted to another
provider.

A confirmed merge of a historical Jules PR leaves its session untouched and
does not restart a matching recurrent prompt. Conflict rejection may retain
the established PR-closing outcome, but it neither resolves an Issue from the
old session binding nor advances or re-enters Issue work from that history.
Failing CI and definitive merge conflicts likewise remain non-authorizing:
daemon PR processing does not send repair messages to historical Jules
sessions. Closing an empty historical Jules PR may retry an Issue associated by
an independent branch name or title, but a persisted session-to-Issue binding
cannot advance or queue that Issue. Daemon processing also never converts such
a binding into a closing link in the PR body, so provider history cannot become
apparent explicit retry authority in a later processing cycle. Empty historical
Jules PRs also ignore unmarked closing references already saved by the retired
linker because their session-derived provenance cannot be distinguished after
persistence; ordinary supported-provider PR body links retain their existing
retry behavior.

Provider-neutral workspace reclamation, update checks, the producer's fixed
maintenance sleep, and PR merge/close wake-up remain independent of retired
Jules state. Ordinary Issue admission continues to select supported Codex Cloud,
Claude Routine, or local backends through the unified Issue dispatch policy.
