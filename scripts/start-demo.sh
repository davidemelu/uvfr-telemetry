#!/usr/bin/env bash
# Start the complete Phase 0 demonstration (lab VM, or any Linux host with vcan + Docker).
#
#   ./scripts/start-demo.sh                       # or: make demo
#   LINK=lora_marginal ./scripts/start-demo.sh    # radio profile (default lora_good)
#   SCENARIO=overheating ./scripts/start-demo.sh  # start with a fault (default normal)
#
# Brings up, in order:
#   vcan0 -> InfluxDB + Grafana (Docker) -> pit receiver -> simulated radio
#   -> car node -> fake ECU
# Safe to run again: it restarts the Python processes and leaves the
# database running. Stop with ./scripts/stop-demo.sh (make demo-stop).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY=.venv/bin/python
LINK="${LINK:-lora_good}"
SCENARIO="${SCENARIO:-normal}"
CAN_IFACE="${CAN_IFACE:-vcan0}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == Linux ]] || die "the demo needs Linux (SocketCAN vcan)"
command -v docker >/dev/null || die "Docker is not installed (see docs/homelab-deployment.md)"
docker info >/dev/null 2>&1 || die "cannot talk to Docker (is your user in the docker group?)"
mkdir -p run logs

if [[ ! -x "$PY" ]]; then
    say "creating the Python virtualenv"
    make -s venv
fi

say "virtual CAN bus $CAN_IFACE"
./scripts/setup-vcan.sh "$CAN_IFACE" >/dev/null

say "InfluxDB + Grafana (Docker)"
make -s infra-up > logs/infra-up.log 2>&1 || { tail -20 logs/infra-up.log; die "make infra-up failed (log: logs/infra-up.log)"; }

./scripts/stop-demo.sh --quiet

start() {
    local name="$1"
    shift
    setsid nohup "$PY" -m "$@" > "logs/$name.log" 2>&1 < /dev/null &
    echo $! > "run/$name.pid"
}

say "pit receiver"
start pit_receiver pit_receiver
sleep 0.5
say "simulated radio (profile $LINK)"
start link_sim link_sim --profile "$LINK"
say "car telemetry node"
start car_node car_node --channel "$CAN_IFACE"
sleep 0.5
say "fake ECU (scenario $SCENARIO)"
start fake_ecu simulator --channel "$CAN_IFACE" --scenario "$SCENARIO"

say "waiting for telemetry to reach the pit"
for _ in $(seq 1 40); do
    if grep -q "LINK NORMAL" logs/pit_receiver.log 2>/dev/null; then
        break
    fi
    sleep 0.5
done
for name in pit_receiver link_sim car_node fake_ecu; do
    kill -0 "$(cat "run/$name.pid")" 2>/dev/null || { tail -20 "logs/$name.log"; die "$name did not start (log: logs/$name.log)"; }
done
grep -q "LINK NORMAL" logs/pit_receiver.log || die "no telemetry at the pit after 20 s (see logs/*.log)"

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

  UVFR telemetry demo is running (all CAN data is SIMULATED).

  Grafana        http://${ip:-localhost}:3000   (login: GRAFANA_ADMIN_USER / GRAFANA_ADMIN_PASSWORD in .env)
  Pit receiver   tail -f logs/pit_receiver.log
  Live CAN       make candump-decoded

  Faults         make scenario SCENARIO=overheating
                 (also low_oil_pressure, battery_voltage_sag, sensor_dropout, frozen_sensor,
                  can_message_timeout, intermittent_can, engine_overrev; back with SCENARIO=normal)
  Radio link     make link LINK=lora_bad        (perfect, lora_good, lora_marginal, lora_bad, congested)
                 .venv/bin/python -m link_sim.ctl outage 5
  Walk-through   make demo-overheating          (scripted: overheat -> telemetry -> InfluxDB -> Grafana -> alarm)

  Status         make demo-status
  Stop           make demo-stop                 (add ALL=1 to stop InfluxDB and Grafana too)

EOF
