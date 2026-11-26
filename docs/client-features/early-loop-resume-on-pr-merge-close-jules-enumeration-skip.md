# Early Producer Wake on PR Merge/Close

  producer_early_wake_on_pr_event:
    description: "Cuts short the main producer loop sleep duration (60s) upon PR merge or close events and resumes provider-neutral maintenance immediately."
    implementation: |
      notify_pr_merged_or_closed, _sleep_or_wake, _check_if_pr_merged_or_closed,
      _producer_loop, _worker_loop in src/auto_coder/automation_engine.py,
      process_github_payload in src/auto_coder/webhook_server.py
    behavior:
      - "When a PR is merged or closed (internally by a worker, via branch cleanup, or externally received via GitHub webhook), signals the engine to cut short the active wait time in _producer_loop."
      - "The wake changes only when the next producer iteration begins; it does not authorize provider work."
      - "Every resumed iteration still performs the update check, but generic recurring maintenance never synchronizes or repairs the monitored checkout; checkout mutation remains owned by explicit branch-processing operations."
      - "Timer-driven and PR-woken iterations use the same maintenance path, so repeated wake-ups do not introduce a provider-specific gate or a busy loop."
