"""Application service behind the API: sessions, background runs, event streams, persistence.

Ownership is enforced here: a user can only see or act on their own sessions,
questions and queries. Anything else is reported as "not found", so ids of other
users' questions can't be probed.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import date

from .executor import QueryGateway
from .intent import Intent
from .pipeline import TERMINAL, WAITING, Analyst, Session
from .settings import load_settings
from .store import ResultCache, Store, now

ANSWER_STATES = TERMINAL


class NotFound(LookupError):
    pass


class Forbidden(PermissionError):
    pass


class Conflict(RuntimeError):
    pass


def _session_to_ctx(s: Session) -> dict:
    return {"clarifications": s.clarifications, "history": [i.as_dict() for i in s.history[-5:]],
            "pending_question": s.pending_question, "clarification_turns": s.clarification_turns,
            "pending_narrowing": s.pending_narrowing, "narrowing_changes": s.narrowing_changes}


def _session_from_ctx(user_id: str, ctx: dict) -> Session:
    names = {f.name for f in fields(Intent)}
    s = Session(user_id, clarifications=dict(ctx.get("clarifications") or {}),
                history=[Intent(**{k: v for k, v in h.items() if k in names}) for h in ctx.get("history") or []],
                pending_question=ctx.get("pending_question"),
                clarification_turns=int(ctx.get("clarification_turns") or 0))
    s.pending_narrowing = ctx.get("pending_narrowing")
    s.narrowing_changes = list(ctx.get("narrowing_changes") or [])
    return s


class AnalystService:
    def __init__(self, analyst: Analyst | None = None, store: Store | None = None, settings: dict | None = None,
                 workers: int | None = None, provider: str | None = None):
        self.settings = settings or (analyst.settings if analyst else load_settings())
        api = self.settings.get("api", {})
        self.store = store or Store(api.get("metadata_db", "data/app.sqlite"))
        self.cache = ResultCache(self.store, int(api.get("cache_ttl_seconds", 900)))
        if analyst is None:
            analyst = Analyst(gateway=QueryGateway(self.settings, cache=self.cache), provider=provider)
        else:
            analyst.gateway.cache = self.cache
        self.analyst = analyst
        self.policy = analyst.policy
        self.pool = ThreadPoolExecutor(max_workers=workers or int(api.get("workers", 4)),
                                       thread_name_prefix="analyst")
        self._sessions: dict[str, Session] = {}
        self._session_locks: dict[str, threading.Lock] = {}
        self._events: dict[str, list[dict]] = {}
        self._runs: dict[str, int] = {}
        self._cond = threading.Condition()
        for p in self.policy.users.values():
            self.store.upsert_user(p.user_id, p.name, p.role, dict(p.attrs))

    # ------------------------------------------------------------------ identity / ownership
    def principal(self, user_id: str | None):
        if not user_id:
            raise Forbidden("missing user")
        try:
            return self.policy.principal(user_id)
        except KeyError as e:
            raise Forbidden(f"unknown user {user_id!r}") from e

    def _own_session(self, sid: str, user_id: str) -> dict:
        row = self.store.session(sid)
        if not row or row["user_id"] != user_id:
            raise NotFound("session not found")
        return row

    def _own_question(self, qid: str, user_id: str) -> dict:
        q = self.store.question(qid)
        if not q or q["user_id"] != user_id:
            raise NotFound("question not found")
        return q

    def _session(self, sid: str, user_id: str) -> Session:
        if sid not in self._sessions:
            row = self._own_session(sid, user_id)
            self._sessions[sid] = _session_from_ctx(user_id, row["context"])
            self._session_locks.setdefault(sid, threading.Lock())
        return self._sessions[sid]

    def is_admin(self, user_id: str) -> bool:
        return bool(self.principal(user_id).attrs.get("admin"))

    # ------------------------------------------------------------------ sessions / questions
    def create_session(self, user_id: str) -> dict:
        self.principal(user_id)
        sid = self.store.create_session(user_id)
        self._sessions[sid] = Session(user_id)
        self._session_locks[sid] = threading.Lock()
        return {"session_id": sid, "user_id": user_id, "created_at": now()}

    def ask(self, sid: str, user_id: str, text: str, wait: bool = False) -> dict:
        self.principal(user_id)
        self._own_session(sid, user_id)
        text = (text or "").strip()
        if not text:
            raise Conflict("empty question")
        if len(text) > 2000:
            raise Conflict("question too long (max 2000 characters)")
        last = self.store.last_question(sid)
        qid = self.store.create_question(sid, user_id, text, last["id"] if last else None)
        self._start(qid, sid, user_id, text, None, wait)
        return {"question_id": qid, "status": self.store.question(qid)["status"],
                "stream_url": f"/api/v1/questions/{qid}/stream"}

    def clarify(self, qid: str, user_id: str, choices: dict[str, str], wait: bool = False) -> dict:
        q = self._own_question(qid, user_id)
        if q["status"] != "AWAITING_CLARIFICATION":
            raise Conflict(f"question is {q['status']}, not awaiting clarification")
        items = {i["key"]: i for i in (q["result"] or {}).get("ambiguity", {}).get("items", [])}
        bad = [k for k, v in choices.items() if k not in items or v not in {o["id"] for o in items[k]["options"]}]
        if bad or not choices:
            raise Conflict(f"unknown clarification key or option: {bad or 'none given'}")
        self.store.record_choice(qid, choices)
        self._start(qid, q["session_id"], user_id, q["text"], dict(choices), wait)
        return {"question_id": qid, "status": self.store.question(qid)["status"]}

    def narrow(self, qid: str, user_id: str, option_id: str, wait: bool = False) -> dict:
        q = self._own_question(qid, user_id)
        if q["status"] != "AWAITING_NARROWING":
            raise Conflict(f"question is {q['status']}, not awaiting narrowing")
        opts = {o["id"] for o in ((q["result"] or {}).get("narrowing") or {}).get("options", [])}
        if option_id not in opts:
            raise Conflict(f"unknown narrowing option {option_id!r}")
        self._start(qid, q["session_id"], user_id, q["text"], {"NARROWING": option_id}, wait)
        return {"question_id": qid, "status": self.store.question(qid)["status"]}

    def _start(self, qid, sid, user_id, text, answers, wait):
        session = self._session(sid, user_id)
        self._runs[qid] = self._runs.get(qid, 0) + 1
        run_no = self._runs[qid]
        self.store.set_status(qid, "RUNNING")
        self._push(qid, {"event": "run_started", "run": run_no, "question": text})
        fut = self.pool.submit(self._run, qid, sid, user_id, text, answers, session, run_no)
        if wait:
            fut.result()

    def _run(self, qid, sid, user_id, text, answers, session, run_no):
        lock = self._session_locks.setdefault(sid, threading.Lock())
        with lock:                           # one run at a time per session (shared conversation state)
            try:
                res = self.analyst.ask(text, user_id, session, answers, on_event=lambda e: self._push(qid, e))
                self.store.record_run(qid, res, run_no)
                done = res.state in TERMINAL
                self.store.set_status(qid, res.state, result=res.as_dict(max_rows=200), done=done)
                self.store.save_session_context(sid, _session_to_ctx(session))
                if res.state == "AWAITING_CLARIFICATION":
                    self._push(qid, {"event": "clarification_needed", "message": res.message,
                                     "items": res.ambiguity.as_dict()["items"]})
                elif res.state == "AWAITING_NARROWING":
                    self._push(qid, {"event": "narrowing_needed", "message": res.message,
                                     "options": res.narrowing["options"]})
                self._push(qid, {"event": "answer", "status": res.state, "answer": res.answer.as_dict()
                                 if res.answer else None})
                self._push(qid, {"event": "done", "status": res.state, "waiting": res.state in WAITING})
            except Exception as e:  # noqa: BLE001
                self.store.set_status(qid, "FAILED", result={"state": "FAILED", "message": str(e)}, done=True)
                self._push(qid, {"event": "error", "message": f"{type(e).__name__}: {e}"})
                self._push(qid, {"event": "done", "status": "FAILED", "waiting": False})

    # ------------------------------------------------------------------ events
    def _push(self, qid: str, ev: dict) -> None:
        with self._cond:
            lst = self._events.setdefault(qid, [])
            lst.append({"id": len(lst) + 1, "ts": time.time(), **ev})
            self._cond.notify_all()

    def events_since(self, qid: str, user_id: str, after: int = 0) -> list[dict]:
        self._own_question(qid, user_id)
        with self._cond:
            if qid not in self._events:       # e.g. after a restart: replay from the store
                return self._replay(qid)[after:]
            return list(self._events[qid][after:])

    def wait_events(self, qid: str, after: int, timeout: float) -> list[dict]:
        with self._cond:
            self._cond.wait_for(lambda: len(self._events.get(qid, [])) > after, timeout=timeout)
            return list(self._events.get(qid, [])[after:])

    def _replay(self, qid: str) -> list[dict]:
        q = self.store.question(qid)
        evs = [{"id": i, "event": "stage_completed", "stage": s["stage"], "status": s["status"],
                "ms": s["latency_ms"], "detail": s["output"]} for i, s in enumerate(self.store.steps(qid), 1)]
        if q and q["status"] != "RUNNING":
            ans = self.store.one("SELECT answer FROM answers WHERE question_id = ?", qid)
            import json
            evs.append({"id": len(evs) + 1, "event": "answer", "status": q["status"],
                        "answer": json.loads(ans["answer"]) if ans else None})
            evs.append({"id": len(evs) + 1, "event": "done", "status": q["status"], "waiting": q["status"] in WAITING})
        return evs

    # ------------------------------------------------------------------ reads
    def get_question(self, qid: str, user_id: str) -> dict:
        q = self._own_question(qid, user_id)
        return {"question_id": qid, "session_id": q["session_id"], "text": q["text"], "status": q["status"],
                "parent_question_id": q["parent_question_id"], "created_at": q["created_at"],
                "completed_at": q["completed_at"], "result": q["result"], "queries": self.store.queries(qid)}

    def get_steps(self, qid: str, user_id: str) -> list[dict]:
        self._own_question(qid, user_id)
        return self.store.steps(qid)

    def rerun(self, query_id: str, user_id: str) -> dict:
        row = self.store.query(query_id)
        if not row:
            raise NotFound("query not found")
        self._own_question(row["question_id"], user_id)
        if not row["generated_sql"]:
            raise Conflict("this query cannot be re-run")
        p = self.principal(user_id)
        # re-executed through the full gate as the CURRENT user, bypassing the cache
        r = self.analyst.gateway.run_sql(row["generated_sql"], p, use_cache=False)
        v = r.validation
        self.store.exec("INSERT INTO access_audit VALUES (?,?,?,?,?,?,?)", f"au_rerun_{time.time_ns()}", p.user_id,
                        p.role, row["question_id"], "QUERY_EXECUTED" if r.ok else "ACCESS_DENIED",
                        '{"rerun": "%s"}' % query_id, now())
        return {"query_id": query_id, "status": r.status, "row_count": r.row_count, "duration_ms": r.duration_ms,
                "cache_hit": r.cache_hit, "columns": r.columns, "rows": r.rows[:200], "data_version": r.data_version,
                "rls": v.rls_predicates_applied if v else [], "error": r.error}

    def feedback(self, qid: str, user_id: str, rating: int, comment: str | None) -> dict:
        self._own_question(qid, user_id)
        if rating not in (-1, 1, 2, 3, 4, 5):
            raise Conflict("rating must be -1, 1 (thumbs) or 1-5")
        return {"feedback_id": self.store.add_feedback(qid, user_id, rating, comment)}

    def catalog(self, user_id: str) -> dict:
        return self.analyst.catalog(self.principal(user_id))

    def dq_summary(self, user_id: str, start: date | None, end: date | None) -> dict:
        self.principal(user_id)
        end = end or self.analyst.as_of
        start = start or date.fromordinal(end.toordinal() - 27)
        if start > end:
            raise Conflict("from must be on or before to")
        res = self.analyst.dq.run(start, end, {"merchant_segment"})
        dv = self.analyst.gateway.executor.data_version(["fact_payment_attempts_raw", "dim_merchant"])
        self.store.record_dq(res, dv)
        return {"window": [start.isoformat(), end.isoformat()], "as_of": self.analyst.as_of.isoformat(),
                "data_version": dv, "checks": [r.as_dict() for r in res],
                "worst": max((r.severity for r in res), key=lambda x: ["OK", "INFO", "WARN", "BLOCK"].index(x),
                             default="OK")}

    def audit(self, user_id: str) -> list[dict]:
        return self.store.audit(None if self.is_admin(user_id) else user_id)

    # ------------------------------------------------------------------ admin
    def run_evals(self, user_id: str) -> dict:
        if not self.is_admin(user_id):
            raise Forbidden("admin only")
        from scripts.check_pipeline import collect
        t0 = time.perf_counter()
        results = collect(self.analyst)
        rows = [{"case_id": n, "passed": ok, "scores": None if ok else {"observed": str(obs)[:500]}} for n, ok, obs in results]
        summary = {"cases": len(rows), "passed": sum(r["passed"] for r in rows),
                   "seconds": round(time.perf_counter() - t0, 1), "llm": self.analyst.llm.name}
        rid = self.store.record_eval(self.analyst.llm.name, {"provider": self.analyst.llm.name}, summary, rows)
        return {"run_id": rid, "summary": summary, "failed": [r for r in rows if not r["passed"]]}

    EDITABLE = {("ambiguity", "mode"), ("ambiguity", "material_threshold_pct"), ("answer", "max_recommendations"),
                ("defaults", "default_time_window_days"), ("api", "cache_ttl_seconds")}

    def get_config(self, user_id: str) -> dict:
        if not self.is_admin(user_id):
            raise Forbidden("admin only")
        return {k: v for k, v in self.settings.items() if k not in ("sql_safety",)} | {
            "editable": sorted(f"{a}.{b}" for a, b in self.EDITABLE)}

    def put_config(self, user_id: str, changes: dict) -> dict:
        if not self.is_admin(user_id):
            raise Forbidden("admin only")
        applied = {}
        for key, val in changes.items():
            sec, _, name = key.partition(".")
            if (sec, name) not in self.EDITABLE:
                raise Conflict(f"{key} is not editable at runtime")
            if (sec, name) == ("ambiguity", "mode") and val not in ("always_ask", "ask_when_material",
                                                                   "assume_and_disclose"):
                raise Conflict("ambiguity.mode must be always_ask | ask_when_material | assume_and_disclose")
            self.settings.setdefault(sec, {})[name] = val
            applied[key] = val
        d = self.analyst.detector
        d.mode = self.settings["ambiguity"].get("mode", d.mode)
        d.threshold = float(self.settings["ambiguity"].get("material_threshold_pct", d.threshold))
        self.analyst.composer.max_recs = int(self.settings.get("answer", {}).get("max_recommendations", 3))
        self.cache.ttl = int(self.settings.get("api", {}).get("cache_ttl_seconds", self.cache.ttl))
        return {"applied": applied}

    def close(self):
        self.pool.shutdown(wait=True)

