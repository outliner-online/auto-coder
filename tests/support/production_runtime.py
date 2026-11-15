"""Select the real production interpreter independently of the pytest host."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def production_python() -> Path:
    candidate = sys.executable if sys.version_info[:2] == (3, 12) else shutil.which("python3.12")
    if not candidate:
        pytest.fail("Production runtime integration tests require an installed Python 3.12")
    result = subprocess.run([candidate, "-I", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "3.12", result.stdout + result.stderr
    return Path(candidate)
