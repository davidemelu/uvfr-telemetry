# UVFR Live Telemetry

Live pit telemetry for the UVFR Formula SAE car.

**Current phase: Phase 0, software-only simulation (in progress).**
No CAN hardware, Raspberry Pi, LoRa radio or race car is used in this phase.
The goal is to prove the software architecture, telemetry protocol, database,
dashboard, alarms, failure handling and bandwidth budget before any hardware is
bought or connected.

## Architecture

Phase 0 (simulated, runs on one Linux VM):

```text
Fake ECU ──▶ SocketCAN vcan0 ──▶ Car telemetry node ──▶ Simulated radio link ──▶ Pit receiver ──▶ InfluxDB ──▶ Grafana
                                  (decode, schedule,     (loss, latency,
                                   encode, transmit)      jitter, outages)
```

Target hardware (later phases, not implemented yet):

```text
Vehicle CAN ──▶ Raspberry Pi 3 + CAN HAT ──▶ USB 915 MHz LoRa ~~~ wireless ~~~ USB 915 MHz LoRa ──▶ Pit laptop ──▶ Homelab
```

## Safety principle

The telemetry system is **passive**. It taps the vehicle CAN bus as a
listener and must never sit between the ECU and the dash, or between any
safety-critical sensor and its controller:

```text
ECU ───────── CAN BUS ───────── Dash
                   │
                   └── telemetry listener (listen-only, never transmits)
```

The car node contains no CAN transmit path. On real hardware the CAN
interface is run in listen-only (silent) mode.

## Simulated CAN IDs

Every CAN ID and signal in `dbc/simulated_uvfr.dbc` is **SIMULATED**. They are
not UVFR's real CAN IDs. When real test-day data is available, swap in the real
DBC and update the channel mapping in `config/channels.yaml`.

## Repository status

| Part | Status |
|---|---|
| Homelab lab VM, vcan0 | Done: [docs/homelab-deployment.md](docs/homelab-deployment.md) |
| Simulated DBC, fake ECU, failure scenarios | Done: [docs/simulation.md](docs/simulation.md) |
| Car node (decoder, scheduler, binary protocol, transport) | Done: [docs/protocol.md](docs/protocol.md) |
| Simulated radio link, pit receiver | Done: [docs/architecture.md](docs/architecture.md) |
| InfluxDB, Grafana dashboard, alerts | Done: [docs/homelab-deployment.md](docs/homelab-deployment.md) |
| Bandwidth measurement, recording and replay | Done: [docs/bandwidth.md](docs/bandwidth.md), [docs/simulation.md](docs/simulation.md) |
| End-to-end demo, hardware-transition document | Next |

**Bandwidth headline:** the 13-channel stream needs 2.2 kbit/s encoded
(968 bit/s of raw values) at 11 packets/s. On LoRa that is 18% airtime at
SF7 / 500 kHz, 36% at SF7 / 250 kHz, about 70% at SF7 / 125 kHz, and does not
fit at SF8 / 125 kHz or slower. Details in [docs/bandwidth.md](docs/bandwidth.md).

## Quick start (so far)

On a Linux host with the `vcan` module (the lab VM is already set up):

```bash
make venv                     # Python virtualenv with pinned dependencies
make vcan                     # create vcan0 (sudo)
make sim SCENARIO=normal      # fake ECU on vcan0; Ctrl+C to stop
make candump-decoded          # in another terminal: decoded live traffic
make car-node                 # car telemetry node: vcan0 in, binary telemetry out (UDP 47001)
make link-sim LINK=lora_good  # simulated radio: UDP 47001 -> impairment -> UDP 47002
make pit                      # pit receiver: decode, link health, alarms, InfluxDB
make scenario SCENARIO=overheating   # switch the running fake ECU
make link LINK=lora_bad       # switch the running radio link profile
make record DURATION=60       # record vcan0 to recordings/
make replay LOG=recordings/x.log SPEED=2 LOOP=1   # replay (stop the fake ECU first)
make bandwidth                # bandwidth and LoRa airtime report
make test                     # unit tests
make test-vcan                # multi-process tests on vcan1 (safe while the demo uses vcan0)
```

Backend (Docker):

```bash
make infra-up                 # InfluxDB + Grafana, secrets in .env, scoped tokens
make firewall                 # once on the lab VM: published ports limited to LAN/VPN
make dashboard                # regenerate the Grafana dashboard from config/*.yaml
make test-influx              # round trip against the running InfluxDB
```

Grafana: `http://<lab VM>:3000`, login in the VM's `.env`
(`GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch, commit and pull-request
conventions, and for how to run the project without access to the homelab.
