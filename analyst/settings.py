"""Loads config/analyst.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS = ROOT / "config" / "analyst.yaml"


def load_settings(path: Path | str = DEFAULT_SETTINGS, **overrides) -> dict:
    """Return the settings dict. `overrides` are 'section__key=value' pairs (handy in tests)."""
    with open(path, encoding="utf-8") as f:
        s = yaml.safe_load(f)
    for k, v in overrides.items():
        section, _, key = k.partition("__")
        s.setdefault(section, {})[key] = v
    db = Path(s["warehouse"]["path"])
    s["warehouse"]["path"] = str(db if db.is_absolute() else ROOT / db)
    return s
