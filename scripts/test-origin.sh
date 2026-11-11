#!/usr/bin/env bash
# Run the integration tests against a throwaway standalone Pelican Origin in Docker.
#
#   scripts/test-origin.sh                 # start Origin, run tests, remove Origin
#   scripts/test-origin.sh -k crash -x     # extra arguments go to pytest
#   KEEP_ORIGIN=1 scripts/test-origin.sh   # leave the Origin running afterwards
#
# The Origin listens on https://localhost:18444 so it does not collide with a dev stack on 8444.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
compose=(docker compose -f "$root/tests/integration/compose.yaml")

"${compose[@]}" up -d --wait
if [[ -z "${KEEP_ORIGIN:-}" ]]; then
  trap '"${compose[@]}" down -v' EXIT
fi

cd "$root"
uv run pytest tests/integration "$@"
