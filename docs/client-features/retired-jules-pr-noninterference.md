# Retired Jules PR non-interference

Jules sessions no longer own pull-request waiting, CI repair, conflict repair,
body rewriting, timeout closure, attempt increments, draft promotion, or
post-merge cleanup. Historical Jules-authored PRs remain ordinary bot/non-local
PRs: local-looking branch names, titles, Auto-Coder prose, and quoted Jules URLs
do not grant local repair authority.

Codex Cloud and Claude Routine retain their provider bindings and exact-head
repair routes. Local PRs still require established local publication evidence,
and dependency bots remain governed by the dependency-bot gates. Shared session
parsing accepts supported Claude and Codex forms but no longer extracts retired
Jules session or task URLs.

The speculative-artifact lifecycle remains an independent negative fence at PR
ingress and immediately before merge. An exact historical provider-bot author
may therefore trigger conservative fence evaluation, but it does not create an
active session, repair route, body-link authority, or local ownership.

Removing the Jules wait and repair stages is observability-neutral for the
remaining routes: no replacement stage or outcome is emitted. Existing Codex,
Claude, local, dependency-bot, CI, review, and merge events retain their schemas;
`pr.speculative-jules-authority` and
`pr.speculative-jules-merge-authority` remain for the historical artifact fence.

Focused regressions are in
`tests/test_retired_jules_pr_noninterference.py`.
