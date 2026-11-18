"""Static checks that never start demo services or inspect local credentials."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/portal_demo.sh"


def test_bash_syntax() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, timeout=10)


def test_shellcheck() -> None:
    executable = shutil.which("shellcheck")
    if executable is None:
        pytest.skip("shellcheck is not installed")
    subprocess.run([executable, str(SCRIPT)], check=True, timeout=20)


def test_help_without_docker(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 99\n")
    docker.chmod(0o700)
    cat = shutil.which("cat")
    assert cat is not None
    (tmp_path / "cat").symlink_to(cat)
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(
        [bash, str(SCRIPT), "--help"],
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert all(word in result.stdout for word in ("setup", "up", "down", "status", "logs"))


def test_secret_output_and_process_contracts() -> None:
    source = SCRIPT.read_text()
    assert "set +x" in source
    assert not re.search(r"(?:source|cat|printenv|env)\s+[\"']?\.env\b", source)
    assert not re.search(
        r"(?:echo|printf|print\().*(?:DEEPSEEK_API_KEY|MASTER_KEY).*\$", source
    )
    assert not re.search(r"(?:echo|printf|print\().*(?:values\[|values\.get|\bmaster\b)", source)
    assert "pkill" not in source
    assert '.data/run/$1.pid' in source
    assert '.data/run/$name.pid' in source
    assert 'kill "$pid"' in source
    assert "-o lstart= -o args=" in source
    assert "secrets.token_hex(32)" in source
    assert 'SCA_OPENCODE_BUILD_MEMORY="2500m"' in source
