# Optional Aider Python API

Auto-Coder and its container do not install `aider-chat`. There is no Aider
extra, dependency declaration, install step or lockfile edge. The default Codex
backend, ordinary and no-edit orders, and normal CLI startup do not require it.
Aider modules are imported only when an Aider client is explicitly constructed.

Operators who explicitly select `aider` or an alias with
`backend_type = "aider"` can manually install Aider into the Python environment
running Auto-Coder. Use a separate operator-managed Python 3.12 environment with
both packages installed explicitly. An Aider executable in another tool's
environment is insufficient: this integration uses the Python scripting API,
not a CLI subprocess. Repository `uv sync` deliberately installs only its lock
and can remove manually added undeclared packages; it is not an Aider installer.

Without Aider, native startup and default backend selection are unchanged.
An explicit Aider prerequisite check uses `find_spec` without importing the
package and fails with manual-installation guidance when absent. Constructing
an unavailable primary Aider client also fails clearly. During an existing
configured rotation, missing Aider initialization is skipped and the next
configured backend is tried. No default or fallback order is broadened.

The adapter retains Aider's existing Python scripting path:
`Model`, `InputOutput`, `Coder.create` and `Coder.run`. Upstream illustrates
this path but does not guarantee API compatibility between releases; offline
checks here verify Aider 0.86.2 on Python 3.12. See the
[upstream scripting guidance](https://aider.chat/docs/scripting.html). The prompt is passed in
memory, never in command arguments or a prompt file. Aider's terminal output is
captured as diagnostics; the return value of `Coder.run` is the model answer.
Diagnostics are never concatenated with that answer or used as an empty-answer
fallback. A missing, empty or whitespace-only return fails explicitly. Usage
limit diagnostics remain failures. Exceptions restore the prior environment
and are recorded as errors in the existing interaction log. That intentional
interaction log retains its established prompt/response logging behavior;
the adapter adds no generic command log or process-argument exposure.

Normal calls retain the model's normal edit format; no-edit calls explicitly
select `ask` and `dry_run=True`. Runtime switching back to a normal call restores
`dry_run=False` and the normal edit format. A client constructed for no-edit
retains that restriction. Auto-commits and dirty commits are disabled because
Auto-Coder owns Git lifecycle operations. Existing configured option-to-environment
mapping and one-time extra-argument consumption are retained.

The direct `python-dateutil` dependency preserves existing issue, Jules and
quota imports formerly satisfied transitively by Aider. Aider-only numerical
overrides and the audioop shim are removed. This change does not alter processing
origins, admission, provider routing, durable resumption or trace schemas;
no dashboard observability changes are required.
