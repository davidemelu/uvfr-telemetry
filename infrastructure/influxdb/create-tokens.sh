#!/usr/bin/env bash
# Create least-privilege InfluxDB tokens and record them in .env:
#   GRAFANA_INFLUX_TOKEN  read-only on the telemetry bucket (dashboard)
#   PIT_INFLUX_TOKEN      write-only on the telemetry bucket (pit receiver)
# The admin token (INFLUX_TOKEN) stays for administration and tests only.
# Idempotent: tokens already present in .env are kept.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

[[ -f .env ]] || { echo "no .env; run make env first" >&2; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a

container=uvfr-influxdb
influx() { docker exec "$container" influx "$@" --host http://localhost:8086 --token "$INFLUX_TOKEN"; }

bucket_id="$(influx bucket list --org "$INFLUX_ORG" --name "$INFLUX_BUCKET" --hide-headers | awk '{print $1}')"
[[ -n "$bucket_id" ]] || { echo "bucket $INFLUX_BUCKET not found" >&2; exit 1; }

set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}

make_token() {
    local key="$1" description="$2"; shift 2
    if [[ -n "${!key:-}" ]]; then
        echo "$key already set; keeping it"
        return
    fi
    local token
    token="$(influx auth create --org "$INFLUX_ORG" --description "$description" "$@" --json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
    set_env "$key" "$token"
    echo "created $key ($description)"
}

make_token GRAFANA_INFLUX_TOKEN "uvfr grafana (read telemetry)" --read-bucket "$bucket_id"
make_token PIT_INFLUX_TOKEN "uvfr pit receiver (write telemetry)" --write-bucket "$bucket_id"
chmod 600 .env
