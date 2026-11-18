#!/usr/bin/env bash
set -euo pipefail
timeout 120 kind load docker-image sca-egress:tls-v1 --name "${1:-sca}"
