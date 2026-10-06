"""The .env loader."""

from __future__ import annotations

import os

from common.env import load_env


def test_parses_and_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\nA=1\nexport B = two \nC='quoted value'\nD=\"x=y\"\nNOT A LINE\n", encoding="utf-8"
    )
    monkeypatch.setenv("A", "already-set")
    for key in ("B", "C", "D"):
        monkeypatch.delenv(key, raising=False)
    values = load_env(env)
    assert values == {"A": "1", "B": "two", "C": "quoted value", "D": "x=y"}
    assert os.environ["A"] == "already-set"
    assert os.environ["C"] == "quoted value"


def test_missing_file_is_fine(tmp_path):
    assert load_env(tmp_path / "nope.env") == {}
