"""Fast tests for the synthetic warehouse generator (runs at reduced scale)."""
from __future__ import annotations

import duckdb
import pytest

from analyst.data_checks import mix_rate_decomposition, run_checks
from analyst.datagen import DEFAULT_CONFIG, generate, load_config

import pandas as pd

SCALE = 0.25
# Checks that are structural (not sensitive to sampling noise at reduced scale)
STRUCTURAL = {
    ("BASE", "date range"), ("BASE", "v_attempts unique attempt_id"), ("BASE", "dimension keys resolve"),
    ("BASE", "no SUCCESS with failure reason"), ("BASE", "settlement net = gross - fee - gst"),
    ("A1", "UPI_COLLECT share jumps"), ("A1", "top driver is UPI_COLLECT"),
    ("A1", "campaign merchant explains the increase"), ("A1", "overall SR declines"),
    ("A2", "target issuer SR collapses in window"), ("A2", "card failures show code 91"),
    ("A4", "duplicates on target date"), ("A4", "no duplicates elsewhere"),
    ("A5", "null merchant keys in window"), ("A5", "no null merchant keys elsewhere"),
    ("A6", "difference fully explained"), ("A7", "pending spike on last day"),
    ("A8", "sale volume spike"), ("A10", "SCD history rows"),
}


@pytest.fixture(scope="module")
def small_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("wh") / "small.duckdb"
    manifest = generate(DEFAULT_CONFIG, path, scale=SCALE)
    return path, manifest


def test_expected_tables_and_views(small_db):
    path, _ = small_db
    con = duckdb.connect(str(path), read_only=True)
    names = set(con.execute("SELECT table_name FROM information_schema.tables").fetchnumpy()["table_name"])
    for t in ["dim_date", "dim_merchant", "dim_payment_method", "dim_issuer_bank", "dim_acquirer",
              "dim_failure_reason", "fact_payment_attempts_raw", "fact_settlements", "fact_refunds",
              "finance_daily_settlement_summary", "_data_versions", "v_attempts", "v_orders"]:
        assert t in names, t


def test_structural_checks_pass(small_db):
    path, _ = small_db
    cfg = load_config(DEFAULT_CONFIG) | {"_small": True}
    rep = run_checks(duckdb.connect(str(path), read_only=True), cfg)
    failed = [c for c in rep.checks if (c.anomaly, c.name) in STRUCTURAL and not c.passed]
    assert not failed, failed


def test_deterministic(small_db, tmp_path):
    _, first = small_db
    second = generate(DEFAULT_CONFIG, tmp_path / "again.duckdb", scale=SCALE)
    assert first["rows"] == second["rows"]
    assert first["special_entities"] == second["special_entities"]


def test_decomposition_is_exact():
    df = pd.DataFrame({
        "period": ["P0", "P0", "P1", "P1"],
        "segment": ["A", "B", "A", "B"],
        "denom": [800, 200, 600, 400],
        "succ": [720, 160, 540, 320],   # A: 90% both periods, B: 80% both periods -> pure mix
    })
    _, tot = mix_rate_decomposition(df)
    assert tot["delta_pp"] == pytest.approx(tot["mix_effect_pp"] + tot["rate_effect_pp"])
    assert tot["rate_effect_pp"] == pytest.approx(0.0)
    assert tot["delta_pp"] == pytest.approx(-2.0)
