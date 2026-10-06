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
