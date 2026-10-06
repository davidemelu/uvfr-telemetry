#!/usr/bin/env bash
# Create the UVFR telemetry lab VM on a Proxmox VE host from a Debian 13 cloud
# image. Run ON THE PROXMOX HOST as root. This is the exact procedure used for
# VMID 300 on 2026-10-06; it only ever creates a new VM and refuses to touch an
# existing VMID or snippet.
#
#   ./create-vm.sh --ssh-key /root/my_key.pub [--vmid 300] [--storage local-lvm] ...
#
# Then, from your workstation:
#   scripts/deploy.sh <vm-ssh-target>
#   ssh <vm-ssh-target> 'cd /opt/uvfr-telemetry && sudo ./scripts/provision-vm.sh'
#   (reboot when it exits with code 100, then run it again)
set -euo pipefail

VMID=300
NAME=uvfr-telemetry-lab
CORES=2
MEMORY_MB=3072
BALLOON_MB=2048
DISK_GB=32
STORAGE=local-lvm
BRIDGE=vmbr0
CI_USER=david
IMAGE=/var/lib/vz/template/cloud/debian-13-genericcloud-amd64.qcow2
IMAGE_URL=https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
SNIPPET_STORAGE=local
SNIPPET_DIR=/var/lib/vz/snippets
SNIPPET_NAME=uvfr-telemetry-vendor.yaml
SSH_KEY=""

usage() { sed -n '2,13p' "$0"; exit 1; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --vmid) VMID="$2"; shift 2 ;;
        --name) NAME="$2"; shift 2 ;;
        --cores) CORES="$2"; shift 2 ;;
        --memory) MEMORY_MB="$2"; shift 2 ;;
        --balloon) BALLOON_MB="$2"; shift 2 ;;
        --disk-gb) DISK_GB="$2"; shift 2 ;;
        --storage) STORAGE="$2"; shift 2 ;;
        --bridge) BRIDGE="$2"; shift 2 ;;
        --user) CI_USER="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        *) usage ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "run as root on the Proxmox host" >&2; exit 1; }
command -v qm >/dev/null || { echo "qm not found: is this a Proxmox host?" >&2; exit 1; }
[[ -n "$SSH_KEY" && -f "$SSH_KEY" ]] || { echo "--ssh-key <public key file> is required" >&2; exit 1; }
if qm status "$VMID" >/dev/null 2>&1 || [[ -e "/etc/pve/lxc/$VMID.conf" ]]; then
    echo "VMID $VMID already exists; refusing to touch it. Pick another --vmid." >&2
    exit 1
fi

if [[ ! -f "$IMAGE" ]]; then
    echo "Downloading Debian 13 cloud image to $IMAGE"
    mkdir -p "$(dirname "$IMAGE")"
    curl -fL --output "$IMAGE" "$IMAGE_URL"
fi

# Vendor snippet: only installs the QEMU guest agent. Everything else is done by
# scripts/provision-vm.sh so it is versioned in the repository.
if [[ ! -e "$SNIPPET_DIR/$SNIPPET_NAME" ]]; then
    mkdir -p "$SNIPPET_DIR"
    cat > "$SNIPPET_DIR/$SNIPPET_NAME" <<'EOF'
#cloud-config
# UVFR telemetry lab - installs guest agent only; the rest is provisioned
# from the repo (scripts/provision-vm.sh) so it stays reproducible.
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - [ systemctl, enable, --now, qemu-guest-agent ]
EOF
fi

qm create "$VMID" \
    --name "$NAME" \
    --description "UVFR Formula SAE Phase 0 telemetry SIMULATION lab (vcan0 fake ECU, car node, pit receiver, InfluxDB, Grafana). Experimental, isolated from production, safe to destroy." \
    --tags "uvfr;telemetry;lab" \
    --ostype l26 --cores "$CORES" --sockets 1 --cpu host \
    --memory "$MEMORY_MB" --balloon "$BALLOON_MB" \
    --net0 "virtio,bridge=$BRIDGE,firewall=1" \
    --scsihw virtio-scsi-single \
    --scsi0 "$STORAGE:0,import-from=$IMAGE,discard=on,iothread=1" \
    --ide2 "$STORAGE:cloudinit" \
    --boot order=scsi0 \
    --serial0 socket --vga serial0 \
    --agent enabled=1 \
    --ciuser "$CI_USER" --sshkeys "$SSH_KEY" --ipconfig0 ip=dhcp \
    --cicustom "vendor=$SNIPPET_STORAGE:snippets/$SNIPPET_NAME" \
    --onboot 0
qm disk resize "$VMID" scsi0 "${DISK_GB}G"
qm start "$VMID"

echo "Waiting for the guest agent to report an IP address..."
for _ in $(seq 1 60); do
    ip="$(qm guest cmd "$VMID" network-get-interfaces 2>/dev/null \
        | grep -oE '"ip-address" : "([0-9]{1,3}\.){3}[0-9]{1,3}"' \
        | grep -v '127.0.0.1' | head -n1 | cut -d'"' -f4 || true)"
    if [[ -n "$ip" ]]; then
        echo "VM $VMID ($NAME) is up at $ip (user: $CI_USER)"
        exit 0
    fi
    sleep 5
done
echo "VM started, but no IP reported yet. Check: qm guest cmd $VMID network-get-interfaces" >&2
