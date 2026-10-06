#!/usr/bin/env bash
# Install the LAN-only DOCKER-USER rule as a systemd unit that is re-applied
# whenever Docker (re)starts. Run on the lab VM: sudo infrastructure/firewall/install.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0" >&2; exit 1; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 0755 "$here/docker-user-lan-only.sh" /usr/local/sbin/uvfr-docker-user-lan-only
cat > /etc/systemd/system/uvfr-docker-lan-only.service <<'EOF'
[Unit]
Description=UVFR telemetry lab: limit published Docker ports to LAN/VPN sources
After=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/uvfr-docker-user-lan-only apply
ExecStop=/usr/local/sbin/uvfr-docker-user-lan-only remove

[Install]
WantedBy=docker.service
EOF
systemctl daemon-reload
systemctl enable uvfr-docker-lan-only.service
systemctl restart uvfr-docker-lan-only.service
/usr/local/sbin/uvfr-docker-user-lan-only show
