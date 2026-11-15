#!/bin/sh
# Report a CI result for a SuperCloudAgents task. Needs only git, curl, awk and a POSIX shell,
# so it works in any CI system.
#
#   <command producing a summary> | SCA_API_URL=… SCA_CI_TOKEN=… report.sh passed|failed
#
# The task id comes from the `Sca-Task-Id:` trailer the platform puts on its commits. Commits
# without it are not the platform's: the script exits 0 without calling anything.
# Optional: SCA_CI_RUN_URL, a link to this run (defaults to the GitHub/Gitea Actions run URL).
set -eu

case "${1:-}" in
  passed | success) passed=true ;;
  failed | failure | cancelled) passed=false ;;
  *) echo "usage: report.sh passed|failed  (summary on stdin)" >&2; exit 2 ;;
esac

task=$(git log -1 --format='%(trailers:key=Sca-Task-Id,valueonly)' | head -n 1 | tr -d '[:space:]')
if [ -z "$task" ]; then
  echo "report.sh: HEAD is not a SuperCloudAgents commit; nothing to report"
  exit 0
fi
: "${SCA_API_URL:?SCA_API_URL is required}"
: "${SCA_CI_TOKEN:?SCA_CI_TOKEN is required}"
commit=$(git rev-parse HEAD)
run_url=${SCA_CI_RUN_URL:-}
if [ -z "$run_url" ] && [ -n "${GITHUB_SERVER_URL:-}" ]; then
  run_url="$GITHUB_SERVER_URL/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
fi

json() { # stdin -> a JSON string literal
  tr -d '\000-\010\013\014\016-\037' | awk 'BEGIN { ORS = ""; printf "\"" }
    { gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); gsub(/\t/, "\\t"); gsub(/\r/, "")
      if (NR > 1) printf "\\n"; print }
    END { printf "\"" }'
}
summary=$(tail -c 20000 | json)
url=$(printf '%s' "$run_url" | json)

curl -sS --fail-with-body -X POST \
  -H "Authorization: Bearer $SCA_CI_TOKEN" -H 'Content-Type: application/json' \
  --data-binary @- "${SCA_API_URL%/}/v1/tasks/$task/ci-result" >/dev/null <<JSON
{"commit": "$commit", "passed": $passed, "summary": $summary, "url": $url}
JSON
echo "report.sh: reported passed=$passed for task $task at $commit"
