# Historical controller identity

Controller protocol evidence remains attributable after a repository changes its
controller GitHub account. Configure the complete set of previous controller logins
in the repository override at `~/.auto-coder/<owner>/<repo>/config.toml`:

```toml
[github-controller-identity-history]
prior_logins = ["old-controller"]
```

GitHub resolves every configured login to its canonical login and positive account
ID. Auto-Coder matches that identity to the actual comment author separately from
validating the receipt, marker, operation, thread, head, or feedback digest. It does
not infer controller status from protocol text, a bot suffix, repository ownership,
or the currently configured App.

In controller-App mode, an absent table means historical association is unknown.
An explicit empty list is a complete declaration that there were no earlier
controllers. A nonempty list is the complete declaration for this repository and
GitHub API origin. Legacy personal-token repositories without an App configuration
or declaration retain current-author-only behavior.

If a relevant historical protocol candidate is present while history or account
resolution is unavailable, the affected operation remains unavailable. Operators
must correct the repository declaration or account lookup before recovery proceeds;
Auto-Coder does not hide the old evidence with a replacement receipt or resend
provider feedback. Fresh operations with no relevant historical candidate continue
normally.
