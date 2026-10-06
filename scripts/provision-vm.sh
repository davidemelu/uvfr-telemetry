#!/usr/bin/env bash
# Provision a Debian 13 host as the UVFR telemetry lab (Phase 0 simulation).
#
# Idempotent: safe to re-run. Run as root (or via sudo) ON THE VM:
#
#   sudo ./scripts/provision-vm.sh
#
# Stage 1  Make sure the running kernel has SocketCAN + vcan. Debian cloud
#          images ship linux-image-cloud-amd64, which has no CAN drivers, so the
#          standard linux-image-amd64 kernel is installed and made the GRUB
#          default. Exits with code 100 when a reboot is needed; reboot and run
#          the script again.
# Stage 2  Remove the cloud kernel, install system packages (can-utils, Python
#          venv support, Docker Engine + compose plugin), autoload the CAN
#          modules and install a systemd unit that creates vcan0 at boot.
#
# Nothing here touches anything outside this VM.
set -euo pipefail

REBOOT_REQUIRED=100
TARGET_USER="${SUDO_USER:-${TARGET_USER:-}}"
REPO_DIR="${REPO_DIR:-/opt/uvfr-telemetry}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == "debian" ]] || die "this script targets Debian (found ${ID:-unknown})"
CODENAME="${VERSION_CODENAME:?}"

export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

# ---------------------------------------------------------------------------
# Stage 1: kernel with CAN support
# ---------------------------------------------------------------------------
running_kernel="$(uname -r)"
if [[ "$running_kernel" == *-cloud-* ]]; then
    log "Running cloud kernel $running_kernel (no SocketCAN/vcan). Installing standard kernel."
    apt-get update -q
    apt-get install "${APT_OPTS[@]}" linux-image-amd64

    generic_kernel="$(find /boot -maxdepth 1 -name 'vmlinuz-*-amd64' ! -name '*cloud*' -printf '%f\n' \
        | sed 's/^vmlinuz-//' | sort -V | tail -n1)"
    [[ -n "$generic_kernel" ]] || die "standard kernel image not found in /boot"
    [[ -e "/lib/modules/$generic_kernel/kernel/drivers/net/can/vcan.ko" ]] \
        || [[ -e "/lib/modules/$generic_kernel/kernel/drivers/net/can/vcan.ko.xz" ]] \
        || die "kernel $generic_kernel has no vcan module"

    # GRUB sorts "-cloud-amd64" above "-amd64" for the same version, so pin the
    # standard kernel explicitly by its menu entry title.
    sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=saved/' /etc/default/grub
    update-grub
    submenu="$(awk -F"'" '/^submenu /{print $2; exit}' /boot/grub/grub.cfg)"
    entry="$(awk -F"'" -v k="$generic_kernel" \
        '/^[[:space:]]*menuentry / && index($2, k) && $2 !~ /recovery/ {print $2; exit}' /boot/grub/grub.cfg)"
    [[ -n "$submenu" && -n "$entry" ]] || die "could not find GRUB entry for $generic_kernel"
    grub-set-default "${submenu}>${entry}"
    log "GRUB default set to: ${submenu} > ${entry}"
    log "REBOOT REQUIRED: run 'sudo reboot', then run this script again."
    exit "$REBOOT_REQUIRED"
fi

modinfo vcan >/dev/null 2>&1 || die "running kernel $running_kernel has no vcan module"
log "Running kernel $running_kernel has vcan support."

# ---------------------------------------------------------------------------
# Stage 2: clean up, packages, CAN autoload, Docker
# ---------------------------------------------------------------------------
mapfile -t cloud_kernels < <(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 'linux-image-*cloud*' 2>/dev/null \
    | awk '$1 ~ /^[ih]i/ {print $2}')
if ((${#cloud_kernels[@]})); then
    log "Removing unused cloud kernel packages: ${cloud_kernels[*]}"
    apt-get purge "${APT_OPTS[@]}" "${cloud_kernels[@]}"
    sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=0/' /etc/default/grub
    update-grub
fi

log "Installing system packages"
apt-get update -q
apt-get install "${APT_OPTS[@]}" --no-install-recommends \
    can-utils iproute2 kmod \
    python3 python3-venv python3-dev \
    git make curl ca-certificates gnupg jq tmux rsync \
    iptables

log "Autoloading CAN kernel modules"
cat > /etc/modules-load.d/uvfr-can.conf <<'EOF'
# UVFR telemetry lab: SocketCAN core, raw sockets and the virtual CAN driver.
can
can_raw
vcan
EOF
modprobe can
modprobe can_raw
modprobe vcan

log "Installing vcan0 boot service (SIMULATION ONLY)"
install -m 0755 "$SCRIPT_DIR/setup-vcan.sh" /usr/local/sbin/uvfr-setup-vcan
cat > /etc/systemd/system/uvfr-vcan0.service <<'EOF'
[Unit]
Description=UVFR telemetry lab: virtual CAN interface vcan0 (simulation only)
After=systemd-modules-load.service
Before=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/uvfr-setup-vcan vcan0

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now uvfr-vcan0.service

if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker Engine from download.docker.com ($CODENAME)"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $CODENAME
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    apt-get update -q
    apt-get install "${APT_OPTS[@]}" docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

if [[ -n "$TARGET_USER" ]] && id "$TARGET_USER" >/dev/null 2>&1; then
    if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx docker; then
        log "Adding $TARGET_USER to the docker group (log out and back in to apply)"
        usermod -aG docker "$TARGET_USER"
    fi
    install -d -o "$TARGET_USER" -g "$TARGET_USER" -m 0755 "$REPO_DIR"
fi

log "Provisioning complete"
echo "  kernel : $(uname -r)"
echo "  vcan0  : $(ip -br link show vcan0 2>/dev/null || echo missing)"
echo "  docker : $(docker --version)"
echo "  compose: $(docker compose version)"
echo "  python : $(python3 --version)"
echo "  repo   : $REPO_DIR"
