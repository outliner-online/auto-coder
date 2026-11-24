"""Aider stays outside the installed application dependency graph."""

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path


def test_aider_is_not_installed_or_locked():
    root = Path(__file__).resolve().parents[1]
    lock = tomllib.loads((root / "uv.lock").read_text())
    names = {package["name"] for package in lock["package"]}
    assert names.isdisjoint({"aider-chat", "audioop-lts", "numpy", "scipy", "pydub", "regex"})
    assert "python-dateutil" in names
    assert importlib.util.find_spec("aider") is None


def test_native_entry_point_without_aider(_use_real_commands):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([str(Path(sys.executable).with_name("auto-coder")), "--help"], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
