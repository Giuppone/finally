#!/usr/bin/env bash
#
# Stop FinAlly (macOS / Linux). Idempotent, and it never removes the data volume — the
# portfolio, trade history and conversation are meant to survive a restart (PLAN.md §11).
#
# To wipe the data too, do it explicitly:  docker volume rm finally-data

set -euo pipefail

CONTAINER="finally"

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running; nothing to stop."
  exit 0
fi

if [[ -z "$(docker ps -aq -f name="^${CONTAINER}$")" ]]; then
  echo "No $CONTAINER container found; nothing to stop."
  exit 0
fi

docker rm -f "$CONTAINER" >/dev/null
echo "Stopped and removed $CONTAINER. The finally-data volume was kept."
