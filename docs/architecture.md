# Architecture

## Goal of Phase 0

Prove the software architecture, telemetry protocol, database, dashboard,
alarms, failure handling and bandwidth budget entirely in software, on one
Linux VM, before any hardware is bought or connected.

## Processes and data flow

```text
                         Phase 0 (all on the lab VM)

 fake ECU ──CAN──▶ vcan0 ──CAN──▶ car node ──UDP:47001──▶ link_sim ──UDP:47002──▶ pit receiver ──▶ InfluxDB ──▶ Grafana
 (simulator)       (kernel)       (car_node)             (simulated radio)        (pit_receiver)
     ▲                                                        ▲
     │ UDP:47020 control                                      │ UDP:47010 control
 simulator.ctl                                            link_sim.ctl
```

| Process | Package | Job | Talks to |
|---|---|---|---|
| Fake ECU | `simulator/` | SIMULATED vehicle traffic and failure scenarios | writes `vcan0` only |
| Car node | `car_node/` | receive CAN, decode, keep state, select, package, transmit | reads `vcan0`, sends UDP (later serial) |
| Simulated radio | `link_sim/` | loss, latency, jitter, corruption, outages, bandwidth cap | UDP in, UDP out |
| Pit receiver | `pit_receiver/` | validate, decode, measure the link, keep latest values, emit points | UDP in (later serial), sinks out |
| InfluxDB, Grafana | `infrastructure/` | storage and dashboard | written by the pit receiver |

Every process is independent: each can be stopped, restarted or replaced
without touching the others. The car node and pit receiver share only the
protocol definition (`common/protocol/`) and `config/channels.yaml`.

### Target hardware (later phases)

```text
Vehicle CAN ──▶ Raspberry Pi 3 + CAN HAT ──▶ USB 915 MHz LoRa  ~~~ wireless ~~~  USB 915 MHz LoRa ──▶ pit laptop ──▶ homelab
                (car node, unchanged)        (serial transport)                  (serial transport)    (pit receiver)  (InfluxDB, Grafana)
```

What changes: the CAN channel (`vcan0` to `can0`, listen-only), the car node
transport (UDP to serial), and the pit receiver transport (UDP to serial).
The fake ECU and `link_sim` are removed. See `docs/hardware-transition.md`.

## Inside the car node

```text
SocketCAN (receive-only, kernel ID filters)
   │  python-can Message
   ▼
FrameDecoder      DBC decode, sensor-fault detection, rolling-counter gaps
   │  {signal: value | None}
   ▼
VehicleState      latest value + receive time of every signal
   │
   ▼  every 100 ms (base_rate_hz)
ChannelScheduler  which channels are due this tick (per-channel rates, phase-spread)
   │
   ▼
Codec             TELEMETRY packet (and STATUS once a second)
   │  bytes
   ▼
Transport         UDP now, serial (COBS-framed) later
```

Key properties:

- **Passive.** The bus handle (`ReceiveOnlyBus`) has no `send()`. On a real
  CAN interface the node refuses to start unless the controller is in
  listen-only mode. Tests enforce both, and scan the package for transmit calls.
- **Decoupled from bus load.** CAN frames only update state; the radio sends
  on its own schedule. 231 CAN frames/s become 11 packets/s.
- **No pit dependencies.** Nothing in `car_node/` knows about InfluxDB,
  Grafana or the pit receiver, so it can run unchanged on a Raspberry Pi 3
  (it uses about 2% of one VM core today).
- **Two kinds of staleness.** If a CAN message times out on the car, the
  channel is transmitted as STALE. The pit can tell this apart from radio loss.

## Inside the pit receiver

```text
Transport ──▶ Codec.decode ──▶ LinkStats      sequence gaps, loss %, rate, staleness, uptime, sessions
                  │                │
                  │ rejected       ▼
                  │ (reason)   SourceClock     car timestamp ──▶ pit wall time (jitter removed)
                  ▼                │
               counters            ▼
                              ChannelTracker   latest value and liveness of every channel
                                   │
                                   ▼
                                Points ──▶ sinks (InfluxDB, JSON lines)
```

- **Validation first.** Packets with a bad CRC, unknown version or type, or a
  wrong length are counted by reason and never written.
- **Link health from what arrives.** Loss, gaps, duplicates, reordering,
  packet rate, last-packet age, uptime and availability are all derived at
  the pit, so they work the same for UDP, LoRa or an RFD900x.
- **Measurement time, not arrival time.** Each sample is timestamped when the
  car measured it, using the minimum observed car-to-pit offset. Radio jitter
  therefore does not smear the plots, and the Pi's clock never has to be right.
- **Sinks.** The receiver produces database-agnostic `Point`s. A failing sink
  (for example InfluxDB down) is logged and skipped; it never stops reception.

### Status vocabulary

The pit uses five statuses everywhere (link, channels, alarms):

| Status | Code | Meaning |
|---|---|---|
| NORMAL | 0 | live and within limits |
| WARNING | 1 | live, outside the warning threshold (or link degraded) |
| CRITICAL | 2 | live, outside the critical threshold (or telemetry lost) |
| STALE | 3 | value too old: CAN timeout on the car, or not received at the pit |
| NO DATA | 4 | never received, or the sensor reports a fault |

## Points written

| Measurement | Rate | Tags | Fields |
|---|---|---|---|
| `vehicle` | per telemetry packet (10/s) | `car` | one field per live channel (`rpm`, `coolant_temperature`, ...) |
| `car_status` | 1/s | `car` | CAN frames/s, counter gaps, decode errors, CPU %, per-message CAN ages, layout match |
| `link` | 1/s | `car` | status, reason, packets received/expected/missing, loss % (total and recent), packet and byte rate, last-packet age, uptime, availability, rejected by reason, excess delay |
| `channel_state` | 1/s per channel | `car`, `channel` | status, status code, age, last value |
| `sim` | 1/s (lab only) | `car` | active fault scenario |

## Configuration

| File | Owns |
|---|---|
| `config/simulation.yaml` | fake ECU: vehicle model, driver, track, scenario parameters |
| `config/channels.yaml` | which signals are sent, at what rate, in what encoding (shared by car and pit) |
| `config/telemetry.yaml` | process wiring: CAN channel, transports, ports, link profiles, pit settings |
| `config/alerts.yaml` | every threshold: link health, staleness, alarms |
| `dbc/simulated_uvfr.dbc` | SIMULATED CAN database |

All loaders are strict: unknown keys, missing keys and inconsistent values are
rejected at start-up with a message naming the file and key.

## Repository layout and why

```text
common/          shared by more than one process
  protocol/      packet format, channel layout, serial framing
  transport/     UDP, serial, impaired link
simulator/       fake ECU (lab only)
car_node/        runs on the car (Pi)
  can_rx/        receive-only CAN input: named can_rx, not can, so it can never shadow python-can's `import can`
  scheduler/
link_sim/        simulated radio (lab only)
pit_receiver/    runs at the pit
infrastructure/  VM creation, Docker Compose, Grafana, InfluxDB
```

Protocol and transport live in `common/` rather than under `car_node/`
because the pit receiver needs them too and must not depend on the car node
package.

## Failure handling summary

| Failure | Where it is detected | What the pit shows |
|---|---|---|
| Sensor fault (raw 0xFFFF) | car node decoder | channel NO DATA |
| CAN message stops | car node state (`stale_after_ms`) | channel STALE, CAN message age rises |
| CAN frames lost | car node rolling counters | CAN counter gaps rise |
| Radio packet loss | pit sequence numbers | loss %, missing packets, link WARNING or CRITICAL |
| Radio corruption | pit CRC | rejected bad_crc |
| Radio outage | pit last-packet age | link STALE, then CRITICAL (lost) |
| Radio too slow | queue growth at the radio | rising loss, stale channels |
| Car node restart | new session id | sessions count, sequence tracking resets |
| Car and pit configs differ | layout hash in STATUS | link WARNING, config mismatch |
| InfluxDB down | sink error | logged; reception continues |
