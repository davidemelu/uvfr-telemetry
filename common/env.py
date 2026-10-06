"""Minimal .env loader (KEY=VALUE lines) so secrets stay out of config files."""

from __future__ import annotations

import os
from pathlib import Path

from common.config import REPO_ROOT


def load_env(path: str | Path | None = None, override: bool = False) -> dict[str, str]:
    """Load .env into os.environ (existing variables win unless override). Returns what was read."""
    p = Path(path) if path is not None else REPO_ROOT / ".env"
    values: dict[str, str] = {}
    if not p.is_file():
        return values
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return values
