"""Shared fixtures: one reduced-scale warehouse per test session."""
from __future__ import annotations

import duckdb
import pytest

from analyst.datagen import DEFAULT_CONFIG, generate

SCALE = 0.25


@pytest.fixture(scope="session")
def small_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("wh") / "small.duckdb"
    manifest = generate(DEFAULT_CONFIG, path, scale=SCALE)
    return path, manifest


@pytest.fixture(scope="session")
def con(small_db):
    c = duckdb.connect(str(small_db[0]), read_only=True)
    yield c
    c.close()
