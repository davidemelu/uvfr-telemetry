# Telemetry protocol (version 1)

A compact binary protocol for a slow, lossy serial radio (915 MHz LoRa in the
first hardware MVP). It is designed to:

- keep packets small (22 to 36 bytes) so airtime stays low,
- never forward CAN frames one-for-one: the car node samples its vehicle state
  on a schedule (`config/channels.yaml`),
- let the pit measure the link itself: packets sent, received, missing,
  loss %, rate, staleness and uptime,
- reject corrupted or malformed packets with a CRC and strict length checks,
- distinguish "the car lost the CAN signal" from "the pit lost the radio".

Code: `common/protocol/` (`packet.py`, `channels.py`, `framing.py`). Encoding
and decoding are pure functions with no I/O, tested in `tests/test_protocol.py`
and `tests/test_framing.py`.

## Packet layout

All multi-byte fields are little-endian.

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | version / type | high nibble = protocol version (1), low nibble = packet type |
| 1 | 1 | session id | random 1 to 255, chosen when the car node starts; a change tells the pit the node restarted |
| 2 | 2 | sequence number | +1 for every packet of any type; wraps at 65536 |
| 4 | 4 | source timestamp | ms since the car node started; wraps after about 49 days |
| 8 | n | payload | depends on the type |
| 8+n | 2 | CRC | CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over bytes 0 to 8+n-1 |

Fixed overhead: 10 bytes per packet.

### Type 1: TELEMETRY

| Size | Field |
|---|---|
| ceil(N/8) | channel bitmap: bit i set means channel i is present (N = number of channels in `channels.yaml`; 13 channels = 2 bytes) |
| varies | values of the present channels, in index order, each in its wire type |

### Type 2: STATUS (sent at `status_rate_hz`, 1 Hz by default)

| Size | Field | Notes |
|---|---|---|
| 2 | layout hash | CRC-16 of the channel layout; the pit alarms if it does not match its own |
| 4 | tx packets | total packets sent by this session, including this one |
| 4 | CAN frames | total CAN frames received |
| 2 | CAN frames/s | over the last status interval |
| 2 | CAN counter gaps | rolling-counter discontinuities (lost CAN frames), saturating |
| 2 | CAN decode errors | saturating |
| 1 | CPU % | car node process CPU (255 = unknown) |
| M | CAN message ages | one byte per CAN message used by the channels, in first-use order; units of 20 ms; 254 = 5.08 s or more; 255 = never received |

With 7 source messages the STATUS packet is 34 bytes.

## Channel values

Each channel in `config/channels.yaml` has a wire type, a scale and an offset:
`physical = raw * scale + offset`.

| Wire type | Bytes | Valid raw range | NO DATA | STALE |
|---|---|---|---|---|
| u8 | 1 | 0 to 253 | 255 | 254 |
| i8 | 1 | -126 to 127 | -128 | -127 |
| u16 | 2 | 0 to 65533 | 65535 | 65534 |
| i16 | 2 | -32766 to 32767 | -32768 | -32767 |

- **NO DATA**: the car node has never received the signal, or the sensor
  reports a fault (the DBC "all bits set" pattern).
- **STALE**: the car node has the signal but it is older than the channel's
  `stale_after_ms` (default 5 x the CAN cycle time, at least 250 ms). This is
  how a CAN message timeout reaches the pit.
- Values outside the representable range saturate to the nearest valid raw
  value (never to a sentinel) and are counted.

When loading, every channel is checked against the DBC: the DBC's min and max
must fit the chosen wire type, scale and offset, or the layout is rejected.

### Layout hash

The hash covers channel order, names, source signals, wire types, scales and
offsets, which is everything a decoder depends on. It deliberately ignores
rates and units, so changing a rate does not break the pit. Rules:

- Add new channels at the end of the list.
- Car and pit must use the same `channels.yaml`; a mismatch is reported on
  every STATUS packet.

## Worked example

Tick 0 with the shipped layout carries 7 channels. A real 24-byte packet:

```text
11 5B 02 01 39 30 00 00 5F 04 28 23 73 B7 18 1B 10 02 E6 03 9B 0B E9 24
```

| Bytes | Meaning |
|---|---|
| `11` | version 1, type 1 (TELEMETRY) |
| `5B` | session 0x5B |
| `02 01` | sequence 0x0102 = 258 |
| `39 30 00 00` | timestamp 12345 ms |
| `5F 04` | bitmap 0x045F: channels 0, 1, 2, 3, 4, 6, 10 |
| `28 23` | rpm: 9000 |
| `73` | throttle_position: 115 x 0.5 = 57.5 % |
| `B7 18` | vehicle_speed: 6327 x 0.01 = 63.27 km/h |
| `1B 10` | oil_pressure: 4123 x 0.1 = 412.3 kPa |
| `02` | gear: 2 |
| `E6 03` | lambda: 998 x 0.001 = 0.998 |
| `9B 0B` | fuel_pressure: 2971 x 0.1 = 297.1 kPa |
| `E9 24` | CRC 0x24E9 |

## Scheduling and packet sizes

The car node ticks at `base_rate_hz` (10 Hz) and sends one TELEMETRY packet
per tick with whichever channels are due. Slower channels are phase-shifted
so they spread across ticks. With the shipped layout every tick carries the
four 10 Hz channels plus two or three slower ones:

| Tick | Size (bytes) | Tick | Size (bytes) |
|---|---|---|---|
| 0 | 24 | 5 | 25 |
| 1 | 25 | 6 | 22 |
| 2 | 24 | 7 | 23 |
| 3 | 25 | 8 | 24 |
| 4 | 24 | 9 | 25 |

Per second: 241 bytes of TELEMETRY plus 34 bytes of STATUS = **275 bytes/s
(2,200 bit/s)** over 11 packets. Of that, 121 bytes/s are channel values; the
rest is header, bitmap and CRC. Measured live on the lab VM, the car node
reports exactly these numbers. See `docs/bandwidth.md` for the comparison with
LoRa link budgets.

## What the pit derives

The protocol carries everything the pit receiver needs to compute these (the
receiver itself is described in `docs/architecture.md`):

| Metric | How |
|---|---|
| Packets transmitted | `tx packets` from the latest STATUS (survives sequence wraparound), cross-checked with the sequence span |
| Packets received | count of valid packets |
| Missing packets / sequence gaps | gaps in the sequence number, unwrapped to 64 bits; late (reordered) packets fill their gap |
| Packet loss % | missing / expected, both cumulative and over a rolling window |
| Packet rate | received packets per second over a rolling window |
| Stale telemetry | time since the last valid packet (link side), and STALE values (CAN side) |
| Telemetry uptime | time since the first packet of the session, and the fraction of seconds with at least one packet |
| Restarts | a new session id resets sequence tracking |

## Framing on serial links

UDP preserves packet boundaries, a serial port does not. On serial links each
packet is COBS-encoded (Consistent Overhead Byte Stuffing removes every 0x00
byte) and followed by a single 0x00 delimiter: 2 extra bytes per packet. After
line noise the receiver discards bytes up to the next 0x00 and resynchronises;
anything damaged is then rejected by the CRC.

## Rejected packets

The decoder raises `DecodeError` with a stable `reason`, which the pit counts:

| Reason | Cause |
|---|---|
| `too_short` | fewer than 10 bytes |
| `bad_crc` | CRC mismatch (corruption on the link) |
| `bad_version` | protocol version is not 1 |
| `unknown_type` | packet type is not TELEMETRY or STATUS |
| `bad_length` | payload length does not match the bitmap or the STATUS layout |
| `bad_bitmap` | bitmap names channels this layout does not have |

Tests flip every single bit of a full packet and confirm each one is caught,
and feed thousands of random blobs to the decoder.

## Versioning

A breaking change to the header or a payload layout bumps the version nibble.
Receivers reject versions they do not know rather than guessing. Adding
channels is not a protocol change; it changes the layout hash, which both
ends check.
