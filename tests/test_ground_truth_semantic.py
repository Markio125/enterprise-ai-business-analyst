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


@pytest.mark.skipif(not _fresh(), reason="run scripts/generate_data.py and scripts/verify_data.py first")
def test_safety_gate_end_to_end():
    from scripts.check_safety import main
    assert main() == 0


@pytest.mark.skipif(not _fresh(), reason="run scripts/generate_data.py and scripts/verify_data.py first")
def test_question_pipeline_end_to_end():
    from scripts.check_pipeline import main
    assert main("offline") == 0


@pytest.mark.skipif(not _fresh(), reason="run scripts/generate_data.py and scripts/verify_data.py first")
def test_eval_suite_meets_the_gate():
    from analyst.evals import load_cases, run_suite
    from analyst.pipeline import Analyst
    rep = run_suite(Analyst(provider="offline"), load_cases())
    failed = [(r["case_id"], r["graders"]) for r in rep["results"] if not r["passed"]]
    assert rep["summary"]["gate"]["passed"] and not failed, failed
