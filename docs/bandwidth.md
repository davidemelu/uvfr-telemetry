# Bandwidth

**Question:** can roughly 10 to 15 telemetry channels at about 10 Hz fit
through a low-bandwidth 915 MHz LoRa link?

**Short answer:** yes, at spreading factor 7 with 250 kHz or 500 kHz
bandwidth (18% to 36% airtime for the shipped 13-channel stream). At
SF7 / 125 kHz it is tight (about 70% airtime) unless several samples are
batched into each radio packet. SF8 and slower at 125 kHz cannot carry it.
The limit is the **number of packets per second**, not the number of bytes:
every LoRa packet pays a fixed preamble and header before any data.

These are calculated and measured numbers for the generated stream. They are
not a range test: the real modems, antennas and track still need to be
measured (see `docs/hardware-transition.md`).

## What the stream needs (measured)

Shipped layout (`config/channels.yaml`): 12 vehicle channels plus one
lab-only channel. Measured on the lab VM over 16 one-second windows; the
measured car-node transmit rate matched the prediction exactly.

| Channel | Rate Hz | Wire type | Bytes | Payload bit/s |
|---|---|---|---|---|
| rpm | 10 | u16 | 2 | 160 |
| throttle_position | 10 | u8 | 1 | 80 |
| vehicle_speed | 10 | u16 | 2 | 160 |
| oil_pressure | 10 | u16 | 2 | 160 |
| gear | 5 | u8 | 1 | 40 |
| coolant_temperature | 5 | i16 | 2 | 80 |
| lambda | 5 | u16 | 2 | 80 |
| gps_speed | 5 | u16 | 2 | 80 |
| battery_voltage | 2 | u16 | 2 | 32 |
| engine_temperature | 2 | i16 | 2 | 32 |
| fuel_pressure | 2 | u16 | 2 | 32 |
| intake_air_temperature | 1 | i16 | 2 | 16 |
| sim_scenario (lab only) | 1 | u16 | 2 | 16 |
| **Total** | | | | **968** |

| Quantity | Value |
|---|---|
| Telemetry packets | 10 per second, 22 to 25 bytes (mean 24.1) |
| STATUS packets | 1 per second, 34 bytes |
| Fixed overhead per packet | 10 bytes (header + CRC) + 2 bytes channel bitmap |
| Raw channel payload | 121 B/s = **968 bit/s** |
| Encoded protocol (UDP) | 275 B/s = **2,200 bit/s** |
| Encoded + COBS serial framing | 297 B/s = **2,376 bit/s** |
| Protocol overhead | 56% of encoded bytes |

The overhead is large because the packets are small and frequent; that is
the price of 10 Hz updates with sequence numbers, timestamps and a CRC on
every packet. The same numbers are live on the dashboard ("Telemetry
bandwidth" row), measured from what actually reaches the pit.

## Why a LoRa headline data rate is not all usable

LoRa's quoted rate (SF x BW / 2^SF x 4/5) ignores the preamble (8 + 4.25
symbols) and the header that every packet pays. Time on air, from Semtech's
formula (implemented in `bandwidth/lora.py`, checked against Semtech's
reference values in the tests):

| Packet | SF7 / 125 kHz airtime | Effective rate |
|---|---|---|
| 10 bytes | 41.2 ms | 1.9 kbit/s |
| 27 bytes (one telemetry packet + framing) | 66.8 ms | 3.2 kbit/s |
| headline | | 5.47 kbit/s |

Our 2.4 kbit/s stream is only 44% of the SF7 / 125 kHz headline rate, but at
10 packets per second it occupies about 70% of the airtime.

## LoRa airtime for this stream

CR 4/5, 8-symbol preamble, explicit header, payload CRC on, COBS framing
included, no extra modem header (`--modem-overhead` adds one).

| LoRa setting | Headline kbit/s | Largest packet | Airtime used | Verdict | Max u16 channels at 10 Hz within 50% airtime |
|---|---|---|---|---|---|
| SF7 / 500 kHz | 21.88 | 19.3 ms | 18% | fits with margin | 47 |
| SF8 / 500 kHz | 12.50 | 36.0 ms | 33% | fits with margin | 19 |
| SF7 / 250 kHz | 10.94 | 38.5 ms | 36% | fits with margin | 14 |
| SF8 / 250 kHz | 6.25 | 71.9 ms | 66% | tight | 0 |
| SF9 / 250 kHz | 3.52 | 133.6 ms | 120% | does not fit | 0 |
| SF7 / 125 kHz | 5.47 | 77.1 ms | 71% | tight | 0 |
| SF8 / 125 kHz | 3.12 | 143.9 ms | 132% | does not fit | 0 |
| SF9 / 125 kHz | 1.76 | 267.3 ms | 241% | does not fit | 0 |
| SF10 / 125 kHz | 0.98 | 493.6 ms (over a 400 ms dwell limit) | 457% | does not fit | 0 |
| SF11 / 125 kHz | 0.54 | 987.1 ms | 922% | does not fit | 0 |
| SF12 / 125 kHz | 0.29 | 1974.3 ms | 1811% | does not fit | 0 |

Verdicts: up to 50% "fits with margin", up to 80% "tight", up to 100%
"saturated", above that "does not fit". The 50% budget leaves room for the
modem's own framing, interference and the link being half-duplex. "Max
channels ... 0" at 125 kHz means that even one channel at 10 packets per
second already exceeds half the airtime there.

### Uniform streams: N channels, all at 10 Hz, 2 bytes each

| Channels | Packet bytes | Encoded bit/s | SF7 / 500 kHz | SF7 / 250 kHz | SF7 / 125 kHz | SF8 / 125 kHz | SF9 / 125 kHz |
|---|---|---|---|---|---|---|---|
| 10 | 32 | 2,832 | 21% | 42% | 85% | 148% | 274% |
| 12 | 36 | 3,152 | 22% | 45% | 90% | 158% | 294% |
| 15 | 42 | 3,632 | 25% | 50% | 100% | 179% | 314% |

## Options if the link has to be SF7 / 125 kHz (or slower)

**Batch several ticks per radio packet.** Not implemented yet; it would be
a protocol v2 option (a 1-byte tick offset and a bitmap per sample). Using
the shipped stream:

| Ticks per packet | Packets/s | Packet bytes | SF7 / 125 kHz | SF7 / 250 kHz | SF8 / 125 kHz | SF9 / 125 kHz | Added latency |
|---|---|---|---|---|---|---|---|
| 1 (today) | 10 | 26 | 69% | 35% | 128% | 233% | none |
| 2 | 5 | 42 | 51% | 26% | 91% | 171% | up to 100 ms |
| 5 | 2 | 87 | 38% | 19% | 70% | 125% | up to 400 ms |
| 10 | 1 | 163 | 34% | 17% | 62% | 111% | up to 900 ms |

Batching two ticks makes SF7 / 125 kHz workable with only 100 ms extra
latency, which is invisible on a pit dashboard. The values are still
sampled at 10 Hz.

Other levers, all configuration-only today:

- Fewer channels at 10 Hz. Most temperatures and pressures are fine at 2 to 5 Hz.
- Narrower wire types (u8 where the resolution allows).
- STATUS at 0.5 Hz instead of 1 Hz.

## What these numbers do not include

- **Modem overhead.** Transparent-serial LoRa modems may add their own
  address or header bytes and split or merge serial data into air packets.
  Measure the real modem and re-run with `--modem-overhead N`.
- **Regulations.** In the 902 to 928 MHz band, narrow-bandwidth LoRa usually
  operates as a frequency-hopping system with a per-transmission dwell limit
  (commonly 400 ms), while 500 kHz modes are treated differently. Check the
  specific modem's FCC / ISED (RSS-247) certification and configuration.
  None of the SF7 to SF9 packets here come close to 400 ms.
- **Range trade-off.** Each doubling of bandwidth costs about 3 dB of link
  budget, roughly 30% less free-space range; each step down in SF gains
  about 2.5 dB. Faster settings must be range-tested on the actual track.
- **Retransmissions.** The protocol never retransmits: a lost packet is
  simply superseded 100 ms later, so loss costs freshness, not airtime.

## RFD900x (future upgrade)

The RFD900x is a frequency-hopping 900 MHz modem with configurable air rates
from a few kbit/s up to about 250 kbit/s (64 kbit/s by default in its SiK
firmware). At the default rate this 2.4 kbit/s stream needs only a few
percent of the raw rate, leaving room for more channels or higher update
rates; its own framing and hopping overhead should still be measured. It uses
the same serial transport (`common/transport/serial_transport.py`).

## Reproducing these numbers

```bash
make bandwidth                       # report + 10 s measurement of the running car node
python -m bandwidth --markdown       # the tables in this document
python -m bandwidth --modem-overhead 4
```

The simulated radio (`link_sim`) applies a comparable airtime model live:
`make link LINK=lora_good` shows about 62% radio load for this stream, and
`LINK=congested` (an SF9-like rate) shows what happens when the link cannot
keep up: the queue fills, packets are dropped and channels go stale.
