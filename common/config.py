"""Configuration loading shared by every component.

YAML files live in config/. Relative paths inside them are resolved against the
repository root, so the same files work on the lab VM, a laptop or a Pi.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, TypeVar

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"

T = TypeVar("T")


class ConfigError(ValueError):
    """A configuration file is missing, malformed or has invalid values."""


def resolve_path(path: str | Path) -> Path:
    """Absolute path; relative paths are taken from the repository root."""
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = resolve_path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level must be a mapping")
    return data


def check_keys(data: Any, allowed: set[str], where: str, required: set[str] | None = None) -> dict[str, Any]:
    """Reject unknown (usually misspelt) and missing keys in a config mapping."""
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")
    missing = sorted((required if required is not None else allowed) - set(data))
    if missing:
        raise ConfigError(f"{where}: missing key(s) {missing}")
    return data


def build_dataclass(cls: type[T], data: Any, where: str) -> T:
    """Build a flat dataclass from a mapping, rejecting unknown or missing keys.

    Strictness catches typos in YAML: a misspelt key would otherwise be ignored
    and the program would quietly behave differently from what the file says.
    """
    fields = {f.name: f for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    required = {
        name
        for name, f in fields.items()
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }
    check_keys(data, set(fields), where, required)
    try:
        return cls(**data)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: {exc}") from None


def float_pair(value: Any, where: str) -> tuple[float, float]:
    """A [low, high] range from YAML, validated."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"{where}: expected [low, high], got {value!r}")
    low, high = float(value[0]), float(value[1])
    if low > high:
        raise ConfigError(f"{where}: low {low} is greater than high {high}")
    return low, high
