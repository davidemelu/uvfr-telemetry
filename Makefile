# UVFR telemetry lab. Run `make help` for targets.
SHELL := /bin/bash
PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
CAN_IFACE ?= vcan0
SCENARIO ?= normal
LINK ?= perfect
COMPOSE := docker compose --env-file .env -f infrastructure/docker-compose.yml

.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

venv: $(VENV)/.installed ## Create the Python virtualenv and install dependencies

$(VENV)/.installed: requirements.txt requirements-dev.txt
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r requirements-dev.txt
	touch $@

vcan: ## Create/verify the vcan0 virtual CAN interface (sudo)
	./scripts/setup-vcan.sh $(CAN_IFACE)

sim: venv ## Run the fake ECU in the foreground (SCENARIO=normal|overheating|...)
	$(PY) -m simulator --scenario $(SCENARIO)

car-node: venv ## Run the car telemetry node in the foreground
	$(PY) -m car_node

link-sim: venv ## Run the simulated radio link (LINK=perfect|lora_good|lora_marginal|lora_bad|congested)
	$(PY) -m link_sim --profile $(LINK)

link: venv ## Switch the running radio link to profile LINK
	$(PY) -m link_sim.ctl profile $(LINK)

pit: venv ## Run the pit receiver in the foreground
	$(PY) -m pit_receiver

scenario: venv ## Switch the running fake ECU to SCENARIO
	$(PY) -m simulator.ctl set $(SCENARIO)

candump: ## Show live raw CAN traffic on vcan0
	candump -t A $(CAN_IFACE)

candump-decoded: venv ## Show live CAN traffic decoded with the DBC
	candump $(CAN_IFACE) | $(PY) -m cantools decode --single-line dbc/simulated_uvfr.dbc

env: ## Create .env with random secrets (never overwrites an existing one)
	./scripts/init-env.sh

infra-up: env dashboard ## Start InfluxDB + Grafana and create scoped InfluxDB tokens
	$(COMPOSE) up -d --wait influxdb
	./infrastructure/influxdb/create-tokens.sh
	$(COMPOSE) up -d --wait
	@echo "Grafana: http://$$(hostname -I | awk '{print $$1}'):3000  (login: GRAFANA_ADMIN_USER / GRAFANA_ADMIN_PASSWORD in .env)"

infra-down: ## Stop InfluxDB + Grafana (data stays in Docker volumes)
	$(COMPOSE) down

infra-status: ## Show backend container status
	$(COMPOSE) ps

infra-logs: ## Follow backend logs
	$(COMPOSE) logs -f --tail=50

firewall: ## Limit published Docker ports to LAN/VPN sources (sudo, lab VM)
	sudo ./infrastructure/firewall/install.sh

dashboard: venv ## Regenerate the Grafana dashboard from config/*.yaml
	$(PY) scripts/generate_dashboard.py

test: venv ## Unit tests (no vcan, InfluxDB or Docker needed)
	$(PY) -m pytest -m "not vcan and not influx and not integration"

test-vcan: venv ## Multi-process tests on their own bus, vcan1 (safe while the demo runs on vcan0)
	./scripts/setup-vcan.sh vcan1 >/dev/null
	$(PY) -m pytest -m "vcan and not integration"

test-influx: venv ## Tests against the running InfluxDB (uses a temporary bucket)
	$(PY) -m pytest -m influx

.PHONY: help venv vcan sim car-node link-sim link pit scenario candump candump-decoded env infra-up infra-down \
	infra-status infra-logs firewall dashboard test test-vcan test-influx
