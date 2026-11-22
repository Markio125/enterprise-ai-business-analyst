"""HTTP API (SPEC section 20): FastAPI + Server-Sent Events.

    uvicorn analyst.api:app --port 8000

Authentication is mocked (SPEC 1.2): the caller identifies with an `X-User-Id`
header naming a user from config/policies.yaml. Every request is authorised
again inside the pipeline (policies, row-level security), so the header only
selects *who* is asking; it never widens access. Unknown or other users'
sessions / questions return 404.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .service import AnalystService, Conflict, Forbidden, NotFound
from .store import dumps

API = "/api/v1"


class AskBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class ClarifyBody(BaseModel):
    choices: dict[str, str]
    free_text: str | None = None


class NarrowBody(BaseModel):
    option_id: str


class FeedbackBody(BaseModel):
    rating: int
    comment: str | None = Field(None, max_length=2000)


class SessionBody(BaseModel):
    user_id: str | None = None


def create_app(service: AnalystService | None = None) -> FastAPI:
    app = FastAPI(title="Enterprise AI Business Analyst", version="0.7.0",
                  description="Ask questions about payments data in plain English. See docs/SPEC.md section 20.")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                                                      "http://localhost:8000"],
                       allow_methods=["*"], allow_headers=["*"])
    state = {"service": service}

    def svc() -> AnalystService:
        if state["service"] is None:
            state["service"] = AnalystService()
        return state["service"]

    def user(x_user_id: str | None = Header(None)) -> str:
        if not x_user_id:
            raise HTTPException(401, "send X-User-Id (mock authentication)")
        try:
            svc().principal(x_user_id)
        except Forbidden as e:
            raise HTTPException(401, str(e)) from e
        return x_user_id

    @app.exception_handler(NotFound)
    async def _nf(_, e):
        return JSONResponse({"detail": str(e)}, 404)

    @app.exception_handler(Forbidden)
    async def _fb(_, e):
        return JSONResponse({"detail": str(e)}, 403)

    @app.exception_handler(Conflict)
    async def _cf(_, e):
        return JSONResponse({"detail": str(e)}, 409)

    def J(obj, code=200):
        return JSONResponse(json.loads(dumps(obj)), code)

    @app.get("/")
    def root():
        s = svc()
        return {"service": "enterprise-ai-business-analyst", "api": API, "docs": "/docs",
                "llm": s.analyst.llm.name, "data_as_of": s.analyst.as_of.isoformat(),
                "users": sorted(s.policy.users)}

    @app.get(f"{API}/health")
    def health():
        s = svc()
        return {"ok": True, "llm": s.analyst.llm.name, "as_of": s.analyst.as_of.isoformat(),
                "cache": {"hits": s.cache.hits, "misses": s.cache.misses}}

    @app.get(f"{API}/me")
    def me(uid: str = Depends(user)):
        p = svc().principal(uid)
        role = svc().policy.role_of(p)
        return {"user_id": p.user_id, "name": p.name, "role": role.name, "description": role.description,
                "admin": bool(p.attrs.get("admin"))}

    @app.post(f"{API}/sessions", status_code=201)
    def create_session(body: SessionBody | None = None, uid: str = Depends(user)):
        if body and body.user_id and body.user_id != uid:
            raise HTTPException(403, "cannot open a session for another user")
        return svc().create_session(uid)

    @app.post(f"{API}/sessions/{{sid}}/questions", status_code=202)
    def ask(sid: str, body: AskBody, wait: bool = Query(False), uid: str = Depends(user)):
        out = svc().ask(sid, uid, body.text, wait=wait)
        return J(svc().get_question(out["question_id"], uid) | {"stream_url": out["stream_url"]} if wait else out,
                 200 if wait else 202)

    @app.get(f"{API}/questions/{{qid}}")
    def get_question(qid: str, uid: str = Depends(user)):
        return J(svc().get_question(qid, uid))

    @app.get(f"{API}/questions/{{qid}}/steps")
    def get_steps(qid: str, uid: str = Depends(user)):
        return J(svc().get_steps(qid, uid))

    @app.post(f"{API}/questions/{{qid}}/clarify")
    def clarify(qid: str, body: ClarifyBody, wait: bool = Query(False), uid: str = Depends(user)):
        out = svc().clarify(qid, uid, body.choices, wait=wait)
        return J(svc().get_question(qid, uid) if wait else out)

    @app.post(f"{API}/questions/{{qid}}/narrow")
    def narrow(qid: str, body: NarrowBody, wait: bool = Query(False), uid: str = Depends(user)):
        out = svc().narrow(qid, uid, body.option_id, wait=wait)
        return J(svc().get_question(qid, uid) if wait else out)

    @app.post(f"{API}/questions/{{qid}}/feedback", status_code=201)
    def feedback(qid: str, body: FeedbackBody, uid: str = Depends(user)):
        return svc().feedback(qid, uid, body.rating, body.comment)

    @app.post(f"{API}/queries/{{query_id}}/rerun")
    def rerun(query_id: str, uid: str = Depends(user)):
        return J(svc().rerun(query_id, uid))

    @app.get(f"{API}/catalog")
    def catalog(uid: str = Depends(user)):
        return J(svc().catalog(uid))

    @app.get(f"{API}/dq/summary")
    def dq_summary(start: date | None = Query(None, alias="from"), end: date | None = Query(None, alias="to"),
                   uid: str = Depends(user)):
        return J(svc().dq_summary(uid, start, end))

    @app.get(f"{API}/audit")
    def audit(uid: str = Depends(user)):
        return J(svc().audit(uid))

    @app.post(f"{API}/evals/run")
    def run_evals(uid: str = Depends(user)):
        return J(svc().run_evals(uid))

    @app.get(f"{API}/admin/config")
    def get_config(uid: str = Depends(user)):
        return J(svc().get_config(uid))

    @app.put(f"{API}/admin/config")
    def put_config(changes: dict, uid: str = Depends(user)):
        return J(svc().put_config(uid, changes))

    @app.get(f"{API}/questions/{{qid}}/stream")
    async def stream(qid: str, request: Request, after: int = Query(0, ge=0),
                     last_event_id: str | None = Header(None), uid: str = Depends(user)):
        s = svc()
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
        first = s.events_since(qid, uid, start)        # ownership check happens here (404 otherwise)

        async def gen():
            pos = start
            pending = first
            idle = 0.0
            while True:
                for i, ev in enumerate(pending):
                    pos = ev["id"]
                    yield f"id: {ev['id']}\nevent: {ev['event']}\ndata: {dumps(ev)}\n\n"
                    # a question can have several runs (clarify / narrow): only the LAST done ends the stream
                    if ev["event"] == "done" and i == len(pending) - 1:
                        return
                if await request.is_disconnected():
                    return
                pending = await asyncio.to_thread(s.wait_events, qid, pos, 10.0)
                if not pending:
                    idle += 10
                    yield ": keep-alive\n\n"
                    if idle >= 600:
                        return
                else:
                    idle = 0.0

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = create_app()
