# Hardware transition

How the Phase 0 simulation becomes the physical MVP. Nothing in this document
is implemented or tested on hardware yet; it is the plan, written against
the code as it exists, so the changes are configuration and bring-up rather
than redesign.

## From simulation to car

```text
Phase 0 (today)                              Physical MVP

fake ECU                                     Vehicle CAN (ECU, dash, sensors)
   |                                            |
vcan0                                        can0 on the CAN HAT, LISTEN-ONLY
   |                                            |
car node (lab VM)                            car node (Raspberry Pi 3), same code
   | UDP                                        | serial (USB 915 MHz LoRa modem)
link_sim (simulated radio)                     ~~~~~~~~~~ wireless ~~~~~~~~~~
   | UDP                                        | serial (USB 915 MHz LoRa modem)
pit receiver (lab VM)                        pit receiver (pit laptop), same code
   |                                            |
InfluxDB + Grafana (lab VM)                  InfluxDB + Grafana (laptop or homelab)
```

RFD900x modems are the planned higher-bandwidth upgrade later. They also
connect over USB serial and use the same transport.

## Safety first: the telemetry node is a passive tap

```text
ECU ───────── CAN BUS ───────── Dash
                   │
                   └── telemetry listener (Raspberry Pi + CAN HAT, listen-only)
```

- The node **taps** the existing bus. It must never sit between the ECU and
  the dash, or between any sensor and its controller. Removing it, or it
  failing, must not change anything else on the car.
- The CAN controller runs in **listen-only (silent) mode**: it does not
  transmit and does not even acknowledge frames. A wrong bitrate then cannot
  disturb the bus with error frames.
- Software enforces the same thing twice. The car node's bus handle has no
  `send()`, and the node refuses to start on a real CAN interface that is not
  in listen-only mode (`car_node.can.require_listen_only: true`). The fake ECU
  and the replay tool refuse to transmit on anything but a virtual bus.
- **Termination:** the bus is already terminated by its two end nodes
  (120 ohm each). Most CAN HATs have a termination resistor jumper; it must be
  **off** for a mid-bus tap. Measure about 60 ohm between CAN-H and CAN-L with
  the car powered down, before and after connecting the HAT.
- Keep the stub from the bus to the HAT short (tens of centimetres), fuse the
  Pi's power feed from the low-voltage system, and agree the connection point
  and harness change with the team's electrical lead and the car's rules
  compliance owner.

## Step 1: Raspberry Pi 3 + CAN HAT

1. Raspberry Pi OS Lite. The code runs on Python 3.11 (bookworm) and 3.13
   (trixie); CI tests both.
2. Enable SPI and the HAT's overlay in `/boot/firmware/config.txt`. For the
   common MCP2515 HATs:

   ```ini
   dtparam=spi=on
   dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
   ```

   The oscillator frequency (8, 12 or 16 MHz) and interrupt GPIO depend on
   the exact HAT. Read them off the board or its documentation; a wrong
   oscillator gives a wrong bitrate.
3. Bring `can0` up **listen-only** at the car's bitrate (500 kbit/s is common
   but must be confirmed from the ECU configuration):

   ```bash
   sudo ip link set can0 down
   sudo ip link set can0 type can bitrate 500000 listen-only on
   sudo ip link set can0 up
   ip -details link show can0        # must show <LISTEN-ONLY>
   candump -t a can0                 # frames should appear; nothing is sent
   ```

   Make it permanent with a systemd unit like the lab's `uvfr-vcan0.service`
   (`scripts/provision-vm.sh`), running the three `ip link` commands.
4. Copy the repository (or `git clone`), then `make venv`.
5. Record a first session before changing anything else:

   ```bash
   python -m replay.record --channel can0 --duration 600 --out recordings/first-car-session.log
   ```

   The recorder is receive-only and also checks listen-only mode. This log
   becomes the test data for everything that follows: replay it in the lab
   (`make replay`) instead of waiting for car time.

## Step 2: real DBC and channels

The car node and pit receiver never hard-code CAN IDs; everything comes from
the DBC and `config/channels.yaml`.

| File | Change |
|---|---|
| `dbc/` | Add the real UVFR DBC (keep `simulated_uvfr.dbc` for the lab). |
| `config/telemetry.yaml` | `car_node.dbc` and `pit_receiver.dbc` point at the real DBC; `car_node.can.channel: can0`; `pit_receiver.car_id` for the real car. |
| `config/channels.yaml` | Map each channel to the real `MESSAGE.Signal`. Loading checks every name against the DBC and checks the wire encoding can carry the DBC range. Drop `sim_scenario`. |
| `config/alerts.yaml` | Replace the generic thresholds with the team's real engine limits. |
| `config/dashboard.yaml` | Display ranges and labels; then `make dashboard`. |

Two assumptions from the simulated DBC to check against the real one:

- **Fault values.** The pit treats out-of-range values as "sensor fault / no
  data". If the real ECU signals faults differently (a status bit, a
  specific value), add that to `common/dbc.py`'s decoding.
- **Rolling counters.** CAN-loss detection uses signals ending in `_Counter`.
  If the real messages have no counters, gaps are not counted; message-age
  staleness still works.

Validate in the lab first: replay the recorded car log through the real DBC
with `make replay`, `make car-node` and `make pit`, and compare the dashboard
with what the dash showed during the session.

## Step 3: USB LoRa modems

1. Configure both modems identically: frequency, spreading factor,
   bandwidth, coding rate and network or sync settings. Start from
   `docs/bandwidth.md`: **SF7 at 250 kHz** carries the shipped stream at about
   36% airtime; SF7 / 125 kHz is tight (about 70%) unless samples are batched.
   Check that the setting is permitted for the modem's FCC / ISED
   certification (dwell-time limits apply to narrow-bandwidth modes).
2. Use the modems in transparent serial mode. Disable any modem-level
   acknowledgements or retries if possible: a late retransmission delays
   fresh data, and the protocol already tolerates loss.
3. Give each modem a stable device name, e.g.
   `/dev/serial/by-id/usb-...` instead of `/dev/ttyUSB0`.
4. Switch the transports:

   ```yaml
   # telemetry.yaml on the Pi
   car_node:
     transport:
       type: serial
       port: /dev/serial/by-id/usb-<car modem>
       baudrate: 57600

   # telemetry.yaml on the pit laptop
   pit_receiver:
     transport:
       type: serial
       port: /dev/serial/by-id/usb-<pit modem>
       baudrate: 57600
   ```

   Packets are COBS-framed on serial links (`common/protocol/framing.py`),
   adding 2 bytes each, so the receiver finds packet boundaries even when the
   modem splits or merges data. The serial transport is tested against a
   loopback but **has not run on real modems yet**.
5. `link_sim` is no longer used: the real radio sits where it was.

Bench test before the car:

1. Both modems on a desk; the Pi runs the car node against a replayed log on
   `vcan0`; the laptop runs the pit receiver.
2. Check the dashboard's link panels: loss near 0%, about 11 packets/s,
   bandwidth matching `docs/bandwidth.md` plus the modem's own overhead (if the
   numbers differ, re-run `python -m bandwidth --modem-overhead N`).
3. Range test: walk the laptop around the track or car park and watch packet
   loss, availability and "latest packet age". The same pit statistics and
   alarms are used as in the simulation.

## Step 4: pit laptop and homelab

Two workable options:

- **Self-contained at the track (recommended first):** run the same Docker
  Compose stack on the pit laptop (`make infra-up`), with the pit receiver
  writing to it locally. No internet dependency at the track.
- **Straight to the homelab:** the pit receiver writes to the homelab
  InfluxDB over Tailscale. Set `INFLUX_URL` on the laptop, set `INFLUX_BIND`
  on the VM to its LAN or Tailscale address, and give the laptop only
  `PIT_INFLUX_TOKEN` (write-only). If the connection drops, the InfluxDB
  writer buffers about two hours of telemetry and writes it on reconnection.

Timestamps need no clock synchronisation: the pit timestamps each sample
from the car's own monotonic clock plus the measured offset
(`pit_receiver/clock.py`), so the Pi needs neither a real-time clock nor NTP
at the track.

## Raspberry Pi 3 performance

On the lab VM the car node uses about 2% of one core for 231 CAN frames/s.
A Pi 3 core is several times slower, so expect around 10 to 20%, which is
fine. Kernel CAN filters already drop every ID the channels do not use. If
the real bus is much busier, measure first (`top`, and the car node's own
`cpu_pct`, sent in every STATUS packet and stored in InfluxDB's
`car_status` measurement).

## What stays the same

Everything else: the protocol, scheduler, sequence and loss tracking,
staleness, alarms, InfluxDB schema, dashboard, bandwidth measurement and
recording and replay. The tests run against the same code that will run on
the Pi.

## RFD900x upgrade (later)

The RFD900x is a frequency-hopping 900 MHz modem with much higher air rates
(64 kbit/s by default). It connects the same way (USB serial, transparent
mode), so the change is the serial port and baud rate in `telemetry.yaml`.
The extra capacity allows more channels or higher rates: change
`channels.yaml`, regenerate the dashboard, and re-check
`python -m bandwidth`.

## Checklist before the first car run

- [ ] Termination jumper on the HAT is off; 60 ohm measured across CAN-H and CAN-L.
- [ ] `ip -details link show can0` shows LISTEN-ONLY at the correct bitrate.
- [ ] `candump can0` shows traffic; the car node starts without the listen-only refusal.
- [ ] Real DBC loaded; `channels.yaml` validated (the car node starts and reports the layout hash).
- [ ] Pit and car use the same `channels.yaml` (no layout-mismatch alarm).
- [ ] Alarm thresholds reviewed by the engine lead.
- [ ] Bench radio test: loss under 2%, packet rate about 11/s.
- [ ] Power, fusing and harness reviewed by the electrical lead.
