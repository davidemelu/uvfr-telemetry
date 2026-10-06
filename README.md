# UVFR Live Telemetry

Live pit telemetry for the UVFR Formula SAE car.

**Phase 0 (complete): the whole system, simulated in software.** No CAN
hardware, Raspberry Pi, LoRa radio or race car was used. Phase 0 proves the
architecture, telemetry protocol, database, dashboard, alarms, failure
handling and bandwidth budget before any hardware is bought or connected.

## Architecture

Phase 0, five independent processes plus the database, on one Linux VM:

```text
fake ECU ──▶ vcan0 ──▶ car node ──UDP──▶ simulated radio ──UDP──▶ pit receiver ──▶ InfluxDB ──▶ Grafana
(simulator)            (car_node)        (link_sim: loss,        (pit_receiver:
                       decode, schedule,   latency, jitter,        decode, link health,
                       encode, transmit    outages, bandwidth)     alarms, points)
```

Target hardware, with the same car node and pit receiver code:

```text
Vehicle CAN ──▶ Raspberry Pi 3 + CAN HAT ──▶ USB 915 MHz LoRa ~~~ wireless ~~~ USB 915 MHz LoRa ──▶ pit laptop ──▶ homelab
                (listen-only)
```

See [docs/architecture.md](docs/architecture.md) and
[docs/hardware-transition.md](docs/hardware-transition.md).

## Run the demo

On the lab VM (`ssh uvfr-lab`, then `cd /opt/uvfr-telemetry`):

```bash
make demo                  # vcan0, InfluxDB + Grafana, pit receiver, radio, car node, fake ECU
```

Open Grafana at `http://<lab VM>:3000`. The pit dashboard is the home page;
the login is `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` in the VM's
`.env`. Then:

```bash
make demo-overheating      # scripted: overheat -> telemetry -> pit -> InfluxDB -> Grafana -> alarm
make scenario SCENARIO=low_oil_pressure   # or any fault below; SCENARIO=normal to go back
make link LINK=lora_bad    # degrade the simulated radio
make demo-status
make demo-stop             # ALL=1 also stops InfluxDB and Grafana
```

`make demo-overheating` is the Phase 0 success test made executable. A run
on the lab VM straight after a cold `make demo`:

```text
  [ OK ] fake ECU reports the overheating scenario (via CAN SIM_STATUS)
  [ OK ] coolant rose in the telemetry (81.7 -> 116 degC)
  [ OK ] pit receiver is receiving (link NORMAL, 776 packets this session)
  [ OK ] InfluxDB recorded it (514 coolant samples in the last 2 min)
  [ OK ] Grafana's query API returns the rising coolant (what the graph shows)
  [ OK ] early warning: coolant rising rapidly after 18 s
  [ OK ] coolant alarm WARNING (>= 105 degC) after 43 s
  [ OK ] coolant alarm CRITICAL (>= 110 degC) after 53 s, 1 critical alarm(s) active
```

Setting up a new lab machine: [docs/homelab-deployment.md](docs/homelab-deployment.md).
Any Linux host with the `vcan` module and Docker works.

### What you can play with

| Area | Options |
|---|---|
| Faults (`make scenario SCENARIO=...`) | `normal`, `overheating`, `low_oil_pressure`, `battery_voltage_sag`, `sensor_dropout`, `frozen_sensor`, `can_message_timeout`, `intermittent_can`, `engine_overrev` (combine with commas) |
| Radio (`make link LINK=...`) | `perfect`, `lora_good`, `lora_marginal`, `lora_bad`, `congested`; or `python -m link_sim.ctl set loss_pct=10 latency_ms=300`; `python -m link_sim.ctl outage 5` |
| Recording and replay | `make record DURATION=60`, `make replay LOG=recordings/x.log SPEED=2 LOOP=1` (0.1x to 50x, pause and resume) |
| Bandwidth | `make bandwidth`; live on the dashboard's bandwidth row |
| Raw CAN | `make candump-decoded` |

## Results

- **Bandwidth.** The 13-channel stream needs 2.2 kbit/s encoded (968 bit/s of
  raw values) at 11 packets/s, measured. On LoRa that is 18% airtime at
  SF7 / 500 kHz, 36% at SF7 / 250 kHz, about 70% at SF7 / 125 kHz, and it does
  not fit at SF8 / 125 kHz or slower. Packet rate, not bytes, is the limit.
  See [docs/bandwidth.md](docs/bandwidth.md).
- **Protocol.** 22 to 36 byte packets with sequence numbers, timestamps and
  CRC-16; every single-bit corruption is detected. See [docs/protocol.md](docs/protocol.md).
- **Load.** The four Python processes use under 6% of a VM core. One
  dashboard viewer at a 1 s refresh costs about one core (measured), so the
  dashboard defaults to 2 s.
- **Tests.** About 300 automated tests, including a five-process end-to-end
  test from the fake ECU to InfluxDB.

## Safety principle

The telemetry system is **passive**. It taps the vehicle CAN bus as a
listener and never sits between the ECU and the dash, or between a sensor
and its controller:

```text
ECU ───────── CAN BUS ───────── Dash
                   │
                   └── telemetry listener (listen-only, never transmits)
```

The car node's bus handle has no transmit method, and it refuses to start on
a real CAN interface that is not in listen-only mode. The fake ECU and the
replay tool refuse to transmit on anything but a virtual bus.

## Simulated CAN IDs

Every CAN ID and signal in `dbc/simulated_uvfr.dbc` is **SIMULATED**. They are
not UVFR's real CAN IDs. For the real car, add the real DBC and map channels
to it in `config/channels.yaml`.

## Repository layout

```text
car_node/          runs on the car: receive-only CAN, decode, state, scheduler, encode, transmit
pit_receiver/      runs at the pit: decode, link health, alarms, bandwidth, InfluxDB writer
common/            shared: protocol (packets, channels, framing), transports, config, DBC helpers
simulator/         SIMULATED fake ECU: vehicle model, driver, failure scenarios
link_sim/          SIMULATED radio link (Phase 0 only)
replay/            CAN recording and timed replay
bandwidth/         bandwidth report and LoRa time-on-air model
config/            channels, alerts, simulation, telemetry wiring, dashboard layout
dbc/               SIMULATED CAN database
infrastructure/    VM creation (Proxmox), Docker Compose, Grafana provisioning, firewall
scripts/           VM provisioning, deploy, vcan, demo, dashboard generator
tests/             unit, vcan, InfluxDB and end-to-end tests
docs/              architecture, simulation, protocol, homelab deployment, bandwidth,
                   hardware transition, troubleshooting
```

| Config file | Owns |
|---|---|
| `config/channels.yaml` | which signals are sent, how often, in what encoding (shared by car and pit) |
| `config/alerts.yaml` | every alarm threshold (the dashboard is generated from it) |
| `config/simulation.yaml` | the fake car: vehicle model, driver, track, fault scenarios |
| `config/telemetry.yaml` | process wiring: CAN channel, transports, ports, radio profiles, InfluxDB output |
| `config/dashboard.yaml` | dashboard layout and display ranges |

## Testing

| Command | What | Needs |
|---|---|---|
| `make test` | unit tests (also run by CI on Python 3.11 and 3.13) | Python only |
| `make test-vcan` | multi-process tests on `vcan1` (safe while the demo runs) | Linux with vcan |
| `make test-influx` | real InfluxDB round trip in a temporary bucket | running InfluxDB |
| `make test-e2e` | fake ECU → vcan → car node → radio → pit → InfluxDB | all of the above |
| `make test-all` | everything | all of the above |

## Documentation

| Document | Contents |
|---|---|
| [architecture.md](docs/architecture.md) | processes, data flow, alarms, data schema, failure handling |
| [simulation.md](docs/simulation.md) | fake ECU, vehicle model, scenarios, recording and replay |
| [protocol.md](docs/protocol.md) | binary telemetry protocol, byte by byte |
| [homelab-deployment.md](docs/homelab-deployment.md) | lab VM, Docker backend, secrets, network exposure, load |
| [bandwidth.md](docs/bandwidth.md) | measured stream and LoRa feasibility |
| [hardware-transition.md](docs/hardware-transition.md) | moving to the Pi, CAN HAT, LoRa modems and the real car |
| [troubleshooting.md](docs/troubleshooting.md) | known problems and fixes |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch, commit and pull
request workflow, and for working without access to the homelab.
