#!/usr/bin/env bash
# Show what the demo is doing: processes, scenario, radio profile, link and alarms.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
PY=.venv/bin/python

for name in pit_receiver link_sim car_node fake_ecu; do
    pid="$(cat "run/$name.pid" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        printf '  %-13s running (pid %s)\n' "$name" "$pid"
    else
        printf '  %-13s stopped\n' "$name"
    fi
done
docker ps --filter name=uvfr- --format '  {{.Names}}  {{.Status}}' 2>/dev/null

scenario="$($PY -m simulator.ctl status 2>/dev/null | $PY -c 'import json,sys; d=json.load(sys.stdin); print("+".join(d["scenarios"]) or "normal")' 2>/dev/null)"
profile="$($PY -m link_sim.ctl status 2>/dev/null | $PY -c 'import json,sys; print(json.load(sys.stdin)["profile"])' 2>/dev/null)"
echo "  scenario      ${scenario:-unknown (fake ECU not reachable)}"
echo "  radio         ${profile:-unknown (link_sim not reachable)}"
echo
grep -E "LINK" logs/pit_receiver.log 2>/dev/null | tail -1 | sed 's/^/  /' || true
alarms="$(grep -E "ALARM" logs/pit_receiver.log 2>/dev/null | tail -5 || true)"
if [[ -n "$alarms" ]]; then
    echo "  recent alarm changes:"
    sed 's/^/    /' <<< "$alarms"
else
    echo "  no alarm changes since the pit receiver started"
fi
exit 0
