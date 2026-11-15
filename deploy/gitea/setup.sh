#!/usr/bin/env bash
# Start a local Gitea with an Actions runner and write its credentials to .data/gitea/env.
# Idempotent. Usage: deploy/gitea/setup.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
COMPOSE=(docker compose -f deploy/docker-compose.yml --profile gitea)
URL=http://localhost:3000
OUT=.data/gitea/env
mkdir -p .data/gitea
umask 077

"${COMPOSE[@]}" up -d gitea
until curl -sf "$URL/api/healthz" >/dev/null; do sleep 2; done

if [ ! -s "$OUT" ]; then
  user=sca-admin
  password=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  docker exec -u git sca-gitea gitea admin user create --admin --username "$user" \
    --password "$password" --email "$user@example.invalid" --must-change-password=false >/dev/null
  token=$(curl -sf -u "$user:$password" -H 'Content-Type: application/json' \
    -d '{"name":"sca","scopes":["all"]}' "$URL/api/v1/users/$user/tokens" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["sha1"])')
  printf 'GITEA_URL=%s\nGITEA_USER=%s\nGITEA_PASSWORD=%s\nGITEA_TOKEN=%s\n' \
    "$URL" "$user" "$password" "$token" > "$OUT"
fi

if ! docker volume inspect sca_gitearunner >/dev/null 2>&1 ||
   ! docker run --rm -v sca_gitearunner:/d alpine test -s /d/.runner; then
  GITEA_RUNNER_TOKEN=$(docker exec -u git sca-gitea gitea actions generate-runner-token | tail -n1)
  export GITEA_RUNNER_TOKEN
fi
"${COMPOSE[@]}" up -d --build gitea-runner
echo "Gitea ready at $URL; credentials in $OUT"
