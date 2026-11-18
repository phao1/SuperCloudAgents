#!/usr/bin/env bash
set -euo pipefail
# SCA_MODEL_KEY is read only by the host; no credential is passed to docker.
: "${SCA_MODEL_KEY:?Set a per-turn gateway key}"
: "${SCA_BRAIN_GATEWAY_URL:?Set the host-facing gateway /v1 URL}"
: "${SCA_BRAIN_MODEL:?Set the gateway model name}"
root="$(cd "$(dirname "$0")/.." && pwd)"
name="sca-brain-smoke-$(timeout 5 cat /proc/sys/kernel/random/uuid)"
session="$(timeout 5 mktemp -d)"
trap 'timeout 20 docker rm -f "$name" >/dev/null 2>&1 || true; timeout 5 rm -rf "$session"' EXIT
timeout 30 docker run -d --network=none --name "$name" --entrypoint sh "${SCA_BRAIN_IMAGE:-sca-sandbox:latest}" -c 'mkdir -p /workspace; sleep 900' >/dev/null
export SCA_BRAIN_CONTAINER="$name" SCA_BRAIN_SESSION="$session"
timeout 880 "$root/.venv/bin/python" - <<'PY' | timeout 890 "$root/.venv/bin/python" "$root/deploy/opencode/brain/launch.py" "$root/.data/opencode/src" "$root/.data/tools/bun-linux-$(case $(uname -m) in arm64|aarch64) echo arm64;; *) echo x64;; esac)/bun"
import json
import os
print(json.dumps({"prompt": "Use the shell tool to write brain-ok to /workspace/brain-proof.txt, then reply done.",
 "model": os.environ["SCA_BRAIN_MODEL"], "gateway_url": os.environ["SCA_BRAIN_GATEWAY_URL"],
 "container": os.environ["SCA_BRAIN_CONTAINER"], "workdir": "/workspace",
 "session_dir": os.environ["SCA_BRAIN_SESSION"], "task_id": "smoke", "docker_prefix": ["docker"]}))
PY
timeout 20 docker exec "$name" grep -q brain-ok /workspace/brain-proof.txt
