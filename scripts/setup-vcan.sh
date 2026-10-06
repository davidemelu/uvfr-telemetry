#!/usr/bin/env bash
# Create (or verify) a virtual CAN interface for the Phase 0 SIMULATION.
# Idempotent. Re-executes itself with sudo when not run as root.
#
#   ./scripts/setup-vcan.sh            # vcan0
#   ./scripts/setup-vcan.sh vcan1
#
# vcan is a software-only bus inside this Linux kernel. Nothing written to it
# leaves the machine, so it is safe to transmit on (unlike a real vehicle bus).
set -euo pipefail

IFACE="${1:-vcan0}"
[[ "$IFACE" == vcan* ]] || { echo "refusing: '$IFACE' is not a vcan interface" >&2; exit 1; }

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

modprobe can
modprobe can_raw
modprobe vcan

if ! ip link show "$IFACE" >/dev/null 2>&1; then
    ip link add dev "$IFACE" type vcan
fi
# Larger queue avoids ENOBUFS bursts when several processes share the bus.
ip link set "$IFACE" txqueuelen 1000
ip link set up "$IFACE"

ip -details -brief link show "$IFACE"
