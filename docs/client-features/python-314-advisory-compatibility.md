# Advisory Python 3.14 compatibility evidence

The independent `Python 3.14 Compatibility (Advisory)` GitHub Actions
workflow runs for pull requests changing its workflow or shared dependency
policy inputs, manual dispatch and a weekly check. Ordinary application-only
pull requests do not incur this separate, potentially expensive advisory job. It
uses the same supported `astral-sh/setup-uv@v7` and pinned uv `0.7.22`
as the required Python 3.12 workflows. Its `uv sync --locked --dev --extra
test` installs the committed dependency graph without regenerating the
lockfile. Stale or incompatible dependencies, missing Python 3.14 wheels,
source-quality failures, and test failures remain visible red job outcomes.
A failed or incomplete run is not compatibility evidence.

The workflow sets `UV_PYTHON=3.14` for uv sync and every uv run so the
repository's default interpreter pin cannot silently create a Python 3.12
virtual environment even after `setup-python` selects 3.14. A separate
version assertion refuses an unexpected interpreter. The workflow verifies
the selected interpreter and clean exact Git checkout,
then runs Black, isort, Flake8 and package-wide mypy before the complete
`not opencode_live` pytest selection. Like the required source workflow, it
prepares the Python-only management Docker fixture from the tested source and
verifies real headless Chromium before running the tests. These prerequisites
are required to avoid incorrectly reporting success with eligible tests
deselected or skipped for missing fixtures; they do not run OpenCode live
tasks. The fixture's isolated container Python is not the interpreter under
evaluation: Python 3.14 is selected for the installed application and host
pytest process.

This independent workflow is **advisory**, not a publication, merge or
deployment authority. It publishes no artifact, grants no package write
permission and does not alter any required Python 3.12 workflow, Docker
base image, production worker, release selection or branch-protection rule.
Green advisory evidence alone is not approval to migrate the production
baseline. See `immutable-ci-uv-setup.md` for the shared immutable-lock gate.

The committed graph preserves Aider 0.86.2's NumPy 1.26.4 and SciPy 1.15.3
on Python below 3.14. On Python 3.14 and later, uv dependency overrides use
Aider upstream's `numpy>=2.3,<2.5` and `scipy>=1.16.1,<1.18` ranges
([upstream requirements](https://github.com/Aider-AI/aider/blob/main/requirements.txt)).
The released Aider metadata still pins numerical releases without Python 3.14
wheels, so merely installing OpenBLAS would leave unsupported NumPy source
builds. These overrides retain both dependencies and lock their compatible
wheels; the compatibility workflow still admits the unchanged committed lock.
`audioop-lts` is installed on Python 3.13 and later because Aider's pydub
import requires the removed standard-library `audioop` module. Real backend
import and PageRank regression tests ensure these dependencies execute, rather
than accepting AiderClient's caught ImportError as evidence of availability.
Production Python 3.12 interpreter selection and numerical versions remain
unchanged. This dependency repair changes no production trace schema or
processing behavior, so no dashboard observability update is required.

The advisory workflow also installs a real Python 3.12 interpreter before
selecting Python 3.14 for the application and pytest host. Worker and management
staging integration fixtures select that separate production interpreter;
they still execute the real installers and all original verification checks.
The installer passes its configured interpreter explicitly to both uv venv and
uv sync, so an inherited advisory `UV_PYTHON=3.14` cannot replace the required
3.12 fixture runtime. Candidate bytecode is compiled with the candidate
interpreter so cache-tampering verification remains meaningful across host
Python versions. Missing production Python is a test failure, not a skip.

Chromium is cached under the runner's temporary directory, outside the source
checkout, so later native fixture builds still verify a clean exact revision.
The complete advisory selection retains coverage context validation using the
observer's context-capable tracing engine and explicit SQLite connection
closure at native bootstrap-store boundaries. Publisher policy unit tests
exercise both Python 3.12 acceptance and Python 3.14 rejection: a successful
advisory run does not grant Python 3.14 production publication authority.
