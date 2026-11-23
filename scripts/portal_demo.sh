#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

help() {
    cat <<'HELP'
用法: bash scripts/portal_demo.sh {setup|up} [--mode docker|source]
      bash scripts/portal_demo.sh {down [--all]|status|logs|--help}
      bash scripts/portal_demo.sh restart-gateway
      bash scripts/portal_demo.sh {export-history|import-history} [--mode docker|source] <zip>
      bash scripts/portal_source.sh {import-history|export-history} <zip>
  setup   准备依赖、数据库和 OpenCode 基线，然后启动 Portal（可重复运行）
  up      启动本地 API 和 worker，显示网址及登录信息
  down    停止 Portal 和源码模式服务；Docker 模式下 --all 同时停止 Compose 服务
  status  查看运行模式、服务、网关连接和已注册版本
  logs    查看当前模式的服务日志
  restart-gateway  加载模型接入配置并重启网关
  export-history   使用当前模式的数据库和对象存储导出历史 ZIP
  import-history   将历史 ZIP 导入本机源码模式或已启动的 Docker 模式
启动模式按 --mode、SCA_RUNTIME_MODE、Portal 保存的首选模式依次选择，默认优先使用可用的 Docker。
本机 Portal 默认免令牌，可在概览打开「需要访问令牌」。
设置 SCA_STRIPE_SIMULATOR=1 可在 up 时启动 Stripe 模拟器。
Docker 模式需要 Docker Compose、至少 15 GB 可用磁盘，macOS 需要 GNU timeout。
源码模式需要本机 PostgreSQL、Node.js 和 npm。两种模式均需 Python 3.12 和 Git。
HELP
}
case "${1:---help}" in -h|--help) help; exit 0;; esac
CALLER_DIR=$PWD
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
COMMAND=$1
shift
REQUESTED_MODE=''
case "$COMMAND" in
    setup|up)
        if [[ $# == 2 && $1 == --mode && ( $2 == docker || $2 == source ) ]]; then
            REQUESTED_MODE=$2
        elif [[ $# != 0 ]]; then help; exit 2; fi;;
    import-history|export-history)
        if [[ $# == 3 && $1 == --mode && ( $2 == source || $2 == docker ) ]]; then
            REQUESTED_MODE=$2
            shift 2
        fi
        [[ $# == 1 && -n $1 && $1 != -* ]] || { help; exit 2; }
        ARCHIVE=$1
        if [[ $ARCHIVE != /* ]]; then ARCHIVE="$CALLER_DIR/$ARCHIVE"; fi;;
    status|logs|restart-gateway|probe-docker) [[ $# == 0 ]] || { help; exit 2; };;
    down) [[ $# == 0 || ( $# == 1 && $1 == --all ) ]] || { help; exit 2; };;
    *) help; exit 2;; esac
TIMEOUT=$(command -v timeout || command -v gtimeout || true)
run() { if [[ -n "$TIMEOUT" ]]; then "$TIMEOUT" 900 "$@"; else "$@"; fi; }
fail() { echo "$1" >&2; exit 1; }
mkdir -p .data/run
PY="$ROOT/.venv/bin/python"
STRIPE_SIM_PORT=12111
STRIPE_SIM_URL="http://127.0.0.1:$STRIPE_SIM_PORT"
STRIPE_SIM_MARKER=.data/run/stripe-sim.prices
# Build and run images natively: arm64 on Apple silicon and ARM Linux, amd64 elsewhere.
case "$(uname -m)" in
    arm64|aarch64) export DOCKER_DEFAULT_PLATFORM=linux/arm64 SCA_CONTAINER_ARCH=arm64;;
    *) export DOCKER_DEFAULT_PLATFORM=linux/amd64 SCA_CONTAINER_ARCH=x64;;
esac
BUN_DIR=.data/tools/bun-linux-$SCA_CONTAINER_ARCH

docker_probe() {
    if [[ -n "$TIMEOUT" ]]; then "$TIMEOUT" 10 "$@"; else "$@"; fi
}
docker_available() {
    if [[ -n ${SCA_DOCKER_BIN:-} ]]; then
        command -v "$SCA_DOCKER_BIN" >/dev/null || return 2
        docker_probe "$SCA_DOCKER_BIN" info >/dev/null 2>&1 || return 3
        docker_probe "$SCA_DOCKER_BIN" compose version >/dev/null 2>&1 || return 4
    else
        command -v docker >/dev/null || return 2
        local failure=3
        if docker_probe docker info >/dev/null 2>&1; then
            docker_probe docker compose version >/dev/null 2>&1 && return 0
            failure=4
        fi
        command -v sg >/dev/null && docker_probe sg docker -c 'docker info' >/dev/null 2>&1 || return "$failure"
        docker_probe sg docker -c 'docker compose version' >/dev/null 2>&1 || return 4
    fi
}
read_preferred_mode() {
    [[ -f .data/runtime-mode.json ]] || return 0
    local python=$PY
    if [[ ! -x "$python" ]]; then python=$(command -v python3.12) || return 0; fi
    PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" run "$python" -c \
        'from pathlib import Path; from sca.runtime_mode import preferred_mode; print(preferred_mode(Path(".data")) or "")'
}
select_mode() {
    local choice="${REQUESTED_MODE:-${SCA_RUNTIME_MODE:-}}"
    if [[ -z "$choice" ]]; then choice=$(read_preferred_mode); fi
    case "$choice" in
        docker|source) printf '%s\n' "$choice";;
        '')
            if docker_available; then
                echo docker
            else
                echo 'Docker 或 Docker Compose 不可用，自动切换到源码模式。' >&2
                echo source
            fi;;
        *) fail 'SCA_RUNTIME_MODE 必须为 docker 或 source。';;
    esac
}
docker_access() {
    [[ -n "$TIMEOUT" ]] || fail '请安装 GNU timeout（macOS: brew install coreutils）。'
    local wrapper="$ROOT/.data/run/docker" override=''
    if [[ -n ${SCA_DOCKER_BIN:-} ]]; then
        override=$(command -v "$SCA_DOCKER_BIN") || fail '找不到 SCA_DOCKER_BIN 指定的 Docker 命令。'
        if [[ $override == */* && $override != /* ]]; then override="$ROOT/$override"; fi
        docker_available || fail 'SCA_DOCKER_BIN 指定的 Docker 或 Docker Compose 不可用。'
        if [[ $override -ef "$wrapper" ]]; then
            export SCA_DOCKER_BIN="$wrapper"
            return 0
        fi
    else
        command -v docker >/dev/null || fail '请安装并启动 Docker。'
        if run docker info >/dev/null 2>&1; then
            :
        elif command -v sg >/dev/null && run sg docker -c 'docker info' >/dev/null 2>&1; then
            :
        else fail 'Docker 不可用；请启动 Docker 或授予当前用户 docker 组权限。'; fi
    fi
    # A wrapper also gives Python subprocesses Docker access through sg.
    cat > .data/run/docker <<WRAPPER
#!/usr/bin/env bash
set -euo pipefail
export DOCKER_DEFAULT_PLATFORM=$DOCKER_DEFAULT_PLATFORM
WRAPPER
    # Build subprocesses use HOME=/tmp, so retain Docker Desktop's user socket explicitly.
    if [[ $(uname -s) == Darwin && -S "$HOME/.docker/run/docker.sock" ]]; then
        printf 'export DOCKER_HOST=%q\n' "unix://$HOME/.docker/run/docker.sock" >> .data/run/docker
    fi
    if [[ -n "$override" ]]; then
        printf 'exec %q "$@"\n' "$override" >> .data/run/docker
    else
        cat >> .data/run/docker <<'WRAPPER'
if docker info >/dev/null 2>&1; then exec docker "$@"; fi
printf -v arguments '%q ' "$@"
command="docker $arguments"
exec sg docker -c "$command"
WRAPPER
    fi
    chmod 700 .data/run/docker
    export SCA_DOCKER_BIN="$ROOT/.data/run/docker"
    run "$SCA_DOCKER_BIN" compose version >/dev/null
}
compose() { run "$SCA_DOCKER_BIN" compose --env-file "$ROOT/.env" -f deploy/docker-compose.yml --profile gateway "$@"; }
gateway_config() {
    run "$PY" - "$ROOT" <<'PY'
import sys
from pathlib import Path

from sca.config import get_settings
from sca.gateway.configuration import deepseek_configured
from sca.gateway.model_providers import gateway_config, read_registry

root = Path(sys.argv[1])
data = get_settings().data_dir
print(gateway_config(root, data, read_registry(data),
                     deepseek=deepseek_configured(root / ".env"), docker=False))
PY
}
start_docker_gateway() {
    run "$PY" - "$ROOT" "$SCA_DOCKER_BIN" "$@" <<'PY'
import os
import sys
from pathlib import Path

from sca.config import get_settings
from sca.gateway.configuration import deepseek_configured
from sca.gateway.model_providers import docker_gateway_command, gateway_config, read_registry

root = Path(sys.argv[1])
data = get_settings().data_dir
registry = read_registry(data)
config = gateway_config(root, data, registry, deepseek=deepseek_configured(root / ".env"), docker=True)
command = docker_gateway_command(root, data, registry, sys.argv[2], sys.argv[3:])
os.execvpe(command[0], command, {**os.environ, **registry.environment(), "SCA_LITELLM_CONFIG": str(config)})
PY
}
load_env() {
    [[ -f .data/portal.env ]] || fail '请先运行 setup。'
    unset SCA_RUNTIME_MODE
    # Only this generated file contains shell syntax; .env is never sourced.
    # shellcheck disable=SC1091
    source .data/portal.env
    export SCA_RUNTIME_MODE="${SCA_RUNTIME_MODE:-docker}"
    if [[ "$SCA_RUNTIME_MODE" == source ]]; then
        load_source_env
    elif [[ "$SCA_RUNTIME_MODE" == docker && -f .env ]]; then
        # Older Docker setups predate the local model configuration setting.
        export SCA_PORTAL_LOCAL_CONFIG="${SCA_PORTAL_LOCAL_CONFIG-$ROOT/.env}"
    fi
}
saved_mode() (
    unset SCA_RUNTIME_MODE
    # shellcheck disable=SC1091
    if [[ -f .data/portal.env ]]; then source .data/portal.env; fi
    printf '%s\n' "${SCA_RUNTIME_MODE:-docker}"
)
save_mode() {
    run "$PY" - "$1" <<'PY'
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

from sca.config import portal_require_token_env

portal = Path(".data/portal.env")
name = "SCA_PORTAL_REQUIRE_TOKEN"
try:
    required = portal_require_token_env(dotenv_values(portal).get(name), dotenv_values(".env").get(name))
except ValueError as exc:
    raise SystemExit(str(exc)) from None
set_key(portal, "SCA_RUNTIME_MODE", sys.argv[1], export=True)
set_key(portal, name, required, export=True)
portal.chmod(0o600)
PY
}
alive() {
    [[ -f ".data/run/$1.pid" ]] || return 1
    local pid
    read -r pid < ".data/run/$1.pid"
    [[ $pid =~ ^[0-9]+$ && $pid -gt 1 ]] && kill -0 "$pid" 2>/dev/null &&
        [[ $(ps -p "$pid" -o stat=) != *Z* ]]
}
owned() {
    alive "$1" || return 1
    local pid expected
    read -r pid < ".data/run/$1.pid"
    read -r expected < ".data/run/$1.identity" || return 1
    [[ $(run ps -p "$pid" -o lstart= -o args=) == "$expected" ]]
}
request() {
    run "$PY" - "$1" <<'PY'
import json
import os
import sys
import urllib.request
try:
    token = os.environ.get("SCA_EVAL_API_TOKEN")
    request = urllib.request.Request(
        "http://127.0.0.1:8000" + sys.argv[1],
        headers={"Authorization": "Bearer " + token} if token else {},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)
    if sys.argv[1] != "/healthz":
        print(json.dumps(data, ensure_ascii=False, indent=2))
except Exception:
    print("API 请求失败，请检查服务状态。", file=sys.stderr)
    sys.exit(1)
PY
}
start() {
    local name=$1
    shift
    if owned "$name"; then return; fi
    alive "$name" && fail 'PID 文件与进程不符，请人工检查 .data/run/。'
    nohup "$@" >> ".data/run/$name.log" 2>&1 < /dev/null &
    local pid=$!
    echo "$pid" > ".data/run/$name.pid"
    run sleep 0.2
    run ps -p "$pid" -o lstart= -o args= > ".data/run/$name.identity" ||
        fail "$name 启动失败，请查看 .data/run/$name.log。"
}
stop() {
    local name=$1 pid
    if owned "$name"; then
        read -r pid < ".data/run/$name.pid"
        if ! kill "$pid" 2>/dev/null && alive "$name"; then
            fail '无法停止进程，请检查 .data/run/。'
        fi
        for ((i=0; i<30; i++)); do alive "$name" || break; run sleep 1; done
        alive "$name" && fail '进程尚未退出，请检查日志后重试。'
    elif alive "$name"; then fail 'PID 文件与进程不符，拒绝停止。'; fi
    rm -f ".data/run/$name.pid" ".data/run/$name.identity"
}
stripe_sim_prices() {
    run "$PY" - "$1" <<'PY'
import asyncio
import os
import sys

from sqlalchemy import text

from sca.config import get_settings
from sca.db.engine import make_engine


async def configure_prices(install: bool) -> None:
    engine = make_engine(get_settings().database_url)
    try:
        async with engine.begin() as conn:
            for plan in ("team", "enterprise"):
                params = {"plan": plan, "price": os.environ[f"SCA_STRIPE_SIM_{plan.upper()}_PRICE_ID"]}
                if install:
                    await conn.execute(text("""
                        UPDATE plans SET stripe_price_id = :price WHERE name = :plan
                        AND stripe_price_id IS NULL
                    """), params)
                else:
                    await conn.execute(text("""
                        UPDATE plans SET stripe_price_id = NULL WHERE name = :plan AND stripe_price_id = :price
                    """), params)
    finally:
        await engine.dispose()


try:
    asyncio.run(configure_prices(sys.argv[1] == "install"))
except Exception:
    raise SystemExit("更新 Stripe 模拟套餐失败，请检查数据库连接和迁移。") from None
PY
}
start_stripe_sim() {
    run "$PY" - <<'PY'
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

portal = Path(".data/portal.env")
values = dotenv_values(portal, interpolate=False)
for name, prefix in (
    ("SCA_STRIPE_SIM_SECRET_KEY", "sk_test_sim_"),
    ("SCA_STRIPE_SIM_WEBHOOK_SECRET", "whsec_"),
    ("SCA_STRIPE_SIM_TEAM_PRICE_ID", "price_sim_team_"),
    ("SCA_STRIPE_SIM_ENTERPRISE_PRICE_ID", "price_sim_enterprise_"),
):
    if not values.get(name):
        set_key(portal, name, prefix + secrets.token_hex(32), export=True)
portal.chmod(0o600)
PY
    load_env
    export SCA_STRIPE_API_URL="$STRIPE_SIM_URL"
    export SCA_STRIPE_SECRET_KEY="$SCA_STRIPE_SIM_SECRET_KEY"
    export SCA_STRIPE_WEBHOOK_SECRET="$SCA_STRIPE_SIM_WEBHOOK_SECRET"
    start stripe-sim "$PY" -m sca.billing.stripe_sim --port "$STRIPE_SIM_PORT" \
        --webhook-url "${SCA_API_URL%/}/v1/billing/stripe/webhook"
    local i
    for ((i=0; i<20; i++)); do
        owned stripe-sim || fail 'Stripe 模拟器启动失败，请查看 .data/run/stripe-sim.log。'
        if run "$PY" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=1).close()' \
            "$STRIPE_SIM_URL/_sim/health" >/dev/null 2>&1; then
            touch "$STRIPE_SIM_MARKER"
            stripe_sim_prices install
            return
        fi
        run sleep 0.5
    done
    fail 'Stripe 模拟器启动超时，请查看 .data/run/stripe-sim.log。'
}
up() {
    load_env
    run "$PY" -m alembic upgrade head
    if [[ $SCA_RUNTIME_MODE == docker ]]; then
        run "$PY" -m sca.agents.docker_node ||
            echo 'DeepSeek Harness 运行时暂未就绪，运行 dsh 任务时将重试。' >&2
    fi
    if [[ ${SCA_STRIPE_SIMULATOR:-0} == 1 ]]; then
        if [[ ! -f "$STRIPE_SIM_MARKER" ]] && { owned api || owned worker; }; then
            fail '请先运行 down，再以 Stripe 模拟模式启动 Portal。'
        fi
    elif [[ -f "$STRIPE_SIM_MARKER" ]] || owned stripe-sim; then
        fail '请先运行 down，再启动普通模式的 Portal。'
    fi
    if [[ ${SCA_STRIPE_SIMULATOR:-0} == 1 ]]; then start_stripe_sim; fi
    start api "$PY" -m sca.api.portal_server --no-proxy-headers --host 127.0.0.1 --port 8000
    start worker "$PY" -m sca.orchestration.worker
    local ready=0
    for ((i=0; i<60; i++)); do
        if ! owned api || ! owned worker; then
            fail '进程启动失败，请查看 .data/run/ 日志。'
        fi
        if request /healthz >/dev/null 2>&1; then ready=1; break; fi
        run sleep 2
    done
    [[ $ready == 1 ]] || fail 'API 启动超时，请查看 .data/run/ 日志。'
    echo 'Portal: http://127.0.0.1:8000/portal'
    if [[ "$SCA_RUNTIME_MODE" == source || -n ${SCA_EVAL_API_TOKEN:-} ]]; then
        echo '如需令牌，请在本机查看 .data/portal.env 的 SCA_EVAL_API_TOKEN。'
    fi
    if [[ ${SCA_STRIPE_SIMULATOR:-0} == 1 ]]; then
        echo "远程访问: ssh -L 8000:127.0.0.1:8000 -L $STRIPE_SIM_PORT:127.0.0.1:$STRIPE_SIM_PORT host"
    else
        echo '远程访问: ssh -L 8000:127.0.0.1:8000 host'
    fi
}
prepare_app() {
    for tool in python3.12 git; do command -v "$tool" >/dev/null || fail "缺少依赖: $tool"; done
    run python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
    [[ -x "$PY" ]] || run python3.12 -m venv .venv
    run "$PY" -m pip install -q -e '.[dev]'
    run "$PY" - <<'PY'
import os
import json
import secrets
import shlex
from pathlib import Path
from dotenv import dotenv_values

path = Path(".env")
fresh = not path.exists()
if fresh:
    path.write_text(Path(".env.example").read_text())
path.chmod(0o600)
values = dotenv_values(path)
# Both services must share one master key; existing credentials are never printed.
master = values.get("LITELLM_MASTER_KEY")
if not master or master == "GENERATED_BY_SETUP":
    master = "sk-" + secrets.token_hex(32)
    from dotenv import set_key
    set_key(path, "LITELLM_MASTER_KEY", master)
from dotenv import set_key
if values.get("SCA_MODEL_GATEWAY_MASTER_KEY") in (None, "", "GENERATED_BY_SETUP"):
    set_key(path, "SCA_MODEL_GATEWAY_MASTER_KEY", master)
elif values["SCA_MODEL_GATEWAY_MASTER_KEY"] != master:
    raise SystemExit(".env 中两个网关 master key 必须相同；请人工修正后重试。")
if not values.get("DEEPSEEK_API_KEY") or values["DEEPSEEK_API_KEY"] == "REPLACE_ME":
    print("尚未配置 DeepSeek API Key；启动后可在 Portal 中填写。")
portal = Path(".data/portal.env")
previous = dotenv_values(portal) if portal.exists() else {}
token = os.environ.get("PORTAL_TOKEN") or previous.get("SCA_EVAL_API_TOKEN") or secrets.token_hex(32)
if not token or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in token):
    raise SystemExit("PORTAL_TOKEN 仅允许字母、数字、下划线和连字符。")
settings = dict(
    SCA_AGENT_BACKEND="opencode", SCA_DEFAULT_MODEL="deepseek-flash",
    SCA_MODEL_GATEWAY_URL="http://localhost:4000",
    SCA_EGRESS_ALLOW="pypi.org,files.pythonhosted.org",
    SCA_API_TOKENS=f"dev-token:default,{token}:eval:admin",
    SCA_EVAL_API_TOKEN=token, SCA_API_URL="http://127.0.0.1:8000",
    # Operator token for /v1/admin/* (tenants, plans, cross-tenant usage); kept across setups.
    SCA_PLATFORM_ADMIN_TOKEN=previous.get("SCA_PLATFORM_ADMIN_TOKEN") or secrets.token_hex(32),
    SCA_PORTAL_LOCAL_CONFIG=str(Path('.env').resolve()),
    SCA_WORKER_METRICS_PORT="9464", SCA_OPENCODE_BUILD_MEMORY="2500m",
    # The brain backend runs `docker exec` itself; reuse the wrapper that falls back to `sg docker`.
    SCA_OPENCODE_BRAIN_DOCKER_PREFIX=json.dumps([str(Path(".data/run/docker").resolve())]),
)
for key in ("SCA_PORTAL_REQUIRE_TOKEN", "SCA_STRIPE_SIM_SECRET_KEY", "SCA_STRIPE_SIM_WEBHOOK_SECRET",
            "SCA_STRIPE_SIM_TEAM_PRICE_ID", "SCA_STRIPE_SIM_ENTERPRISE_PRICE_ID"):
    if previous.get(key):
        settings[key] = previous[key]
portal.write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in settings.items()))
portal.chmod(0o600)
PY
}
start_docker_services() {
    compose up -d --wait postgres temporal
    # LiteLLM expects its own database; never remove existing databases or volumes.
    compose exec -T postgres psql -U sca -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'CREATE DATABASE litellm' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'litellm')\gexec
SQL
    start_docker_gateway
}
setup_docker() {
    command -v python3.12 >/dev/null || fail '缺少依赖: python3.12'
    run python3.12 -c 'import shutil; assert shutil.disk_usage(".").free >= 15 * 1024**3, "需要至少 15 GB 可用磁盘"'
    echo 'OpenCode 构建内存上限为 2.5 GB；请为系统及其他服务预留额外内存。'
    docker_access
    prepare_app
    save_mode docker
    load_env
    start_docker_services
    run "$SCA_DOCKER_BIN" build -t sca-sandbox:latest deploy/sandbox
    run "$SCA_DOCKER_BIN" build -f deploy/pi/sandbox.Dockerfile -t sca-sandbox-pi:latest deploy/pi
    run "$SCA_DOCKER_BIN" build -f deploy/dsh/sandbox.Dockerfile -t sca-sandbox-dsh:latest deploy/dsh
    run "$SCA_DOCKER_BIN" build -t sca-sandbox-analysis:latest deploy/sandbox-analysis
    # TLS-inspecting egress proxy for restricted sandboxes (see docs/design-phase4.md).
    run "$SCA_DOCKER_BIN" build -t sca-egress:tls-v1 deploy/egress
    mkdir -p "$BUN_DIR"
    run "$SCA_DOCKER_BIN" run --rm --memory=2500m --memory-swap=2500m --entrypoint cat oven/bun:1.4.4 /usr/local/bin/bun > "$BUN_DIR/bun.tmp"
    mv "$BUN_DIR/bun.tmp" "$BUN_DIR/bun"
    chmod 700 "$BUN_DIR/bun"
    "$TIMEOUT" 3600 "$PY" scripts/opencode_setup.py --execute-docker
}

# Local source mode
# shellcheck source=scripts/portal_source.sh
source "$ROOT/scripts/portal_source.sh"

ensure_mode_switch() {
    local name
    if [[ $(saved_mode) != "$1" ]]; then
        for name in worker api stripe-sim litellm temporal postgres; do
            if alive "$name"; then fail '请先运行 down 停止当前模式的服务，再切换模式。'; fi
        done
    fi
}

print_status() {
    local name
    for name in "$@"; do
        if owned "$name"; then echo "$name: 运行中"; else echo "$name: 未运行或 PID 不匹配"; fi
    done
}

case "$COMMAND" in
    setup|up)
        mode=$(select_mode)
        ensure_mode_switch "$mode"
        if [[ "$mode" == source ]]; then
            find_postgres
            if [[ $COMMAND == setup || ! -x "$PY" || ! -f .data/portal.env || ! -f .env ]]; then
                prepare_app
            fi
            source_port temporal 7233
            source_port litellm 4000
            save_mode source
            load_env
            start_source_services
        elif [[ $COMMAND == setup ]]; then
            setup_docker
        else
            [[ -f .data/portal.env ]] || fail '请先运行 setup。'
            docker_access
            save_mode docker
            load_env
            start_docker_services
        fi
        up;;
    import-history|export-history)
        history_mode=$REQUESTED_MODE
        if [[ -z "$history_mode" ]]; then
            if [[ $COMMAND == import-history ]]; then history_mode=source; else history_mode=$(saved_mode); fi
        fi
        if [[ $history_mode == source ]]; then
            ensure_mode_switch source
            find_postgres
            if [[ ! -x "$PY" || ! -f .data/portal.env || ! -f .env ]]; then
                prepare_app
            fi
            if [[ $(saved_mode) != source ]]; then save_mode source; fi
            load_env
            start_postgres
            "$PY" "$SOURCE_HELPER" history "${COMMAND%-history}" "$ARCHIVE" --migrate
        else
            [[ $(saved_mode) == docker ]] || fail '请先启动 Docker 模式。'
            load_env
            [[ -x "$PY" ]] || fail '请先运行 setup。'
            "$PY" "$SOURCE_HELPER" history "${COMMAND%-history}" "$ARCHIVE"
        fi;;
    probe-docker)
        [[ -n "$TIMEOUT" ]] || exit 5
        docker_available;;
    down)
        mode=$(saved_mode)
        cleanup_failed=0
        for name in worker api stripe-sim; do stop "$name"; done
        if [[ -f "$STRIPE_SIM_MARKER" ]]; then
            load_env
            if stripe_sim_prices remove; then
                rm -f "$STRIPE_SIM_MARKER"
            elif [[ $mode == source ]]; then
                cleanup_failed=1
            else
                exit 1
            fi
        fi
        if [[ $mode == source ]]; then
            stop_source_services
        elif [[ ${1:-} == --all ]]; then docker_access; compose down; fi
        exit "$cleanup_failed";;
    status)
        load_env
        echo "运行模式: $SCA_RUNTIME_MODE"
        print_status api worker
        if [[ -f .data/run/stripe-sim.pid ]]; then print_status stripe-sim; fi
        if [[ $SCA_RUNTIME_MODE == source ]]; then
            print_status postgres temporal litellm
            if ! owned api; then exit 0; fi
        else
            docker_access
            compose ps
        fi
        request /v1/portal/config
        if [[ -n ${SCA_EVAL_API_TOKEN:-} ]]; then request /v1/agents/opencode/versions; fi;;
    logs)
        log_files=(.data/run/api.log .data/run/worker.log)
        if [[ -f .data/run/stripe-sim.pid ]]; then log_files+=(.data/run/stripe-sim.log); fi
        if [[ $(saved_mode) == source ]]; then
            for name in postgres temporal litellm; do
                if [[ -f ".data/run/$name.log" ]]; then log_files+=(".data/run/$name.log"); fi
            done
        fi
        run tail -n 100 "${log_files[@]}";;
    restart-gateway)
        load_env
        if [[ $SCA_RUNTIME_MODE == source ]]; then
            stop litellm
            start_litellm 80
        else
            docker_access
            start_docker_gateway --force-recreate
            if owned worker; then
                # The Docker worker resolves the gateway's sandbox IP on startup.
                stop worker
                start worker "$PY" -m sca.orchestration.worker
            fi
        fi;;
esac
