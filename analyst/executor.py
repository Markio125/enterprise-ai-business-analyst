"""Read-only, sandboxed execution of validated SQL (SPEC section 10.2).

Defence in depth: even if the validator had a hole, the connection itself is
read-only, has external access disabled (no files, URLs or extensions), and its
configuration is locked. The executor refuses anything that does not carry a
valid seal from the validator, so the only path to the database is through it.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from .policies import AccessDenied, Policy, Principal
from .semantic import QuerySpec, SemanticModel
from .settings import load_settings
from .sqlsafety import SqlValidator, ValidationResult, verify_seal


class UnvalidatedQuery(PermissionError):
    pass


@dataclass
class ExecutionResult:
    status: str                       # OK | TRUNCATED | TIMEOUT | ERROR
    sql: str
    columns: list[dict] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    duration_ms: int = 0
    data_version: str | None = None
    error: str | None = None
    validation: ValidationResult | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("OK", "TRUNCATED")

    def df(self):
        import pandas as pd
        return pd.DataFrame(self.rows, columns=[c["name"] for c in self.columns])

    def as_dict(self) -> dict:
        return {"status": self.status, "row_count": self.row_count, "truncated": self.truncated,
                "duration_ms": self.duration_ms, "columns": self.columns, "rows": self.rows,
                "data_version": self.data_version, "error": self.error}


class ReadOnlyExecutor:
    def __init__(self, db_path: str | Path, settings: dict):
        self.settings = settings
        wh = settings["warehouse"]
        self.timeout_s = wh["statement_timeout_ms"] / 1000
        self.max_rows = wh["max_result_rows"]
        self.con = duckdb.connect(str(db_path), read_only=True, config={
            "enable_external_access": "false",
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "memory_limit": str(wh.get("memory_limit", "1GB")),
            "threads": str(wh.get("threads", 4)),
        })
        self.con.execute("SET lock_configuration = true")
        self._lock = threading.Lock()

    def schema(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for t, c, ty in self.con.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main'").fetchall():
            out.setdefault(t, {})[c] = ty
        return out

    def explain(self, sql: str):
        with self._lock:
            cur = self.con.cursor()
            try:
                return cur.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchall()[0][1]
            finally:
                cur.close()

    def _view_bases(self) -> dict[str, set[str]]:
        """view -> base tables it reads (recursively), parsed from the view definitions."""
        if getattr(self, "_bases", None) is None:
            import sqlglot
            from sqlglot import exp
            defs = dict(self.con.execute("SELECT view_name, sql FROM duckdb_views() WHERE NOT internal").fetchall())
            direct = {}
            for v, ddl in defs.items():
                try:
                    direct[v] = {t.name for t in sqlglot.parse_one(ddl, read="duckdb").find_all(exp.Table)} - {v}
                except Exception:  # noqa: BLE001
                    direct[v] = set()

            def expand(v, seen=()):
                out = set()
                for t in direct.get(v, ()):
                    out |= expand(t, seen + (v,)) if t in direct and t not in seen else {t}
                return out
            self._bases = {v: expand(v) for v in direct}
        return self._bases

    def data_version(self, tables: list[str]) -> str | None:
        if not tables:
            return None
        bases = self._view_bases()
        tables = sorted({b for t in tables for b in bases.get(t, {t})})
        with self._lock:
            rows = self.con.execute(
                "SELECT table_name, version FROM _data_versions WHERE table_name IN "
                f"({', '.join('?' * len(tables))})", tables).fetchall()
        return ";".join(sorted(v for _, v in rows)) or None

    def execute(self, v: ValidationResult) -> ExecutionResult:
        if not verify_seal(v):
            raise UnvalidatedQuery("refusing to run SQL that did not pass the validator unchanged")
        sql = v.rewritten_sql
        started = time.perf_counter()
        timed_out = threading.Event()
        with self._lock:
            cur = self.con.cursor()

            def interrupt():
                timed_out.set()
                cur.interrupt()
            timer = threading.Timer(self.timeout_s, interrupt)
            timer.start()
            try:
                cur.execute(sql)
                cols = [{"name": d[0], "type": str(d[1])} for d in cur.description]
                rows = cur.fetchmany(self.max_rows + 1)
            except Exception as e:  # noqa: BLE001
                ms = int((time.perf_counter() - started) * 1000)
                if timed_out.is_set():
                    return ExecutionResult("TIMEOUT", sql, duration_ms=ms, validation=v,
                                           error=f"query exceeded {self.timeout_s:.0f}s and was cancelled")
                return ExecutionResult("ERROR", sql, duration_ms=ms, validation=v, error=str(e).splitlines()[0])
            finally:
                timer.cancel()
                cur.close()
        ms = int((time.perf_counter() - started) * 1000)
        truncated = len(rows) > self.max_rows
        rows = [list(r) for r in rows[: self.max_rows]]
        return ExecutionResult("TRUNCATED" if truncated else "OK", sql, columns=cols, rows=rows,
                               row_count=len(rows), truncated=truncated, duration_ms=ms,
                               data_version=self.data_version(v.tables_used), validation=v)

    def close(self):
        self.con.close()


class QueryGateway:
    """The single entry point for user-derived queries: validate -> (seal) -> execute."""

    def __init__(self, settings: dict | None = None, model: SemanticModel | None = None,
                 policy: Policy | None = None, db_path: str | Path | None = None):
        self.settings = settings or load_settings()
        self.model = model or SemanticModel.load()
        self.policy = policy or Policy.load()
        self.executor = ReadOnlyExecutor(db_path or self.settings["warehouse"]["path"], self.settings)
        self.validator = SqlValidator(self.model, self.policy, self.settings, self.executor.schema(),
                                      explain=self.executor.explain)

    def validate(self, sql: str, principal: Principal, params=None, metrics_used=None) -> ValidationResult:
        return self.validator.validate(sql, principal, params=params, metrics_used=metrics_used)

    def run_sql(self, sql: str, principal: Principal, params=None, metrics_used=None) -> ExecutionResult:
        v = self.validate(sql, principal, params, metrics_used)
        if not v.ok:
            return ExecutionResult(v.status, sql, error=v.user_message(), validation=v)
        return self.executor.execute(v)

    def run_spec(self, spec: QuerySpec, principal: Principal) -> ExecutionResult:
        """Semantic path: policy pre-check, compile WITHOUT scope, then the same validator injects scope.
        Raises AccessDenied when the pre-check fails (callers turn it into a REFUSED_ACCESS answer)."""
        auth = self.policy.authorize(self.model, principal, spec)
        if not auth.allowed:
            raise AccessDenied(auth)
        q = self.model.compile(spec)
        refs = [m.ref for m in q.metrics]
        res = self.run_sql(q.sql, principal, q.params, metrics_used=refs)
        if res.validation is not None:
            # the validator states the scope itself; keep the pre-check's other notes (e.g. out-of-scope asks)
            extra = [d for d in auth.disclosures if not d.startswith(("Results limited", "Transactions without"))]
            res.validation.disclosures = q.disclosures + extra + res.validation.disclosures
        return res

    def close(self):
        self.executor.close()
