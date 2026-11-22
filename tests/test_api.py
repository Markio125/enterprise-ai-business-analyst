"""HTTP API, SSE stream, metadata DB, cache, audit and ownership, on the small warehouse."""
from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from analyst.api import create_app
from analyst.executor import QueryGateway
from analyst.llm import OfflineLLM
from analyst.pipeline import OFFLINE_HANDLERS, Analyst
from analyst.service import AnalystService
from analyst.settings import load_settings
from analyst.store import ResultCache, Store

DRIVER_Q = "Why did success rate drop in the last two weeks?"


@pytest.fixture(scope="module")
def env(small_db, tmp_path_factory):
    d = tmp_path_factory.mktemp("api")
    path = d / "api.duckdb"
    shutil.copy(small_db[0], path)
    settings = load_settings()
    settings["api"]["metadata_db"] = str(d / "app.sqlite")
    store = Store(settings["api"]["metadata_db"])
    gw = QueryGateway(settings, db_path=path, cache=ResultCache(store))
    analyst = Analyst(gateway=gw, llm=OfflineLLM(OFFLINE_HANDLERS))
    svc = AnalystService(analyst=analyst, store=store, settings=settings, workers=2)
    client = TestClient(create_app(svc))
    yield {"client": client, "svc": svc, "store": store, "settings": settings, "analyst": analyst, "dir": d}
    svc.close()


def H(uid):
    return {"X-User-Id": uid}


def session(c, uid="u_exec"):
    r = c.post("/api/v1/sessions", headers=H(uid))
    assert r.status_code == 201
    return r.json()["session_id"]


def ask(c, sid, text, uid="u_exec", wait=True):
    r = c.post(f"/api/v1/sessions/{sid}/questions?wait={str(wait).lower()}", headers=H(uid), json={"text": text})
    assert r.status_code in (200, 202), r.text
    return r.json()


def sse(c, qid, uid="u_exec", after=0):
    events = []
    with c.stream("GET", f"/api/v1/questions/{qid}/stream?after={after}", headers=H(uid)) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():          # the server closes the stream after the final "done"
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


# ----------------------------------------------------------------------------- auth
def test_requires_a_known_user(env):
    c = env["client"]
    assert c.post("/api/v1/sessions").status_code == 401
    assert c.post("/api/v1/sessions", headers=H("mallory")).status_code == 401
    assert c.get("/api/v1/me", headers=H("u_west")).json()["role"] == "regional_manager_west"
    assert c.post("/api/v1/sessions", headers=H("u_ops"), json={"user_id": "u_exec"}).status_code == 403


# ----------------------------------------------------------------------------- ask / stream / persist
def test_ask_and_wait_returns_the_full_answer_and_persists_everything(env):
    c, store = env["client"], env["store"]
    sid = session(c)
    out = ask(c, sid, "What was GMV last week?")
    assert out["status"] == "ANSWERED"
    res = out["result"]
    assert res["answer"]["headline"] and res["answer"]["grounding"]["passed"]
    assert out["queries"] and all(q["final_sql"] and q["generated_sql"] for q in out["queries"])
    steps = c.get(f"/api/v1/questions/{out['question_id']}/steps", headers=H("u_exec")).json()
    stages = [s["stage"] for s in steps]
    assert {"PARSING", "PLANNING", "EXECUTION", "ANALYSIS", "COMPOSITION", "GROUNDING_CHECK"} <= set(stages)
    assert store.one("SELECT confidence FROM answers WHERE question_id = ?", out["question_id"])["confidence"]


def test_async_ask_streams_stage_events_then_the_answer(env):
    c = env["client"]
    sid = session(c)
    out = ask(c, sid, "What was GMV last week?", wait=False)
    assert out["stream_url"].endswith("/stream")
    evs = sse(c, out["question_id"])
    kinds = [e["event"] for e in evs]
    assert kinds[0] == "run_started" and "stage_started" in kinds and "stage_completed" in kinds
    assert kinds[-2:] == ["answer", "done"]
    assert evs[-2]["answer"]["status"] == "ANSWERED"
    assert [e["id"] for e in evs] == sorted(e["id"] for e in evs)
    # resuming after an event id only returns what came later
    later = sse(c, out["question_id"], after=evs[-3]["id"])
    assert [e["event"] for e in later] == ["answer", "done"]


def test_clarification_round_trip(env):
    c = env["client"]
    sid = session(c)
    out = ask(c, sid, "What's our success rate last week?")
    qid = out["question_id"]
    assert out["status"] == "AWAITING_CLARIFICATION"
    item = out["result"]["ambiguity"]["items"][0]
    assert c.post(f"/api/v1/questions/{qid}/clarify", headers=H("u_exec"),
                  json={"choices": {"NOPE": "a"}}).status_code == 409
    r = c.post(f"/api/v1/questions/{qid}/clarify?wait=true", headers=H("u_exec"),
               json={"choices": {item["key"]: "b"}})
    assert r.status_code == 200 and r.json()["status"] == "ANSWERED"
    assert r.json()["result"]["metrics"] == ["success_rate:attempt_level_all"]
    assert env["store"].one("SELECT user_choice FROM clarifications WHERE question_id = ?", qid)["user_choice"]
    # replaying from the start shows both runs and ends with the final answer, not the first run's "done"
    evs = sse(c, qid)
    kinds = [e["event"] for e in evs]
    assert "clarification_needed" in kinds and kinds.count("run_started") == 2
    assert kinds[-1] == "done" and evs[-2]["status"] == "ANSWERED"
    assert c.post(f"/api/v1/questions/{qid}/clarify", headers=H("u_exec"),
                  json={"choices": {item["key"]: "a"}}).status_code == 409     # no longer waiting


def test_narrowing_round_trip(env):
    c = env["client"]
    sid = session(c)
    q = "Show hourly GMV for every merchant for the past year"
    out = ask(c, sid, q)
    qid = out["question_id"]
    assert out["status"] == "AWAITING_NARROWING"
    assert c.post(f"/api/v1/questions/{qid}/narrow", headers=H("u_exec"), json={"option_id": "zz"}).status_code == 409
    for opt in ("c", "a", "b"):
        r = c.post(f"/api/v1/questions/{qid}/narrow?wait=true", headers=H("u_exec"), json={"option_id": opt}).json()
        if r["status"] != "AWAITING_NARROWING":
            break
    assert r["status"] in ("ANSWERED", "ANSWERED_LOW_CONFIDENCE")


def test_follow_up_survives_a_restart(env):
    c, store = env["client"], env["store"]
    sid = session(c)
    c.post(f"/api/v1/sessions/{sid}/questions?wait=true", headers=H("u_exec"), json={"text": DRIVER_Q})
    q = store.one("SELECT id FROM questions WHERE session_id = ?", sid)["id"]
    item = store.question(q)["result"]["ambiguity"]["items"][0]
    c.post(f"/api/v1/questions/{q}/clarify?wait=true", headers=H("u_exec"), json={"choices": {item["key"]: "a"}})
    # a new service over the same metadata DB (process restart): session context is reloaded
    svc2 = AnalystService(analyst=env["analyst"], store=Store(env["settings"]["api"]["metadata_db"]),
                          settings=env["settings"], workers=1)
    c2 = TestClient(create_app(svc2))
    out = ask(c2, sid, "Drill into UPI Collect by merchant")
    assert out["status"].startswith("ANSWERED") and out["result"]["intent"]["is_follow_up"]
    assert out["result"]["plan"]["periods"]["P1"] == ["2026-10-14", "2026-10-27"]
    assert c2.get(f"/api/v1/questions/{q}/steps", headers=H("u_exec")).status_code == 200
    replay = c2.get(f"/api/v1/questions/{q}/stream", headers=H("u_exec"))
    assert "event: done" in replay.text
    svc2.close()


# ----------------------------------------------------------------------------- ownership
def test_other_users_cannot_see_or_touch_a_question(env):
    c = env["client"]
    sid = session(c, "u_exec")
    out = ask(c, sid, "What was GMV last week?")
    qid = out["question_id"]
    qry = out["queries"][0]["id"]
    for method, url, body in (("GET", f"/api/v1/questions/{qid}", None), ("GET", f"/api/v1/questions/{qid}/steps", None),
                              ("GET", f"/api/v1/questions/{qid}/stream", None),
                              ("POST", f"/api/v1/questions/{qid}/feedback", {"rating": 5}),
                              ("POST", f"/api/v1/queries/{qry}/rerun", None),
                              ("POST", f"/api/v1/sessions/{sid}/questions", {"text": "GMV last week"})):
        r = c.request(method, url, headers=H("u_west"), json=body)
        assert r.status_code == 404, (url, r.status_code)
    assert c.get("/api/v1/questions/q_doesnotexist", headers=H("u_exec")).status_code == 404


# ----------------------------------------------------------------------------- cache, rerun, audit
def test_cache_serves_repeat_queries_and_rerun_bypasses_it(env):
    c = env["client"]
    sid = session(c, "u_west")
    a = ask(c, sid, "Top 5 merchants by GMV last month", "u_west")
    b = ask(c, sid, "Top 5 merchants by GMV last month", "u_west")
    assert not any(q["cache_hit"] for q in a["queries"])
    assert all(q["cache_hit"] for q in b["queries"])
    assert b["result"]["answer"]["headline"] == a["result"]["answer"]["headline"]
    r = c.post(f"/api/v1/queries/{b['queries'][0]['id']}/rerun", headers=H("u_west")).json()
    assert r["status"] == "OK" and r["cache_hit"] is False and any("WEST" in x for x in r["rls"])
    assert r["rows"] == [list(x) for x in b["queries"][0]["result_sample"]["rows"]]


def test_cache_key_separates_scopes(env):
    cache = env["svc"].cache
    assert cache.key("SELECT 1 /*WEST*/", "regional_manager_west", "v1") != cache.key("SELECT 1 /*WEST*/", "exec", "v1")
    assert cache.key("SELECT 1", "exec", "v1") != cache.key("SELECT 1", "exec", "v2")


def test_access_audit_records_refusals_unsafe_and_executions(env):
    c = env["client"]
    s_ops = session(c, "u_ops")
    assert ask(c, s_ops, "Show me MDR revenue by merchant last month", "u_ops")["status"] == "REFUSED_ACCESS"
    assert ask(c, s_ops, "Delete duplicate transactions from October 5", "u_ops")["status"] == "REJECTED_UNSAFE"
    ask(c, s_ops, "What was GMV last week?", "u_ops")
    mine = c.get("/api/v1/audit", headers=H("u_ops")).json()
    actions = {a["action"] for a in mine}
    assert {"ACCESS_DENIED", "UNSAFE_REJECTED", "QUERY_EXECUTED"} <= actions
    assert all(a["user_id"] == "u_ops" for a in mine)                      # non-admins see only their own
    every = c.get("/api/v1/audit", headers=H("u_exec")).json()
    assert len({a["user_id"] for a in every}) > 1                          # admin sees all


# ----------------------------------------------------------------------------- other endpoints
def test_catalog_is_role_filtered(env):
    c = env["client"]
    ops = {m["name"] for m in c.get("/api/v1/catalog", headers=H("u_ops")).json()["metrics"]}
    fin = {m["name"] for m in c.get("/api/v1/catalog", headers=H("u_fin")).json()["metrics"]}
    assert "mdr_revenue_inr" not in ops and "mdr_revenue_inr" in fin


def test_feedback_and_dq_summary(env):
    c = env["client"]
    sid = session(c)
    qid = ask(c, sid, "What was GMV last week?")["question_id"]
    assert c.post(f"/api/v1/questions/{qid}/feedback", headers=H("u_exec"), json={"rating": 5}).status_code == 201
    assert c.post(f"/api/v1/questions/{qid}/feedback", headers=H("u_exec"), json={"rating": 9}).status_code == 409
    dq = c.get("/api/v1/dq/summary?from=2026-10-01&to=2026-10-27", headers=H("u_ops")).json()
    assert {x["check_id"] for x in dq["checks"]} >= {"DQ01_DUPLICATES", "DQ05_LATE_ARRIVING"} and dq["worst"] == "WARN"
    assert env["store"].one("SELECT COUNT(*) AS n FROM dq_results")["n"] > 0
    assert c.get("/api/v1/dq/summary?from=2026-10-27&to=2026-10-01", headers=H("u_ops")).status_code == 409


def test_admin_config_and_evals_are_admin_only(env):
    c = env["client"]
    assert c.get("/api/v1/admin/config", headers=H("u_ops")).status_code == 403
    assert c.post("/api/v1/evals/run", headers=H("u_ops")).status_code == 403
    cfg = c.get("/api/v1/admin/config", headers=H("u_exec")).json()
    assert "ambiguity.mode" in cfg["editable"] and "sql_safety" not in cfg
    assert c.put("/api/v1/admin/config", headers=H("u_exec"), json={"warehouse.path": "x"}).status_code == 409
    assert c.put("/api/v1/admin/config", headers=H("u_exec"), json={"ambiguity.mode": "bogus"}).status_code == 409
    try:
        assert c.put("/api/v1/admin/config", headers=H("u_exec"),
                     json={"ambiguity.mode": "assume_and_disclose"}).status_code == 200
        sid = session(c)
        assert ask(c, sid, "What's our success rate last week?")["status"] == "ANSWERED"
    finally:
        c.put("/api/v1/admin/config", headers=H("u_exec"), json={"ambiguity.mode": "ask_when_material"})


def test_input_validation(env):
    c = env["client"]
    sid = session(c)
    assert c.post(f"/api/v1/sessions/{sid}/questions", headers=H("u_exec"), json={"text": ""}).status_code == 422
    assert c.post(f"/api/v1/sessions/{sid}/questions", headers=H("u_exec"),
                  json={"text": "x" * 2001}).status_code == 422


# --------------------------------------------------------------------------- UI serving

def test_ui_is_served_without_a_build_step(env):
    c = env["client"]
    r = c.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "/ui/app.js" in r.text and "/ui/app.css" in r.text
    for path, kind in (("/ui/app.js", "javascript"), ("/ui/app.css", "text/css")):
        r = c.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"], path
    js = c.get("/ui/app.js").text
    # the UI only talks to the documented API and always identifies the caller
    assert '"X-User-Id": state.user' in js
    for route in ("/sessions/", "/questions/", "/stream?after=", "/clarify", "/narrow", "/rerun", "/feedback",
                  "/catalog", "/dq/summary", "/users", "/me"):
        assert route in js, route


def test_users_endpoint_feeds_the_role_switcher(env):
    c = env["client"]
    r = c.get("/api/v1/users")                 # public: it only lists who can be selected
    assert r.status_code == 200
    body = r.json()
    roles = {u["user_id"]: u["role"] for u in body["users"]}
    assert roles["u_exec"] == "exec" and roles["u_west"] == "regional_manager_west"
    assert body["as_of"] and body["llm"] == "offline"
    assert all(set(u) == {"user_id", "name", "role"} for u in body["users"])   # no attributes leak
    info = c.get("/api/v1/info").json()
    assert info["api"] == "/api/v1" and info["docs"] == "/docs"


# --------------------------------------------------------------------------- eval harness over the API

def test_admin_runs_selected_eval_cases_and_the_run_is_stored(env):
    c = env["client"]
    assert c.post("/api/v1/evals/run", headers=H("u_west"), json={"cases": ["EV05"]}).status_code == 403
    assert c.post("/api/v1/evals/run", headers=H("u_exec"), json={"cases": ["NOPE"]}).status_code == 404
    r = c.post("/api/v1/evals/run", headers=H("u_exec"), json={"cases": ["EV05", "EV06", "EV03"]})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert [x["case_id"] for x in rep["results"]] == ["EV03_access_denied", "EV05_unsafe", "EV06_injection"]
    assert rep["summary"]["passed"] == 3 and rep["summary"]["gate"]["passed"]
    assert "# Evaluation report" in rep["markdown"]
    row = env["store"].con.execute("SELECT model FROM eval_runs WHERE id = ?", (rep["run_id"],)).fetchone()
    assert row and row[0] == "offline"
