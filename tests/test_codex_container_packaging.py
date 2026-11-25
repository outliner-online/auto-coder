"""Execute the production Codex packaging commands against platform bundle fixtures."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("_use_real_commands")


@pytest.mark.parametrize("platform,architecture", [("x64", "x86_64"), ("arm64", "aarch64")])
@pytest.mark.parametrize("host_present", [True, False], ids=["complete-bundle", "missing-host"])
def test_dockerfile_installs_executable_codex_companion(tmp_path: Path, platform: str, architecture: str, host_present: bool) -> None:
    """The real install/COPY instructions preserve the host or reject an incomplete bundle."""
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
    version_match = re.search(r"^ARG CODEX_VERSION=(.+)$", dockerfile, re.MULTILINE)
    assert version_match is not None
    version = version_match.group(1)
    package_root = tmp_path / "package"
    source_bin = package_root / "node_modules" / "@openai" / f"codex-linux-{platform}" / "vendor" / f"{architecture}-unknown-linux-musl" / "bin"
    source_bin.mkdir(parents=True)
    (source_bin / "codex").write_text(f'#!/bin/sh\n[ "$1" = "--version" ] || exit 2\nprintf "codex-cli {version}\\n"\n', encoding="utf-8")
    host_output = f"codex-code-mode-host {version} {platform}"
    if host_present:
        (source_bin / "codex-code-mode-host").write_text(f'#!/bin/sh\n[ "$1" = "--help" ] || exit 2\nprintf "{host_output}\\n"\n', encoding="utf-8")

    build_bin = tmp_path / "build-bin"
    build_bin.mkdir()
    # Execute the production install and smoke commands, rebasing only absolute
    # container paths. The platform bundle fixture replaces the network fetch.
    install_commands = dockerfile.split("npm install --prefix /tmp/codex", 1)[1].split("&&", 1)[1].split("rm -rf /tmp/opencode /tmp/codex", 1)[0]
    install_commands = install_commands.replace("\\\n", "").strip().removesuffix("&&").strip()
    install_commands = install_commands.replace("/tmp/codex", shlex.quote(str(package_root))).replace("/usr/local/bin", shlex.quote(str(build_bin)))
    environment = {**os.environ, "OPENCODE_ARCH": platform, "CODEX_ARCH": architecture, "CODEX_VERSION": version}
    installed = subprocess.run(["/bin/sh", "-c", install_commands], env=environment, capture_output=True, text=True, timeout=10)
    if not host_present:
        assert installed.returncode != 0, "The build accepted a bundle without the required code-mode host"
        assert not (build_bin / "codex-code-mode-host").exists()
        return
    assert installed.returncode == 0, installed.stderr

    runtime_bin = tmp_path / "runtime-bin"
    runtime_bin.mkdir()
    # Follow the actual final-stage COPY list rather than assuming both binaries
    # survive the stage boundary. copy2 retains the installed executable mode.
    for line in dockerfile.splitlines():
        if not line.startswith("COPY --from=build "):
            continue
        _, _, source, destination = shlex.split(line)
        if Path(source).name.startswith("codex"):
            shutil.copy2(build_bin / Path(source).name, runtime_bin / Path(destination).name)

    for name, argument, output in (("codex", "--version", f"codex-cli {version}"), ("codex-code-mode-host", "--help", host_output)):
        executable = runtime_bin / name
        assert executable.is_file(), f"The runtime image lost {name}"
        assert executable.read_bytes() == (source_bin / name).read_bytes()
        assert executable.stat().st_mode & 0o777 == 0o755
        observed = subprocess.run([str(executable), argument], capture_output=True, text=True, timeout=10, check=True)
        assert observed.stdout.strip() == output
