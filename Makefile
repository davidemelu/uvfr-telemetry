# UVFR telemetry lab. Run `make help` for targets.
SHELL := /bin/bash
PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
CAN_IFACE ?= vcan0
SCENARIO ?= normal

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

scenario: venv ## Switch the running fake ECU to SCENARIO
	$(PY) -m simulator.ctl set $(SCENARIO)

candump: ## Show live raw CAN traffic on vcan0
	candump -t A $(CAN_IFACE)

candump-decoded: venv ## Show live CAN traffic decoded with the DBC
	candump $(CAN_IFACE) | $(PY) -m cantools decode --single-line dbc/simulated_uvfr.dbc

test: venv ## Unit tests (no vcan, InfluxDB or Docker needed)
	$(PY) -m pytest -m "not vcan and not influx and not integration"

test-vcan: venv ## Tests that need a vcan interface
	$(PY) -m pytest -m "vcan and not integration"

.PHONY: help venv vcan sim scenario candump candump-decoded test test-vcan
