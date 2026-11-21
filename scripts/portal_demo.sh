#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

help() {
    cat <<'HELP'
用法: bash scripts/portal_demo.sh {setup|up|down [--all]|status|logs|--help}
  setup   准备依赖、数据库和 OpenCode 基线，然后启动 Portal（可重复运行）
  up      启动本地 API 和 worker，打印网址和登录令牌
  down    按 PID 文件停止进程；--all 同时停止 Compose 服务（保留数据）
  status  查看服务、网关连接和已注册版本
  logs    查看 API 和 worker 最近日志
设置 SCA_STRIPE_SIMULATOR=1 可在 up 时启动 Stripe 模拟器。
需要 Python 3.12、Git、Docker Compose 和至少 15 GB 可用磁盘。
OpenCode 构建内存上限为 2.5 GB；macOS 需要 Docker Desktop 和 GNU timeout。
HELP
}
case "${1:---help}" in -h|--help) help; exit 0;; esac
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
COMMAND=$1
shift
case "$COMMAND" in setup|up|status|logs) [[ $# == 0 ]] || { help; exit 2; };;
    down) [[ $# == 0 || ( $# == 1 && $1 == --all ) ]] || { help; exit 2; };;
    *) help; exit 2;; esac
TIMEOUT=$(command -v timeout || command -v gtimeout || true)
[[ -n "$TIMEOUT" ]] || { echo '请安装 GNU timeout（macOS: brew install coreutils）。'; exit 1; }
run() { "$TIMEOUT" 900 "$@"; }
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

docker_access() {
    command -v docker >/dev/null || fail '请安装并启动 Docker。'
    if run docker info >/dev/null 2>&1; then
        :
    elif command -v sg >/dev/null && run sg docker -c 'docker info' >/dev/null 2>&1; then
        :
    else fail 'Docker 不可用；请启动 Docker 或授予当前用户 docker 组权限。'; fi
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
    cat >> .data/run/docker <<'WRAPPER'
if docker info >/dev/null 2>&1; then exec docker "$@"; fi
printf -v arguments '%q ' "$@"
command="docker $arguments"
exec sg docker -c "$command"
WRAPPER
    chmod 700 .data/run/docker
    export SCA_DOCKER_BIN="$ROOT/.data/run/docker"
    run "$SCA_DOCKER_BIN" compose version >/dev/null
}
compose() { run "$SCA_DOCKER_BIN" compose --env-file "$ROOT/.env" -f deploy/docker-compose.yml --profile gateway "$@"; }
load_env() {
    [[ -f .data/portal.env ]] || fail '请先运行 setup。'
    # Only this generated file contains shell syntax; .env is never sourced.
    # shellcheck disable=SC1091
    source .data/portal.env
}
alive() {
    [[ -f ".data/run/$1.pid" ]] || return 1
    local pid
    read -r pid < ".data/run/$1.pid"
    [[ $pid =~ ^[0-9]+$ && $pid -gt 1 ]] && kill -0 "$pid" 2>/dev/null
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
    request = urllib.request.Request(
        "http://127.0.0.1:8000" + sys.argv[1],
        headers={"Authorization": "Bearer " + os.environ["SCA_EVAL_API_TOKEN"]},
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
    run ps -p "$pid" -o lstart= -o args= > ".data/run/$name.identity"
}
stop() {
    local name=$1 pid
    if owned "$name"; then
        read -r pid < ".data/run/$name.pid"
        kill "$pid"
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
    if [[ ${SCA_STRIPE_SIMULATOR:-0} == 1 ]]; then
        if [[ ! -f "$STRIPE_SIM_MARKER" ]] && { owned api || owned worker; }; then
            fail '请先运行 down，再以 Stripe 模拟模式启动 Portal。'
        fi
    elif [[ -f "$STRIPE_SIM_MARKER" ]] || owned stripe-sim; then
        fail '请先运行 down，再启动普通模式的 Portal。'
    fi
    docker_access
    if [[ ${SCA_STRIPE_SIMULATOR:-0} == 1 ]]; then start_stripe_sim; fi
    start api "$ROOT/.venv/bin/uvicorn" sca.api.app:create_app --factory --host 127.0.0.1 --port 8000
    start worker "$PY" -m sca.orchestration.worker
    local ready=0
    for ((i=0; i<60; i++)); do
        owned api && owned worker || fail '进程启动失败，请查看 .data/run/ 日志。'
        if request /healthz >/dev/null 2>&1; then ready=1; break; fi
        run sleep 2
    done
    [[ $ready == 1 ]] || fail 'API 启动超时，请查看 .data/run/ 日志。'
    echo 'Portal: http://127.0.0.1:8000/portal'
    printf '登录令牌: %s\n' "$SCA_EVAL_API_TOKEN"
    if [[ ${SCA_STRIPE_SIMULATOR:-0} == 1 ]]; then
        echo "远程访问: ssh -L 8000:127.0.0.1:8000 -L $STRIPE_SIM_PORT:127.0.0.1:$STRIPE_SIM_PORT host"
    else
        echo '远程访问: ssh -L 8000:127.0.0.1:8000 host'
    fi
}
setup() {
    for tool in python3.12 git; do command -v "$tool" >/dev/null || fail "缺少依赖: $tool"; done
    run python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
    run python3.12 -c 'import shutil; assert shutil.disk_usage(".").free >= 15 * 1024**3, "需要至少 15 GB 可用磁盘"'
    echo 'OpenCode 构建内存上限为 2.5 GB；请为系统及其他服务预留额外内存。'
    docker_access
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
for key in ("SCA_STRIPE_SIM_SECRET_KEY", "SCA_STRIPE_SIM_WEBHOOK_SECRET",
            "SCA_STRIPE_SIM_TEAM_PRICE_ID", "SCA_STRIPE_SIM_ENTERPRISE_PRICE_ID"):
    if previous.get(key):
        settings[key] = previous[key]
portal.write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in settings.items()))
portal.chmod(0o600)
PY
    load_env
    compose up -d --wait postgres temporal
    # LiteLLM expects its own database; never remove existing databases or volumes.
    compose exec -T postgres psql -U sca -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'CREATE DATABASE litellm' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'litellm')\gexec
SQL
    compose up -d --wait
    run .venv/bin/alembic upgrade head
    run "$SCA_DOCKER_BIN" build -t sca-sandbox:latest deploy/sandbox
    run "$SCA_DOCKER_BIN" build -f deploy/pi/sandbox.Dockerfile -t sca-sandbox-pi:latest deploy/pi
    run "$SCA_DOCKER_BIN" build -t sca-sandbox-analysis:latest deploy/sandbox-analysis
    # TLS-inspecting egress proxy for restricted sandboxes (see docs/design-phase4.md).
    run "$SCA_DOCKER_BIN" build -t sca-egress:tls-v1 deploy/egress
    mkdir -p "$BUN_DIR"
    run "$SCA_DOCKER_BIN" run --rm --memory=2500m --memory-swap=2500m --entrypoint cat oven/bun:1.4.4 /usr/local/bin/bun > "$BUN_DIR/bun.tmp"
    mv "$BUN_DIR/bun.tmp" "$BUN_DIR/bun"
    chmod 700 "$BUN_DIR/bun"
    "$TIMEOUT" 3600 "$PY" scripts/opencode_setup.py --execute-docker
    up
}
case "$COMMAND" in
    setup) setup;;
    up) up;;
    down)
        for name in worker api stripe-sim; do stop "$name"; done
        if [[ -f "$STRIPE_SIM_MARKER" ]]; then
            load_env
            stripe_sim_prices remove
            rm -f "$STRIPE_SIM_MARKER"
        fi
        if [[ ${1:-} == --all ]]; then docker_access; compose down; fi;;
    status)
        load_env
        for name in api worker; do
            if owned "$name"; then echo "$name: 运行中"; else echo "$name: 未运行或 PID 不匹配"; fi
        done
        if [[ -f .data/run/stripe-sim.pid ]]; then
            if owned stripe-sim; then echo 'stripe-sim: 运行中'; else echo 'stripe-sim: 未运行或 PID 不匹配'; fi
        fi
        docker_access
        compose ps
        request /v1/portal/config
        request /v1/agents/opencode/versions;;
    logs)
        log_files=(.data/run/api.log .data/run/worker.log)
        if [[ -f .data/run/stripe-sim.pid ]]; then log_files+=(.data/run/stripe-sim.log); fi
        run tail -n 100 "${log_files[@]}";;
esac
