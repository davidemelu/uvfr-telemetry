# Troubleshooting

Problems met while building Phase 0, and their fixes.

## CAN and vcan

**`Cannot find device "vcan0"` / `ip link add ... type vcan` fails**
The running kernel has no vcan module. Debian *cloud* images ship
`linux-image-cloud-amd64`, which has no CAN drivers. Run
`sudo ./scripts/provision-vm.sh`, reboot when it exits with code 100, and run
it again. Check with `uname -r` (must not end in `-cloud-amd64`) and
`modinfo vcan`.

**vcan0 missing after a reboot**
`systemctl status uvfr-vcan0` (installed by `provision-vm.sh`). On another
host: `./scripts/setup-vcan.sh`.

**The fake ECU or replay says `refusing to transmit on socketcan:can0`**
Intended. Simulated and replayed traffic may only go to `vcan*`,
`udp_multicast` or `virtual`.

**The car node says `REFUSING TO START ... not in listen-only mode`**
Intended on real hardware. Put the interface in listen-only mode (see
`docs/hardware-transition.md`).

**Frame counts are doubled, or replay looks wrong**
Two sources are writing to the same bus (for example the fake ECU and a
replay). Stop one: `make demo-stop`, or kill the fake ECU, before
`make replay`. Tests use `vcan1` so they can run next to the demo on `vcan0`.

## The demo

**`make demo` says `no telemetry at the pit after 20 s`**
Look at `logs/*.log`. Common causes: vcan missing (above), a port already in
use by a process started by hand (`ss -ulpn | grep 470`), or the virtualenv
missing (`make venv`).

**`cannot bind control port 127.0.0.1:47020` (or 47010, 47030)**
Another fake ECU, link_sim or replay is already running. `make demo-stop`,
then check `pgrep -af "python -m"`.

**Do not stop processes with `pkill -f "python -m pit_receiver"` inside a
compound shell command**: the pattern also matches the shell's own command
line, so the shell gets killed. Use `make demo-stop` (PID files) or anchor the
pattern: `pkill -f '^\.venv/bin/python -m pit_receiver'`.

**Processes started over SSH keep the session open or die when it closes**
Start them as `setsid nohup ... > log 2>&1 < /dev/null &`, which is what
`scripts/start-demo.sh` does.

## InfluxDB and Grafana

**Grafana is not reachable right after `make infra-up` on a fresh install**
The first start of Grafana 13 runs several hundred database migrations (about
a minute). The compose health check waits for `/api/health`; `make infra-up`
returns once it is healthy.

**The dashboard is missing in Grafana (404 for uid `uvfr-pit`) although the JSON exists**
Grafana bind-mounts `infrastructure/grafana/dashboards/`. If that directory is
deleted and recreated while Grafana runs (for example by a sync), Grafana
keeps the old, empty directory. Recreate the container:
`docker compose --env-file .env -f infrastructure/docker-compose.yml up -d --force-recreate grafana`.

**A panel shows "No data"**
- The pit receiver is not running or not writing: `make demo-status`, and
  the `pit` measurement's `influx_*` fields on the dashboard.
- Gauges only show values from the last 10 seconds by design; stale data
  shows "NO DATA" rather than an old number.
- Check the query through Grafana's API (see `scripts/demo_overheating.py`
  for an example) to see the actual Flux error.

**Dashboard changes in `config/*.yaml` do not appear**
Run `make dashboard` and commit the JSON. A test (and CI) fails if the
committed dashboard is out of date with the config.

**`pit_receiver: influxdb token is empty (check .env)`**
`make infra-up` creates `PIT_INFLUX_TOKEN` and `GRAFANA_INFLUX_TOKEN`.
If `.env` was recreated, run `./infrastructure/influxdb/create-tokens.sh`.

**InfluxDB was down: is data lost?**
No, up to about two hours: the writer buffers and retries. The `pit`
measurement shows `influx_buffered`, `influx_dropped` and
`influx_write_errors`.

**Grafana is slow or the VM is busy**
One browser at 1 s refresh costs about one VM core. Use the default 2 s
refresh, or close extra viewers.

## Alarms

**An alarm flaps between states**
Increase `hysteresis` or `hold_s` for that rule in `config/alerts.yaml`.
Rate-of-rise rules need a window long enough to average out lap-to-lap load
changes (15 s for coolant).

**"Packet loss high" right after the pit receiver starts**
Loss is only judged once the rolling window has enough packets (half of
`loss_window_packets`). If it still appears, the link really is losing
packets: check `make link` / the radio.

**"Car and pit channel layouts differ"**
The car node and pit receiver are using different `config/channels.yaml`
files (the STATUS packet's layout hash does not match). Deploy the same file
to both.

## Git on Windows (PowerShell 5.1)

**`git commit -m @'...'@` fails with `pathspec ... did not match any file(s)`**
PowerShell 5.1 does not escape double quotes inside a here-string passed to a
native program, so git sees the message split into several arguments. Avoid
`"` in commit messages, or write the message to a file and use
`git commit -F message.txt`.

**`make: warning: Clock skew detected`**
Harmless: the Windows machine's clock is slightly ahead of the VM's, so freshly
deployed files look like they are from the future.
