# Simulation

Phase 0 replaces the car with a fake ECU that writes SIMULATED CAN traffic to
`vcan0`, a software-only CAN bus inside the Linux kernel. Everything
downstream (car node, radio link, pit receiver, database, dashboard) reads
that bus exactly as it will later read the real one.

```text
simulator/
  fake_ecu.py         real-time loop: step the simulation, send frames, answer control requests
  simulation.py       deterministic core: model + driver + scenarios + CAN schedule (no I/O)
  vehicle_model.py    longitudinal, thermal, oil, fuel and electrical model
  driver.py           simulated driver lapping a configurable track
  scenarios/          fault scenarios
  ctl.py              switch scenarios on a running fake ECU
config/simulation.yaml  every model, driver and scenario parameter
dbc/simulated_uvfr.dbc  SIMULATED CAN database
```

## Running it

```bash
make vcan                                          # once per boot (the lab VM does this automatically)
python simulator/fake_ecu.py --scenario normal     # or: make sim
python simulator/fake_ecu.py --scenario overheating
python simulator/fake_ecu.py --scenario low_oil_pressure,intermittent_can
python -m simulator --list-scenarios
```

Useful options: `--seed N` for a repeatable run, `--duration S` to stop
automatically, `--quiet` to suppress the once-per-second status line,
`--channel vcan1` for a second virtual bus.

Watch the traffic:

```bash
make candump            # raw frames
make candump-decoded    # decoded with the DBC, e.g.
#  vcan0 100 [8] C6 30 44 02 03 00 00 B0 :: SIM_ENGINE_FAST(EngineSpeed: 12486 rpm, ThrottlePosition: 58.0 %, Gear: Third, ...)
```

### Switching scenarios while it runs

```bash
python -m simulator.ctl set overheating            # or: make scenario SCENARIO=overheating
python -m simulator.ctl add intermittent_can
python -m simulator.ctl remove overheating
python -m simulator.ctl normal
python -m simulator.ctl status
```

The control channel is JSON over UDP on `127.0.0.1:47020` (configurable).
Physical faults ramp in and out, so switching mid-session looks continuous.

### Safety guard

The fake ECU transmits, so it refuses to run on anything except a virtual bus
(`vcan*`, `udp_multicast` or `virtual`). Pointing it at `can0` exits with an
error. The car node, by contrast, never transmits at all.

## The simulated CAN database

Every ID below is **SIMULATED**. None of them are UVFR's real CAN IDs. All
signals are little-endian, unsigned, in 8-byte frames, and every message ends
with a 4-bit rolling counter so a receiver can detect lost frames.

| ID | Message | Rate | Signals (unit, scaling) |
|---|---|---|---|
| 0x100 | SIM_ENGINE_FAST | 100 Hz | EngineSpeed (rpm, 1), ThrottlePosition (%, 0.1), Gear (0 = neutral) |
| 0x101 | SIM_ENGINE_PRESSURES | 50 Hz | OilPressure (kPa, 0.1), FuelPressure (kPa, 0.1), Lambda (0.001) |
| 0x102 | SIM_ENGINE_TEMPS | 10 Hz | CoolantTemp, EngineTemp, IntakeAirTemp (degC, 0.1, offset -40) |
| 0x103 | SIM_ELECTRICAL | 10 Hz | BatteryVoltage (V, 0.01) |
| 0x110 | SIM_VEHICLE | 50 Hz | VehicleSpeed (km/h, 0.01) from the undriven wheels |
| 0x120 | SIM_GPS | 10 Hz | GpsSpeed (km/h, 0.01), GpsFix, GpsSatellites |
| 0x7F0 | SIM_STATUS | 1 Hz | SimScenarioMask, SimElapsed. Lab harness only, never on a car |

Total: about 231 frames/s.

**Fault convention:** a raw value with every bit set (0xFFFF for a 16-bit
signal) means "sensor fault / not available". It decodes outside the signal's
min/max range, so decoders treat any out-of-range value as invalid.

## How the model behaves

The model is deliberately simple but causal, so signals move together the way
they do on a car:

- **Driver and track.** The car idles in neutral in the pits for 8 s (with
  warm-up throttle blips), then laps a 1 km track of straights and corners.
  The driver accelerates towards each segment's target speed and brakes early
  enough to reach the next one. A lap takes about 58 s at an average of
  60 km/h.
- **Powertrain.** Throttle sets engine torque (600 cc, about 55 Nm peak),
  which goes through a six-speed gearbox to the wheels, limited by tyre grip.
  RPM comes from road speed and gear once the clutch is locked, and from a
  slipping clutch when pulling away. Upshifts at 12,800 rpm with a 60 ms
  ignition cut; limiter at 13,500 rpm.
- **Cooling.** Engine power heats the coolant. A thermostat (80 to 90 degC),
  a radiator whose effectiveness rises with road speed, and a fan (on at
  96 degC) remove it. Coolant warms from 75 degC and settles around
  85 to 90 degC. Cylinder head and oil temperatures follow coolant plus load.
- **Oil pressure** rises with RPM up to a relief valve (520 kPa), and is
  higher when the oil is cold.
- **Fuel and lambda.** Regulated fuel pressure dips slightly under high
  flow. Lambda reads about 0.87 at full throttle, about 1.0 at part throttle,
  and spikes lean during deceleration fuel cut.
- **Electrical.** The alternator holds about 13.9 V. The fan and the shift
  actuator cause small dips under load.
- **Sensors** add realistic noise. GPS speed lags wheel speed by 150 ms.

## Failure scenarios

| Scenario | Bit | What happens | What the pit should see |
|---|---|---|---|
| `normal` | none | No faults | All NORMAL |
| `overheating` | 0 | Fan failure and partial coolant loss (cooling at 15%, coolant mass at 60%) | Coolant rises about 0.5 degC/s from about 89 degC: 105 degC after about 33 s, 110 degC after about 41 s |
| `low_oil_pressure` | 1 | Worn pump (30% output) plus oil surge under braking | Oil pressure 50 to 160 kPa above 6,000 rpm (healthy: 350 to 520 kPa) |
| `battery_voltage_sag` | 2 | Alternator stops charging | Voltage falls from 13.9 V to below 12 V after about 29 s and below 11.5 V after about 50 s |
| `sensor_dropout` | 3 | Oil pressure sensor reports "not available" in random 0.5 to 2.5 s bursts | Channel flips to NO DATA while frames keep arriving |
| `frozen_sensor` | 4 | Coolant sensor sticks at its last value | Coolant flat-lines while everything else moves |
| `can_message_timeout` | 5 | The ECU stops sending SIM_ENGINE_TEMPS | Coolant, head and intake temps go STALE |
| `intermittent_can` | 6 | Loose connector: 15% random frame loss plus 0.3 to 1.5 s total outages | Rolling-counter gaps, intermittent staleness |
| `engine_overrev` | 7 | Late upshifts (14,300 rpm, limiter mis-set to 14,800) and occasional mis-shifts | RPM above 13,500 on every straight, spikes over 14,000 |

Scenarios combine (`overheating,intermittent_can`). The active set is
broadcast in `SIM_STATUS.SimScenarioMask` so the dashboard can show what is
being simulated. All parameters are in `config/simulation.yaml` under
`scenarios:`.

## Configuration

`config/simulation.yaml` holds every parameter. Loading is strict: unknown
keys (usually typos), missing keys and inconsistent values (for example an
upshift point above the rev limiter) are rejected with a clear error instead
of silently changing behaviour.

## Determinism and tests

`simulation.py` has no I/O and no wall-clock dependency. With a fixed seed
the same frames come out every time, and tests run minutes of simulated
driving in about a second (the model runs about 200 times faster than real
time). See `tests/test_vehicle_model.py`, `tests/test_scenarios.py` and
`tests/test_dbc.py`.

## Recording and replay

```bash
make record DURATION=60                     # vcan0 -> recordings/vcan0-<timestamp>.log
python -m replay.record --channel can0 --out recordings/endurance.log   # later, on the car (listen-only)

make replay LOG=recordings/x.log SPEED=2 LOOP=1
python -m replay recordings/x.log --speed 0.5 --paused
python -m replay.ctl pause | resume | speed 5 | loop on | status | stop
```

- Logs are SocketCAN candump format (`candump -l`), so can-utils
  (`canplayer`, `log2asc`) read them too. The player also reads Vector
  `.asc`/`.blf`, PCAN `.trc` and CSV through python-can.
- Playback keeps the original inter-frame timing scaled by the speed (any
  value from 0.1x to 50x, e.g. 0.5x, 1x, 2x, 5x), loops, and pauses and
  resumes without a burst. In a terminal: space pauses, `+`/`-` change speed,
  `l` toggles looping, `q` quits. If the host stalls, playback jumps forward
  instead of flooding the bus.
- Frames recorded on `can0` are played onto `vcan0`: the recorded channel
  name is ignored, so the bus you choose is the only one that receives them.
- Recording is receive-only. Replay, like the fake ECU, refuses to transmit
  on anything but a virtual bus.
- Stop the fake ECU before replaying onto the same bus, or the two streams mix.

## Replacing the fake ECU with real data

The rest of the pipeline only sees CAN frames on a bus, so the fake ECU can
be swapped for the replay tool (recorded traffic) or, later, the real car.
This is tested: `tests/test_replay.py` replays a recorded drive at 2x into
the unchanged car node and checks the telemetry that comes out.

When real UVFR data arrives, use the real DBC and map telemetry channels to
its signal names in `config/channels.yaml`; nothing in the car node or pit
receiver depends on the simulated IDs.
