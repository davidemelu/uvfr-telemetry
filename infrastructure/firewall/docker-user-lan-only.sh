#!/usr/bin/env bash
# Restrict published Docker ports (Grafana) to private and VPN source addresses.
#
# Docker's published ports bypass the host's INPUT chain, so the place to filter
# them is the DOCKER-USER chain. Traffic entering on the external interface is
# allowed only from RFC 1918 LAN ranges and the Tailscale CGNAT range; replies
# to connections the containers opened themselves are always allowed.
# Only forwarded traffic into containers is affected; SSH to the VM is not.
#
#   sudo ./docker-user-lan-only.sh apply    # idempotent
#   sudo ./docker-user-lan-only.sh remove
#   sudo ./docker-user-lan-only.sh show
set -euo pipefail

CHAIN=UVFR-LAN-ONLY
ALLOWED=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10)
EXT_IF="${EXT_IF:-$(ip route show default | awk '{print $5; exit}')}"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -n "$EXT_IF" ]] || { echo "cannot determine the external interface" >&2; exit 1; }

apply() {
    iptables -L DOCKER-USER -n >/dev/null 2>&1 || { echo "DOCKER-USER chain missing; is Docker running?" >&2; exit 1; }
    iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
    iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    for net in "${ALLOWED[@]}"; do
        iptables -A "$CHAIN" -s "$net" -j RETURN
    done
    iptables -A "$CHAIN" -j DROP
    iptables -C DOCKER-USER -i "$EXT_IF" -j "$CHAIN" 2>/dev/null || iptables -I DOCKER-USER -i "$EXT_IF" -j "$CHAIN"
    echo "DOCKER-USER: traffic from $EXT_IF to containers limited to ${ALLOWED[*]}"
}

remove() {
    while iptables -C DOCKER-USER -i "$EXT_IF" -j "$CHAIN" 2>/dev/null; do
        iptables -D DOCKER-USER -i "$EXT_IF" -j "$CHAIN"
    done
    iptables -F "$CHAIN" 2>/dev/null || true
    iptables -X "$CHAIN" 2>/dev/null || true
    echo "removed $CHAIN"
}

case "${1:-apply}" in
    apply) apply ;;
    remove) remove ;;
    show) iptables -L DOCKER-USER -n -v; iptables -L "$CHAIN" -n -v 2>/dev/null || true ;;
    *) echo "usage: $0 apply|remove|show" >&2; exit 2 ;;
esac
