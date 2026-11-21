"""Re-derive the segment-1 ground truth through the semantic layer (full-size warehouse only)."""
import json
from pathlib import Path

import pytest

from analyst.datagen import DEFAULT_CONFIG, config_sha, load_config

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse.duckdb"
TRUTH = ROOT / "fixtures" / "ground_truth.json"


def _fresh() -> bool:
    if not (DB.exists() and TRUTH.exists()):
        return False
    t = json.loads(TRUTH.read_text(encoding="utf-8"))
    return t.get("generated_with", {}).get("config_sha") == config_sha(load_config(DEFAULT_CONFIG))


@pytest.mark.skipif(not _fresh(), reason="run scripts/generate_data.py and scripts/verify_data.py first")
def test_semantic_layer_reproduces_ground_truth():
    from scripts.check_semantic import main
    assert main(DB, TRUTH) == 0
