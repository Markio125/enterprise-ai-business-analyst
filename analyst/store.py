"""Application / metadata DB (SPEC section 19), SQLite, separate from the warehouse.

Holds sessions, questions, the per-stage trace (run_steps), every executed query,
clarifications, answers, feedback, the access audit, the query-result cache, DQ
results and eval runs. One connection guarded by a lock: the API runs pipelines in
worker threads and SQLite serialises writes anyway.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, role TEXT, attributes JSON);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, user_id TEXT, created_at TIMESTAMP, context JSON);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY, session_id TEXT, user_id TEXT, text TEXT, status TEXT, parent_question_id TEXT,
  created_at TIMESTAMP, completed_at TIMESTAMP, result JSON);
CREATE TABLE IF NOT EXISTS run_steps (
  id TEXT PRIMARY KEY, question_id TEXT, seq INTEGER, stage TEXT, step_ref TEXT, input JSON, output JSON,
  status TEXT, error TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, latency_ms INTEGER,
  created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS queries (
  id TEXT PRIMARY KEY, question_id TEXT, step_ref TEXT, generated_sql TEXT, final_sql TEXT, validation JSON,
  rls_applied JSON, status TEXT, row_count INTEGER, duration_ms INTEGER, cache_hit BOOLEAN, data_version TEXT,
  result_sample JSON, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS clarifications (id TEXT PRIMARY KEY, question_id TEXT, ambiguity JSON, user_choice JSON,
  created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS answers (question_id TEXT PRIMARY KEY, answer JSON, confidence TEXT, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, question_id TEXT, user_id TEXT, rating INTEGER,
  comment TEXT, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS access_audit (id TEXT PRIMARY KEY, user_id TEXT, role TEXT, question_id TEXT,
  action TEXT, details JSON, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS query_cache (key TEXT PRIMARY KEY, result JSON, data_version TEXT, created_at TIMESTAMP,
  expires_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS dq_results (id TEXT PRIMARY KEY, check_id TEXT, table_name TEXT, window_start DATE,
  window_end DATE, data_version TEXT, severity TEXT, metric REAL, details JSON, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS eval_runs (id TEXT PRIMARY KEY, started_at TIMESTAMP, model TEXT, config JSON, summary JSON);
CREATE TABLE IF NOT EXISTS eval_results (run_id TEXT, case_id TEXT, passed BOOLEAN, scores JSON, answer JSON,
  latency_ms INTEGER, cost_usd REAL);
CREATE INDEX IF NOT EXISTS ix_questions_session ON questions(session_id);
CREATE INDEX IF NOT EXISTS ix_steps_question ON run_steps(question_id, seq);
CREATE INDEX IF NOT EXISTS ix_queries_question ON queries(question_id);
"""


def _default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if hasattr(o, "as_dict"):
        return o.as_dict()
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


def dumps(o) -> str:
    return json.dumps(o, default=_default, ensure_ascii=False)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL" if str(path) != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self.con.executescript(SCHEMA)
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ low level
    def exec(self, sql: str, *params) -> None:
        with self.lock:
            self.con.execute(sql, params)
            self.con.commit()

    def one(self, sql: str, *params) -> dict | None:
        with self.lock:
            r = self.con.execute(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql: str, *params) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ users / sessions
    def upsert_user(self, uid: str, name: str, role: str, attrs: dict) -> None:
        self.exec("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?)", uid, name, role, dumps(attrs))

    def create_session(self, user_id: str) -> str:
        sid = new_id("ses")
        self.exec("INSERT INTO sessions VALUES (?, ?, ?, ?)", sid, user_id, now(), dumps({}))
        return sid

    def session(self, sid: str) -> dict | None:
        r = self.one("SELECT * FROM sessions WHERE id = ?", sid)
        if r:
            r["context"] = json.loads(r["context"] or "{}")
        return r

    def save_session_context(self, sid: str, ctx: dict) -> None:
        self.exec("UPDATE sessions SET context = ? WHERE id = ?", dumps(ctx), sid)

    # ------------------------------------------------------------------ questions
    def create_question(self, sid: str, user_id: str, text: str, parent: str | None) -> str:
        qid = new_id("q")
        self.exec("INSERT INTO questions (id, session_id, user_id, text, status, parent_question_id, created_at) "
                  "VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)", qid, sid, user_id, text, parent, now())
        return qid

    def question(self, qid: str) -> dict | None:
        r = self.one("SELECT * FROM questions WHERE id = ?", qid)
        if r and r.get("result"):
            r["result"] = json.loads(r["result"])
        return r

    def set_status(self, qid: str, status: str, result: dict | None = None, done: bool = False) -> None:
        if result is not None:
            self.exec("UPDATE questions SET status = ?, result = ?, completed_at = ? WHERE id = ?",
                      status, dumps(result), now() if done else None, qid)
        else:
            self.exec("UPDATE questions SET status = ? WHERE id = ?", status, qid)

    def last_question(self, sid: str) -> dict | None:
        return self.one("SELECT id FROM questions WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                        sid)

    # ------------------------------------------------------------------ run record
    def record_run(self, qid: str, res, run_no: int) -> None:
        """Persist the trace, the queries, the answer, clarifications and the access audit of one run."""
        base = self.one("SELECT COALESCE(MAX(seq), 0) AS m FROM run_steps WHERE question_id = ?", qid)["m"]
        with self.lock:
            cur = self.con.cursor()
            for i, t in enumerate(res.trace, 1):
                u = t.get("usage") or {}
                d = t.get("detail") or {}
                cur.execute("INSERT INTO run_steps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (new_id("rs"), qid, base + i, t["stage"], str(d.get("step", "")) or None,
                             dumps({"run": run_no}), dumps(d), t["status"],
                             str(d["error"]) if isinstance(d, dict) and d.get("error") else None,
                             u.get("model"), u.get("input_tokens"), u.get("output_tokens"), None, t.get("ms"), now()))
            for s in res.steps:
                cur.execute("INSERT INTO queries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (f"{qid}:{run_no}:{s.step_id}", qid, s.step_id, s.generated_sql, s.sql,
                             dumps({"status": s.status, "error": s.error, "attempts": s.attempts}),
                             dumps(s.rls), s.status, s.row_count, s.duration_ms, bool(s.cache_hit),
                             s.data_version, dumps({"columns": s.columns, "rows": s.rows[:200]}), now()))
                action = {"REFUSED_ACCESS": "ACCESS_DENIED", "REJECTED_UNSAFE": "UNSAFE_REJECTED"}.get(
                    s.status, "QUERY_EXECUTED" if s.status in ("OK", "TRUNCATED") else None)
                if action:
                    cur.execute("INSERT INTO access_audit VALUES (?,?,?,?,?,?,?)",
                                (new_id("au"), res.user["id"], res.user["role"], qid, action,
                                 dumps({"step": s.step_id, "rls": s.rls, "rows": s.row_count,
                                        "cache_hit": s.cache_hit}), now()))
            if res.state in ("REFUSED_ACCESS", "REJECTED_UNSAFE") and not any(
                    s.status in ("REFUSED_ACCESS", "REJECTED_UNSAFE") for s in res.steps):
                cur.execute("INSERT INTO access_audit VALUES (?,?,?,?,?,?,?)",
                            (new_id("au"), res.user["id"], res.user["role"], qid,
                             "ACCESS_DENIED" if res.state == "REFUSED_ACCESS" else "UNSAFE_REJECTED",
                             dumps({"question": res.question, "refusal": res.refusal, "message": res.message}), now()))
            if res.ambiguity and res.ambiguity.items:
                cur.execute("INSERT INTO clarifications VALUES (?,?,?,?,?)",
                            (new_id("cl"), qid, dumps(res.ambiguity.as_dict()), None, now()))
            if res.answer is not None:
                cur.execute("INSERT OR REPLACE INTO answers VALUES (?,?,?,?)",
                            (qid, dumps(res.answer.as_dict()), res.answer.confidence.get("level"), now()))
            self.con.commit()

    def record_choice(self, qid: str, choice: dict) -> None:
        self.exec("UPDATE clarifications SET user_choice = ? WHERE id = (SELECT id FROM clarifications WHERE "
                  "question_id = ? AND user_choice IS NULL ORDER BY created_at DESC LIMIT 1)", dumps(choice), qid)

    def steps(self, qid: str) -> list[dict]:
        out = self.all("SELECT seq, stage, step_ref, status, error, model, tokens_in, tokens_out, latency_ms, output, "
                       "created_at FROM run_steps WHERE question_id = ? ORDER BY seq", qid)
        for r in out:
            r["output"] = json.loads(r["output"] or "{}")
        return out

    def queries(self, qid: str) -> list[dict]:
        out = self.all("SELECT * FROM queries WHERE question_id = ? ORDER BY created_at, rowid", qid)
        for r in out:
            for k in ("validation", "rls_applied", "result_sample"):
                r[k] = json.loads(r[k] or "null")
            r["cache_hit"] = bool(r["cache_hit"])
        return out

    def query(self, query_id: str) -> dict | None:
        r = self.one("SELECT * FROM queries WHERE id = ?", query_id)
        return r

    def add_feedback(self, qid: str, user_id: str, rating: int, comment: str | None) -> str:
        fid = new_id("fb")
        self.exec("INSERT INTO feedback VALUES (?,?,?,?,?,?)", fid, qid, user_id, rating, comment, now())
        return fid

    def audit(self, user_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM access_audit" + (" WHERE user_id = ?" if user_id else "") + " ORDER BY created_at, rowid"
        out = self.all(q, *([user_id] if user_id else []))
        for r in out:
            r["details"] = json.loads(r["details"] or "{}")
        return out

    def record_dq(self, results, data_version: str | None) -> None:
        with self.lock:
            for r in results:
                self.con.execute("INSERT INTO dq_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (new_id("dq"), r.check_id, r.table, r.window[0], r.window[1], data_version,
                                  r.severity, r.metric, dumps(r.as_dict()), now()))
            self.con.commit()

    def record_eval(self, model: str, config: dict, summary: dict, results: list[dict]) -> str:
        rid = new_id("ev")
        with self.lock:
            self.con.execute("INSERT INTO eval_runs VALUES (?,?,?,?,?)", (rid, now(), model, dumps(config),
                                                                          dumps(summary)))
            for r in results:
                self.con.execute("INSERT INTO eval_results VALUES (?,?,?,?,?,?,?)",
                                 (rid, r["case_id"], r["passed"], dumps(r.get("scores")), dumps(r.get("answer")),
                                  r.get("latency_ms"), r.get("cost_usd")))
            self.con.commit()
        return rid


class ResultCache:
    """Query-result cache (SPEC 17). Key = sha256(final SQL incl. RLS literals + role + data version), so a
    change in the data or a different scope can never be served a stale or foreign result."""

    def __init__(self, store: Store, ttl_seconds: int = 900):
        self.store, self.ttl = store, ttl_seconds
        self.hits = self.misses = 0

    @staticmethod
    def key(sql: str, role: str, data_version: str | None) -> str:
        return hashlib.sha256(f"{sql}\x00{role}\x00{data_version}".encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        r = self.store.one("SELECT result, created_at, expires_at FROM query_cache WHERE key = ?", key)
        if not r or r["expires_at"] < now():
            self.misses += 1
            return None
        self.hits += 1
        out = json.loads(r["result"])
        out["cached_at"] = r["created_at"]
        return out

    def put(self, key: str, result: dict, data_version: str | None) -> None:
        exp = (datetime.now(timezone.utc) + timedelta(seconds=self.ttl)).isoformat(timespec="seconds")
        self.store.exec("INSERT OR REPLACE INTO query_cache VALUES (?,?,?,?,?)", key, dumps(result), data_version,
                        now(), exp)

    def purge_expired(self) -> None:
        self.store.exec("DELETE FROM query_cache WHERE expires_at < ?", now())
