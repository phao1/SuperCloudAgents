"""Static checks that never start demo services or inspect local credentials."""

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/portal_demo.sh"


def test_bash_syntax() -> None:
    for script in (SCRIPT, SCRIPT.with_name("portal_source.sh")):
        subprocess.run(["bash", "-n", str(script)], check=True, timeout=10)


def test_shellcheck() -> None:
    executable = shutil.which("shellcheck")
    if executable is None:
        pytest.skip("shellcheck is not installed")
    subprocess.run([executable, "-x", str(SCRIPT)], check=True, timeout=20)


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


@pytest.fixture
def demo(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / SCRIPT.name
    shutil.copyfile(SCRIPT, script)
    (scripts / "portal_source.sh").write_text(
        'find_postgres() { :; }\nsource_port() { :; }\nload_source_env() { :; }\n'
        'start_source_services() { gateway_config > source-gateway-config; }\n'
        'stop_source_services() { echo "stopped native services"; }\n'
        'start() { echo "$1" >> startup-calls; }\n'
        'owned() { [[ -f startup-calls ]] && grep -qx "$1" startup-calls; }\n'
        'request() { :; }\n'
        'stop() { :; }\n'
    )
    portal_env = tmp_path / ".data" / "portal.env"
    portal_env.parent.mkdir()
    portal_env.write_text(f"export SCA_PORTAL_LOCAL_CONFIG={shlex.quote(str(tmp_path / '.env'))}\n")
    (tmp_path / ".env").touch()
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    python = tmp_path / ".venv" / "bin" / "python"
    python.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = -m ]; then\n'
        '  printf "%s\\n" "$*" >> startup-calls\n'
        '  if [ "$2" = alembic ]; then exit "${DEMO_MIGRATE_STATUS:-0}"; fi\n'
        '  if [ "$2" = sca.agents.docker_node ]; then exit 0; fi\n'
        'fi\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    python.chmod(0o700)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in {
        "timeout": 'shift\nexec "$@"',
        "docker": 'printf "%s\n" "$*" >> docker-calls\n'
                  'if [ -n "${SCA_LITELLM_CONFIG:-}" ]; then '
                  'printf "%s\n" "${SCA_LITELLM_CONFIG##*/}" > docker-gateway-config; fi\n'
                  'if [ "$1" = info ]; then exit "${DEMO_INFO_STATUS:-0}"; fi\n'
                  'exit "${DEMO_COMPOSE_STATUS:-0}"',
        "sg": "exit 1",
    }.items():
        binary = binaries / name
        binary.write_text("#!/bin/sh\n" + body + "\n")
        binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries), prepend=":")
    monkeypatch.delenv("SCA_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("SCA_STRIPE_SIMULATOR", raising=False)

    def run(*args):
        return subprocess.run(
            ["bash", str(script), *args], check=False, capture_output=True, text=True, timeout=10,
        )

    return run, portal_env


@pytest.mark.parametrize(
    ("args", "env_mode", "down_mode", "info_code", "compose_code", "expected"),
    [
        ([], "", "", "0", "0", "docker"),
        ([], "", "", "1", "0", "source"),
        ([], "", "", "0", "1", "source"),
        ([], "source", "docker", "0", "0", "source"),
        (["--mode", "source"], "docker", "docker", "0", "0", "source"),
        (["--mode", "docker"], "source", "", "0", "0", "docker"),
    ],
)
def test_mode_selection(demo, monkeypatch, args, env_mode, down_mode, info_code, compose_code, expected):
    run, portal_env = demo
    monkeypatch.setenv("SCA_RUNTIME_MODE", env_mode)
    monkeypatch.setenv("DEMO_INFO_STATUS", info_code)
    monkeypatch.setenv("DEMO_COMPOSE_STATUS", compose_code)
    result = run("up", *args)
    assert result.returncode == 0, result.stderr
    assert "Portal: http://127.0.0.1:8000/portal" in result.stdout
    assert f"SCA_RUNTIME_MODE='{expected}'" in portal_env.read_text()
    fallback = not args and not env_mode and expected == "source"
    assert ("自动切换到源码模式" in result.stderr) is fallback
    if down_mode:
        monkeypatch.setenv("SCA_RUNTIME_MODE", down_mode)
        status = run("status")
        assert status.returncode == 0
        assert "运行模式: source" in status.stdout
        assert all(name in status.stdout for name in ("postgres", "temporal", "litellm", "api", "worker"))
        assert run("down", "--all").stdout.strip() == "stopped native services"
        assert not (portal_env.parents[1] / "docker-calls").exists()


def test_invalid_mode_is_rejected(demo, monkeypatch):
    run, _ = demo
    monkeypatch.setenv("SCA_RUNTIME_MODE", "other")
    assert run("up").returncode != 0
    assert run("up", "--mode", "other").returncode == 2


@pytest.mark.parametrize("mode", ["docker", "source"])
def test_gateway_starts_with_and_without_a_model_key(demo, mode):
    run, portal_env = demo
    root = portal_env.parents[1]
    selected = root / f"{mode}-gateway-config"
    for entry, expected in (("", "history.yaml"), ("DEEPSEEK_API_KEY=REPLACE_ME\n", "history.yaml"),
                            ("DEEPSEEK_API_KEY=fixture-configured\n", "config.yaml")):
        (root / ".env").write_text(entry)
        result = run("up", "--mode", mode)
        assert result.returncode == 0, result.stderr
        assert selected.read_text().strip() == expected


@pytest.mark.parametrize("mode", ["docker", "source"])
@pytest.mark.parametrize("migration_status", ["0", "1"])
def test_up_migrates_before_starting_api_and_worker(demo, monkeypatch, mode, migration_status):
    run, portal_env = demo
    monkeypatch.setenv("DEMO_MIGRATE_STATUS", migration_status)
    result = run("up", "--mode", mode)
    assert result.returncode == int(migration_status), result.stderr
    calls = ["-m alembic upgrade head"]
    if migration_status == "0":
        if mode == "docker":
            calls.append("-m sca.agents.docker_node")
        calls.extend(("api", "worker"))
    assert (portal_env.parents[1] / "startup-calls").read_text().splitlines() == calls
