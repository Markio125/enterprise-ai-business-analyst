"""Shared fixtures: one reduced-scale warehouse per test session."""
from __future__ import annotations

import os

import duckdb
import pytest

# Tests must never reach a real model: the server's .env may hold a key and a provider choice.
os.environ["ANALYST_LLM_PROVIDER"] = "offline"
for _k in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    os.environ.pop(_k, None)

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
