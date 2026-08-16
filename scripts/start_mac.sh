#!/usr/bin/env bash
#
# Start FinAlly (macOS / Linux). Idempotent: safe to run repeatedly.
#
#   ./scripts/start_mac.sh            # build if the image is missing, then run
#   ./scripts/start_mac.sh --build    # force a rebuild first
#   ./scripts/start_mac.sh --open     # also open a browser

set -euo pipefail

IMAGE="finally:latest"
CONTAINER="finally"
VOLUME="finally-data"
PORT="${FINALLY_PORT:-8000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FORCE_BUILD=false
OPEN_BROWSER=false
for arg in "$@"; do
  case "$arg" in
    --build) FORCE_BUILD=true ;;
    --open)  OPEN_BROWSER=true ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$ROOT"

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop and try again." >&2
  exit 1
fi

# The backend fails fast without OPENROUTER_API_KEY unless LLM_MOCK=true (PLAN.md §5), so
# catch a missing .env here rather than letting the container exit a second later.
if [[ ! -f .env ]]; then
  echo "No .env found. Creating one from .env.example." >&2
  cp .env.example .env
  echo "Edit .env and add OPENROUTER_API_KEY (or set LLM_MOCK=true), then re-run." >&2
  exit 1
fi

if $FORCE_BUILD || [[ -z "$(docker images -q "$IMAGE" 2>/dev/null)" ]]; then
  echo "Building $IMAGE …"
  docker build -t "$IMAGE" .
fi

# Idempotence: remove any previous container, running or stopped. The VOLUME is untouched,
# so the portfolio survives.
if [[ -n "$(docker ps -aq -f name="^${CONTAINER}$")" ]]; then
  echo "Removing the previous $CONTAINER container (data volume is kept) …"
  docker rm -f "$CONTAINER" >/dev/null
fi

docker volume create "$VOLUME" >/dev/null

echo "Starting $CONTAINER on port $PORT …"
docker run -d \
  --name "$CONTAINER" \
  -p "${PORT}:8000" \
  --env-file .env \
  -v "${VOLUME}:/app/db" \
  --restart unless-stopped \
  "$IMAGE" >/dev/null

printf "Waiting for the app to become healthy "
for _ in $(seq 1 60); do
  status="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo starting)"
  if [[ "$status" == "healthy" ]]; then
    echo ""
    echo "FinAlly is running: http://localhost:${PORT}"
    $OPEN_BROWSER && open "http://localhost:${PORT}"
    exit 0
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]]; then
    echo ""
    echo "The container exited. Logs:" >&2
    docker logs "$CONTAINER" >&2
    exit 1
  fi
  printf "."
  sleep 2
done

echo ""
echo "Timed out waiting for a healthy status. Recent logs:" >&2
docker logs --tail 40 "$CONTAINER" >&2
exit 1
