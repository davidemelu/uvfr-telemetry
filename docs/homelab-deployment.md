# Homelab deployment

How the Phase 0 telemetry lab runs on the team's Proxmox homelab, and how to
rebuild it. If you do not have homelab access, see "Running without the
homelab" at the end.

## Isolation rules

The homelab already runs other production workloads. The telemetry lab must
never modify, stop, resize, migrate or reconfigure any existing VM, container,
storage pool, backup job or service. Everything for this project lives inside
one dedicated VM, and the only host-level artefacts it owns are:

| Host artefact | Purpose |
|---|---|
| VM `300` (`uvfr-telemetry-lab`) and its disks on `local-lvm` | The lab itself |
| `/var/lib/vz/snippets/uvfr-telemetry-vendor.yaml` | cloud-init vendor data (installs the QEMU guest agent) |

The VM is not part of any backup job and does not start on host boot
(`onboot: 0`). Everything needed to rebuild it is in this repository, so it can
be destroyed and recreated at any time.

## The VM

| Setting | Value | Reason |
|---|---|---|
| VMID / name | `300` / `uvfr-telemetry-lab` | Separate from the existing ID ranges; tagged `uvfr;telemetry;lab` |
| CPU | 2 vCPU, `cpu: host` | Light workload (about 230 CAN frames/s, a few Python processes, InfluxDB, Grafana). A second core keeps database writes and dashboard queries from disturbing simulator timing |
| RAM | 3 GiB, balloon floor 2 GiB | Expected use is about 1 to 1.5 GiB; the host can reclaim the rest under pressure |
| Disk | 32 GB, thin-provisioned on `local-lvm`, discard on | Actual use is a few GB (OS, Docker images, venv, recordings) |
| OS | Debian 13 (trixie) cloud image, switched to the standard kernel | See "Why the kernel is replaced" below. Debian 13 also matches the Raspberry Pi OS base used on the car later |
| Network | virtio NIC on `vmbr0`, DHCP | Currently `192.168.1.54`. Add a DHCP reservation on the router so the address does not drift |
| Login | user `david`, SSH key only (no password login) | cloud-init default |

### Why the kernel is replaced

Debian cloud images ship `linux-image-cloud-amd64`, a trimmed kernel without
the SocketCAN drivers (`can`, `can_raw`, `vcan`). `vcan0` cannot exist on it.
`scripts/provision-vm.sh` installs the standard `linux-image-amd64` kernel,
makes it the GRUB default, and after a reboot removes the cloud kernel.

## Rebuilding from scratch

1. On the Proxmox host, as root, with your SSH public key copied to the host:

   ```bash
   ./infrastructure/proxmox/create-vm.sh --ssh-key /root/your_key.pub
   ```

   It refuses to run if the VMID already exists. Options: `--vmid`, `--name`,
   `--cores`, `--memory`, `--balloon`, `--disk-gb`, `--storage`, `--bridge`,
   `--user`, `--image`.

2. From your workstation (Git Bash on Windows works), add an SSH alias:

   ```sshconfig
   Host uvfr-lab
     HostName 192.168.1.54
     User david
     IdentityFile ~/.ssh/id_ed25519_homelab
     IdentitiesOnly yes
   ```

3. Create the repo directory, copy the working tree over and provision:

   ```bash
   ssh uvfr-lab 'sudo install -d -o david -g david /opt/uvfr-telemetry'
   scripts/deploy.sh uvfr-lab
   ssh uvfr-lab 'cd /opt/uvfr-telemetry && sudo ./scripts/provision-vm.sh'
   # exit code 100 means "reboot required":
   ssh uvfr-lab 'sudo reboot'
   ssh uvfr-lab 'cd /opt/uvfr-telemetry && sudo ./scripts/provision-vm.sh'
   ```

`provision-vm.sh` is idempotent. It installs:

- the standard kernel (stage 1, then reboot)
- `can-utils`, Python 3 venv support, git, make, jq, tmux, rsync
- Docker Engine and the Compose plugin from `download.docker.com`
- `/etc/modules-load.d/uvfr-can.conf` so `can`, `can_raw` and `vcan` load at boot
- `uvfr-vcan0.service`, which creates `vcan0` at boot via
  `/usr/local/sbin/uvfr-setup-vcan` (a copy of `scripts/setup-vcan.sh`)

### Verifying

```bash
ssh uvfr-lab
uname -r                                   # ...-amd64, not ...-cloud-amd64
ip -details -brief link show vcan0         # vcan0 UP
candump vcan0 &  cansend vcan0 123#DEADBEEF  # frame is echoed by candump
docker compose version
```

## Backend: InfluxDB and Grafana

Both run in Docker Compose on the VM (`infrastructure/docker-compose.yml`),
pinned to `influxdb:2.9.1` and `grafana/grafana:13.2.3`, with named volumes
(`influxdb-data`, `influxdb-config`, `grafana-data`) so data survives restarts
and upgrades.

```bash
make infra-up       # creates .env with random secrets if missing, starts both,
                    # creates scoped InfluxDB tokens, regenerates the dashboard
make infra-status
make infra-logs
make infra-down     # stops containers; data stays in the volumes
make firewall       # once: LAN-only rule for published ports (see below)
```

### Secrets and tokens

`make env` (run by `make infra-up`) creates `.env` from `.env.example` with
random secrets. It is `chmod 600`, git-ignored, and never overwritten.
`infrastructure/influxdb/create-tokens.sh` then creates least-privilege tokens:

| Token | Scope | Used by |
|---|---|---|
| `INFLUX_TOKEN` | operator (admin) | administration and tests only |
| `GRAFANA_INFLUX_TOKEN` | read the telemetry bucket | Grafana data source |
| `PIT_INFLUX_TOKEN` | write the telemetry bucket | pit receiver |

The Grafana login is `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` in the
VM's `.env`. To read it without printing other secrets:

```bash
ssh uvfr-lab "grep '^GRAFANA_ADMIN' /opt/uvfr-telemetry/.env"
```

### Network exposure

| Service | Published on | Reachable from |
|---|---|---|
| Grafana | `0.0.0.0:3000` (IPv4) | LAN and Tailscale only: `http://192.168.1.54:3000` |
| InfluxDB | `127.0.0.1:8086` | processes on the VM only (the pit receiver) |

Docker's published ports bypass the host's INPUT chain, so
`infrastructure/firewall/` adds a rule to Docker's `DOCKER-USER` chain:
traffic from the external interface to containers is accepted only from
RFC 1918 ranges and the Tailscale CGNAT range `100.64.0.0/10`. It is
installed as `uvfr-docker-lan-only.service`, re-applied whenever Docker
restarts, and affects only forwarded container traffic (SSH is untouched).
The VM has no global IPv6 address, and nothing is port-forwarded on the
router. Never port-forward either service.

Verified from a LAN workstation: Grafana answers on port 3000; InfluxDB on
port 8086 refuses the connection.

When a separate pit laptop needs to write to InfluxDB, set
`INFLUX_BIND=<VM LAN IP>` in `.env` and give the laptop the
`PIT_INFLUX_TOKEN`; the DOCKER-USER rule still limits it to the LAN.

### Tailscale (optional)

The VM is not on Tailscale yet; joining needs your own login
(`curl -fsSL https://tailscale.com/install.sh | sh; sudo tailscale up`). The
firewall rule already allows the Tailscale range, so Grafana would then be
reachable at the VM's Tailscale address as well.

### Dashboard

Grafana provisions the data source and the dashboard from
`infrastructure/grafana/provisioning/` at start-up. The dashboard JSON is
**generated** (`make dashboard`) from `config/dashboard.yaml`,
`config/alerts.yaml` and `config/channels.yaml`, and is read-only in Grafana.
A test fails if the committed JSON is out of date with the config.

Open `http://192.168.1.54:3000`; the pit dashboard is the home page (folder
"UVFR Telemetry").

### Measured load

| Situation | Grafana CPU | InfluxDB CPU | Notes |
|---|---|---|---|
| Ingesting, nobody watching | about 1% | about 3% | 28 points/s; InfluxDB about 90 MB RAM, Grafana about 300 MB |
| One browser at 1 s refresh | about 64% | about 31% | 45 queries per refresh, full refresh about 470 ms |

The dashboard defaults to a 2 s refresh, which halves the viewing cost; 1 s
works for one viewer. With 2 vCPUs, more than two viewers at 1 s would
saturate the VM. The four Python processes together use under 6% of a core.

## Day-to-day

- `scripts/deploy.sh` copies your working tree (tracked files plus new,
  non-ignored files) to `/opt/uvfr-telemetry` on the VM. `.env`, `.venv`,
  `run/`, `logs/` and `recordings/` on the VM are never overwritten.
- The VM does not start automatically with the host. Start it from the Proxmox
  UI or `qm start 300` when you need it.

## Running without the homelab

Any Linux machine with the `vcan` kernel module and Docker can run the full lab:
run `scripts/setup-vcan.sh` instead of the provisioning script and install the
same packages. Machines without SocketCAN (macOS, Windows, most WSL2 kernels)
can run the unit tests and the pipeline over python-can's UDP-multicast virtual
bus; see the README once that path is in place.
