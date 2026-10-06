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

Phase 0 is being built in this order: homelab VM, vcan0, DBC, fake ECU, car
node (decoder, scheduler, binary protocol, transport), simulated radio link,
pit receiver, InfluxDB, Grafana, alerts, bandwidth measurement, recording and
replay, automated tests, end-to-end demo, then the hardware-transition
document. This README is expanded as each part lands.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch, commit and pull-request
conventions, and for how to run the project without access to the homelab.
