# Native services for portal_demo.sh.

PG_DIR="$ROOT/.data/pg"
PG_PASSWORD_FILE="$ROOT/.data/pg-password"
PG_PORT_FILE=.data/run/postgres.port
TEMPORAL="$ROOT/.data/bin/temporal"
LITELLM_VENV="$ROOT/.data/litellm-venv"
LITELLM_VERSION=1.110.2
SOURCE_HELPER="$ROOT/scripts/portal_source.py"

find_postgres() {
    local pg_config_dir='' initdb_dir='' bin
    if command -v pg_config >/dev/null; then pg_config_dir=$(pg_config --bindir 2>/dev/null) || true; fi
    if command -v initdb >/dev/null; then initdb_dir=$(dirname "$(command -v initdb)"); fi
    for bin in "$initdb_dir" "$pg_config_dir" /usr/lib/postgresql/*/bin \
        /opt/homebrew/opt/postgresql@16/bin /usr/local/opt/postgresql@16/bin; do
        if [[ -n "$bin" && -x "$bin/initdb" && -x "$bin/pg_ctl" && -x "$bin/psql" ]]; then
            PG_BIN=$bin
            return
        fi
    done
    fail '缺少 PostgreSQL 工具。Linux: sudo apt install postgresql。macOS: brew install postgresql@16。'
}

load_source_env() {
    export SCA_DATA_DIR="$ROOT/.data" SCA_SANDBOX_DRIVER=local SCA_STATE_TRANSPORT=sync
    export SCA_TEMPORAL_ADDRESS=127.0.0.1:7233 SCA_MODEL_GATEWAY_URL=http://127.0.0.1:4000
    export SCA_OPENCODE_DEFAULT_VERSION=2.3.0-sca.0 SCA_OPENCODE_VERSIONS_DIR="$ROOT/.data/bin/opencode"
    if [[ -f "$PG_PORT_FILE" && -f "$PG_PASSWORD_FILE" ]]; then
        local port password
        read -r port < "$PG_PORT_FILE"
        read -r password < "$PG_PASSWORD_FILE"
        export SCA_DATABASE_URL="postgresql+asyncpg://sca:$password@127.0.0.1:$port/sca"
        LITELLM_DB_URL="postgresql://sca:$password@127.0.0.1:$port/litellm"
    fi
}

postgres_running() {
    [[ -f "$PG_DIR/postmaster.pid" ]] && run "$PG_BIN/pg_ctl" -D "$PG_DIR" status >/dev/null 2>&1
}

start_postgres() {
    local port password
    if [[ ! -f "$PG_DIR/PG_VERSION" ]]; then
        if [[ ! -f "$PG_PASSWORD_FILE" ]]; then run "$PY" "$SOURCE_HELPER" postgres-password "$PG_PASSWORD_FILE"; fi
        run "$PG_BIN/initdb" -D "$PG_DIR" -U sca --auth=scram-sha-256 \
            --encoding=UTF8 --no-locale --pwfile="$PG_PASSWORD_FILE" >> .data/run/postgres.log 2>&1 ||
            fail '初始化 PostgreSQL 失败，请查看 .data/run/postgres.log。'
    fi
    [[ -f "$PG_PASSWORD_FILE" ]] || fail '本机 PostgreSQL 密码文件缺失，请恢复 .data/pg-password。'
    if ! postgres_running; then
        if [[ -f "$PG_PORT_FILE" ]] && { owned api || owned worker || owned litellm; }; then
            read -r port < "$PG_PORT_FILE"
            run "$PY" "$SOURCE_HELPER" port "$port" --strict >/dev/null
        else
            port=$(run "$PY" "$SOURCE_HELPER" port 5432)
        fi
        run "$PG_BIN/pg_ctl" -D "$PG_DIR" -l "$ROOT/.data/run/postgres.log" -w -t 60 \
            -o "-h 127.0.0.1 -p $port -c unix_socket_directories=''" start ||
            fail '启动 PostgreSQL 失败，请查看 .data/run/postgres.log。'
    fi
    sed -n '1p' "$PG_DIR/postmaster.pid" > .data/run/postgres.pid
    local pid
    read -r pid < .data/run/postgres.pid
    run ps -p "$pid" -o lstart= -o args= > .data/run/postgres.identity
    sed -n '4p' "$PG_DIR/postmaster.pid" > "$PG_PORT_FILE"
    read -r port < "$PG_PORT_FILE"
    read -r password < "$PG_PASSWORD_FILE"
    PGPASSWORD="$password" run "$PG_BIN/psql" -X -h 127.0.0.1 -p "$port" -U sca -d postgres \
        -v ON_ERROR_STOP=1 >> .data/run/postgres.log 2>&1 <<'SQL' ||
SELECT 'CREATE DATABASE sca' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'sca')\gexec
SELECT 'CREATE DATABASE litellm' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'litellm')\gexec
SQL
        fail '初始化数据库失败，请查看 .data/run/postgres.log。'
    load_source_env
    run "$ROOT/.venv/bin/alembic" upgrade head
}

stop_postgres() {
    if [[ -f "$PG_DIR/postmaster.pid" ]]; then
        find_postgres
        if postgres_running; then
            run "$PG_BIN/pg_ctl" -D "$PG_DIR" -w -t 60 -m fast stop ||
                fail '停止 PostgreSQL 失败，请查看 .data/run/postgres.log。'
        fi
    fi
    rm -f .data/run/postgres.pid .data/run/postgres.identity
}

prepare_litellm() {
    local stamp="$LITELLM_VENV/.sca-version" litellm_python="$LITELLM_VENV/bin/python" schema
    if [[ -x "$litellm_python" && -f "$stamp" && $(cat "$stamp") == "$LITELLM_VERSION" ]]; then return; fi
    local tool
    for tool in node npm; do
        command -v "$tool" >/dev/null || fail 'LiteLLM 需要 Node.js 和 npm。Linux: sudo apt install nodejs npm。macOS: brew install node。'
    done
    echo '安装本机 LiteLLM 网关及其 PostgreSQL 客户端…'
    [[ -x "$litellm_python" ]] || run "$PY" -m venv "$LITELLM_VENV"
    run "$litellm_python" -m pip install -q "litellm[proxy]==$LITELLM_VERSION" \
        'prisma==0.11.0' 'psycopg[binary]>=3.2,<4' >> .data/run/litellm.log 2>&1 ||
        fail '安装 LiteLLM 失败，请查看 .data/run/litellm.log。'
    schema=$(run "$litellm_python" -c 'from importlib.resources import files; print(files("litellm_proxy_extras").joinpath("schema.prisma"))')
    (
        cd "$LITELLM_VENV"
        export PATH="$LITELLM_VENV/bin:$PATH" PRISMA_BINARY_CACHE_DIR="$LITELLM_VENV/prisma-cache"
        run "$LITELLM_VENV/bin/prisma" generate --schema "$schema"
    ) >> .data/run/litellm.log 2>&1 || fail '生成 LiteLLM 数据库客户端失败，请查看 .data/run/litellm.log。'
    printf '%s\n' "$LITELLM_VERSION" > "$stamp"
}

wait_service() {
    local name=$1 attempts=$2 i
    shift 2
    for ((i=0; i<attempts; i++)); do
        owned "$name" || fail "$name 启动失败，请查看 .data/run/$name.log。"
        if "$@" >/dev/null 2>&1; then return; fi
        sleep 1
    done
    fail "$name 启动超时，请查看 .data/run/$name.log。"
}

source_port() {
    if ! owned "$1"; then run "$PY" "$SOURCE_HELPER" port "$2" --strict >/dev/null; fi
}

start_litellm() {
    : "${LITELLM_DB_URL:?请先启动源码模式 PostgreSQL。}"
    source_port litellm 4000
    local config
    config=$(gateway_config)
    DATABASE_URL="$LITELLM_DB_URL" PATH="$LITELLM_VENV/bin:$PATH" \
        PRISMA_BINARY_CACHE_DIR="$LITELLM_VENV/prisma-cache" \
        start litellm "$LITELLM_VENV/bin/python" "$SOURCE_HELPER" gateway "$ROOT/deploy/litellm/$config"
    wait_service litellm "${1:-300}" "$PY" -c \
        'from urllib.request import urlopen; urlopen("http://127.0.0.1:4000/health/readiness", timeout=1).close()'
}

start_source_services() {
    run "$PY" "$SOURCE_HELPER" install "$ROOT/.data/bin"
    prepare_litellm
    start_postgres
    mkdir -p .data/temporal
    start temporal "$TEMPORAL" server start-dev --db-filename "$ROOT/.data/temporal/dev.db" \
        --ip 127.0.0.1 --headless
    wait_service temporal 60 "$TEMPORAL" operator cluster health --address "$SCA_TEMPORAL_ADDRESS" --command-timeout 1s
    start_litellm
}

stop_source_services() {
    stop litellm
    stop temporal
    stop_postgres
}
