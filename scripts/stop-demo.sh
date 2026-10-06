#!/usr/bin/env bash
# Stop the demo's Python processes (and with --all, InfluxDB and Grafana too).
#
#   ./scripts/stop-demo.sh            # or: make demo-stop
#   ./scripts/stop-demo.sh --all      # or: make demo-stop ALL=1
#
# Processes are found through run/<name>.pid and only signalled if that PID
# still belongs to the expected python -m <module> command.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

QUIET=0
ALL=0
for arg in "$@"; do
    case "$arg" in
        --quiet) QUIET=1 ;;
        --all) ALL=1 ;;
        *) echo "usage: $0 [--all] [--quiet]" >&2; exit 2 ;;
    esac
done
say() { [[ $QUIET -eq 1 ]] || printf '\033[1;34m==>\033[0m %s\n' "$*"; }

declare -A MODULE=([fake_ecu]=simulator [car_node]=car_node [link_sim]=link_sim [pit_receiver]=pit_receiver)

# Upstream first, so receivers see a clean end of stream.
for name in fake_ecu car_node link_sim pit_receiver; do
    pidfile="run/$name.pid"
    [[ -f "$pidfile" ]] || continue
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o args= | grep -q -- "-m ${MODULE[$name]}"; then
        kill -TERM "$pid"
        for _ in $(seq 1 25); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
        say "stopped $name (pid $pid)"
    fi
    rm -f "$pidfile"
done

if [[ $ALL -eq 1 ]]; then
    say "stopping InfluxDB and Grafana (data stays in the Docker volumes)"
    make -s infra-down
fi
