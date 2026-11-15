#!/usr/bin/env bash
# Register and start a GitHub Actions self-hosted runner for ONE private repository.
# Usage: deploy/ci/github-runner.sh <owner/repo> [start|stop]
# Needs: gh logged in with the `repo` scope (and `workflow` to push pipeline files).
# Never point this at a public repository.
set -euo pipefail
repo=${1:?usage: github-runner.sh <owner/repo> [start|stop]}
action=${2:-start}
cd "$(dirname "$0")/../.."
dir=.data/github-runner/${repo//\//_}

if [ "$action" = stop ]; then
  # run.sh leads its own process group (setsid below); stop the listener it spawned too.
  [ -f "$dir/pid" ] && kill -- "-$(cat "$dir/pid")" 2>/dev/null || true
  rm -f "$dir/pid"
  exit 0
fi

[ "$(gh api "repos/$repo" --jq .private)" = true ] || { echo "refusing: $repo is not private" >&2; exit 1; }
mkdir -p "$dir"
if [ ! -x "$dir/run.sh" ]; then
  version=$(gh api repos/actions/runner/releases/latest --jq .tag_name | sed 's/^v//')
  curl -sSL "https://github.com/actions/runner/releases/download/v$version/actions-runner-linux-x64-$version.tar.gz" |
    tar -xz -C "$dir"
fi
if [ ! -f "$dir/.runner" ]; then
  token=$(gh api -X POST "repos/$repo/actions/runners/registration-token" --jq .token)
  (cd "$dir" && ./config.sh --unattended --url "https://github.com/$repo" --token "$token" \
    --name "sca-$(hostname)" --labels sca-ci --replace >/dev/null)
fi
if [ -f "$dir/pid" ] && kill -0 "$(cat "$dir/pid")" 2>/dev/null; then
  echo "runner already running"; exit 0
fi
# Detach fully (own session, no inherited stdin) so callers piping our output do not hang.
(cd "$dir" && { setsid nohup ./run.sh > runner.log 2>&1 < /dev/null & echo $! > pid; })
echo "runner started for $repo"
