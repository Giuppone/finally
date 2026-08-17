# Shared runner for the portfolio harness scripts. Sourced, never executed.
#
# Finds a Python that can run backend/scripts/portfolio_tool.py and hands off to it. The
# tool is stdlib-only, so a bare interpreter is enough - uv is a fallback, not a
# requirement, and a host with neither still works through the container.

# Prints the argv needed to run the tool, or returns 1 if nothing on this machine can.
_finally_python() {
  local root="$1"
  local tool="$root/backend/scripts/portfolio_tool.py"
  local candidate

  # A bare interpreter first: `uv run` re-checks the lockfile on every invocation, which
  # is wasted work for a script that imports nothing outside the stdlib.
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
         >/dev/null 2>&1; then
      echo "$candidate|$tool"
      return 0
    fi
  done

  if command -v uv >/dev/null 2>&1 && [ -f "$root/backend/pyproject.toml" ]; then
    echo "uv|--directory|$root/backend|run|python|scripts/portfolio_tool.py"
    return 0
  fi

  # No Python on the host. The image already carries backend/scripts at /app/scripts
  # (the Dockerfile's `COPY backend/ ./`), so the running container can do it - but the
  # app is then on loopback INSIDE the container, so --base has to be overridden. It is
  # prepended, not appended, so a user-supplied --base still wins: argparse takes the last.
  if command -v docker >/dev/null 2>&1 &&
     [ -n "$(docker ps -q -f name='^finally$' 2>/dev/null)" ]; then
    echo "docker|exec|-i|finally|python|/app/scripts/portfolio_tool.py"
    return 0
  fi

  return 1
}

run_portfolio_tool() {
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

  local runner
  if ! runner="$(_finally_python "$root")"; then
    echo "No way to run the portfolio tool on this machine." >&2
    echo "Install Python 3.9+, or start the container first:" >&2
    echo "  ./scripts/start_mac.sh            # then re-run this script" >&2
    return 1
  fi

  # IFS scoped to the read alone - a `local IFS` that is later unset leaves the rest of
  # the function splitting on something other than the default.
  local -a argv=()
  IFS='|' read -r -a argv <<< "$runner"

  if [ "${argv[0]}" = "docker" ]; then
    # Sessions are files, and inside the container the default sessions/ path resolves to
    # /sessions -- ephemeral, and invisible from the host. The seeders are fine over docker
    # exec because they only talk HTTP; save and load are not, so say so rather than
    # writing a file that vanishes with the container.
    case "$1" in
      save|load)
        echo "save/load need Python on the host: they read and write files under" >&2
        echo "sessions/, and the container's filesystem is not the host's." >&2
        echo "Install Python 3.9+ (or run the tool yourself with an explicit --file" >&2
        echo "inside the container)." >&2
        return 1
        ;;
    esac
    "${argv[@]}" "$1" --base http://127.0.0.1:8000 "${@:2}"
  else
    "${argv[@]}" "$@"
  fi
}
