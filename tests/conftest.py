from __future__ import annotations

import copy
import csv
import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable

import pytest

from common.config import load_yaml
from common.dbc import load_dbc
from common.env import load_env
from simulator.simulation import Simulator

# Tests get their own virtual bus so they can run while the demo uses vcan0.
CAN_CHANNEL = os.environ.get("UVFR_TEST_CAN_CHANNEL", "vcan1")


@pytest.fixture(scope="session")
def sim_config() -> dict:
    return load_yaml("config/simulation.yaml")


@pytest.fixture(scope="session")
def dbc():
    return load_dbc("dbc/simulated_uvfr.dbc")


@pytest.fixture
def make_sim(sim_config) -> Callable[..., Simulator]:
    """Simulator factory; pass overrides as {"section": {"key": value}}."""

    def _make(seed: int = 1, overrides: dict | None = None) -> Simulator:
        cfg = copy.deepcopy(sim_config)
        for section, values in (overrides or {}).items():
            if isinstance(values, dict):
                cfg[section].update(values)
            else:
                cfg[section] = values
        return Simulator.from_config(cfg, seed=seed)

    return _make


def _vcan_available(channel: str) -> bool:
    try:
        out = subprocess.run(["ip", "link", "show", channel], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False
    return out.returncode == 0 and "UP" in out.stdout


@pytest.fixture(scope="session")
def can_channel() -> str:
    if not _vcan_available(CAN_CHANNEL):
        pytest.skip(f"{CAN_CHANNEL} not available (run scripts/setup-vcan.sh {CAN_CHANNEL})")
    return CAN_CHANNEL


@pytest.fixture
def influx():
    """A temporary bucket in the running InfluxDB (admin token from .env), deleted afterwards."""
    load_env()
    url, token, org = os.environ.get("INFLUX_URL"), os.environ.get("INFLUX_TOKEN"), os.environ.get("INFLUX_ORG")
    if not (url and token and org) or token.startswith("change-me"):
        pytest.skip("no InfluxDB credentials in .env (run make infra-up)")
    try:
        urllib.request.urlopen(f"{url}/health", timeout=2)
    except (urllib.error.URLError, OSError):
        pytest.skip(f"InfluxDB not reachable at {url}")

    def api(method, path, body=None, content_type="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"{url}{path}", data=data, method=method,
                                     headers={"Authorization": f"Token {token}", "Content-Type": content_type,
                                              "Accept": "application/csv"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()

    def query(flux: str) -> list[dict[str, str]]:
        text = api("POST", f"/api/v2/query?org={org}", flux.encode(), "application/vnd.flux").decode()
        return list(csv.DictReader(line for line in text.splitlines() if line.strip()))

    orgs = json.loads(api("GET", f"/api/v2/orgs?org={org}"))
    bucket = f"test_{uuid.uuid4().hex[:8]}"
    created = json.loads(api("POST", "/api/v2/buckets", {"orgID": orgs["orgs"][0]["id"], "name": bucket,
                                                          "retentionRules": [{"type": "expire", "everySeconds": 3600}]}))
    yield {"url": url, "token": token, "org": org, "bucket": bucket, "api": api, "query": query}
    api("DELETE", f"/api/v2/buckets/{created['id']}")
