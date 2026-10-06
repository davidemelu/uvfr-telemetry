#!/usr/bin/env bash
# Create .env from .env.example with freshly generated secrets.
# Never overwrites an existing .env. The file is chmod 600 and git-ignored.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then
    echo ".env already exists; leaving it alone"
    exit 0
fi
rand() { python3 -c "import secrets; print(secrets.token_hex($1))"; }
umask 077
sed -e "s|^INFLUX_ADMIN_PASSWORD=.*|INFLUX_ADMIN_PASSWORD=$(rand 16)|" \
    -e "s|^INFLUX_TOKEN=.*|INFLUX_TOKEN=$(rand 32)|" \
    -e "s|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD=$(rand 12)|" \
    .env.example > .env
echo "created .env with random secrets (Grafana login: see GRAFANA_ADMIN_USER / GRAFANA_ADMIN_PASSWORD)"
