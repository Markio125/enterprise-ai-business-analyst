"""SQL safety gate (SPEC section 10.1): every query derived from user input passes here.

The validator works on the sqlglot AST, never on regexes over SQL text. It:
  * rejects anything that is not exactly one read-only SELECT (S01-S04, S15)
  * enforces the caller's role on tables and on every resolved column, through CTEs,
    subqueries and SELECT * expansion (S05, S06)
  * asks for repairs on missing time filters, SELECT * on facts, cartesian joins (S07-S09)
  * auto-fixes LIMITs (S10) and injects row-level security into EVERY reference of a
    merchant-linked table by replacing it with a filtered subquery (S11)
  * estimates cost with DuckDB EXPLAIN (S12)
  * warns on unguarded division, metric definitions that differ from the semantic layer,
    and use of the raw (duplicated) table (S13, S14, S16)

Only a PASS result is sealed. The executor runs sealed SQL and nothing else.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from .policies import Policy, Principal, Role
from .semantic import SemanticModel, columns_in, ratio_parts

PASS = "PASS"
FAIL_UNSAFE = "FAIL_UNSAFE"
FAIL_ACCESS = "FAIL_ACCESS_DENIED"
FAIL_REPAIRABLE = "FAIL_REPAIRABLE"
NEEDS_NARROWING = "NEEDS_NARROWING"
_SEVERITY = [FAIL_UNSAFE, FAIL_ACCESS, FAIL_REPAIRABLE, NEEDS_NARROWING]

# Statement types that are never allowed anywhere in the tree (S03).
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in (
    "Insert", "Update", "Delete", "Merge", "Drop", "Alter", "Create", "Command", "Copy", "Pragma", "Set",
    "Use", "Attach", "Detach", "Install", "Transaction", "Commit", "Rollback", "LoadData", "Describe",
    "Summarize", "Show", "Grant", "Revoke", "TruncateTable", "Analyze", "Export", "Refresh", "Cache",
    "Uncache", "Into", "Kill", "Comment") if hasattr(exp, n))
_DANGEROUS_LEAD = re.compile(
    r"^\s*(insert|update|delete|merge|drop|alter|create|copy|attach|detach|pragma|set|reset|install|load|"
    r"export|import|call|vacuum|checkpoint|grant|revoke|truncate|begin|commit|rollback|use|describe|show|"
    r"summarize|force|unpivot\s+into)\b", re.I)
_SELECT_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_SEAL_KEY = secrets.token_bytes(32)
logging.getLogger("sqlglot").setLevel(logging.ERROR)   # "falling back to Command" is handled as UNSAFE


@dataclass
class Check:
    rule: str
    passed: bool
    detail: str = ""
    severity: str = "info"        # info | warn | auto_fix | one of the FAIL_* / NEEDS_NARROWING statuses


@dataclass
class ValidationResult:
    status: str
    original_sql: str
    principal: Principal
    checks: list[Check] = field(default_factory=list)
    rewritten_sql: str | None = None
    rls_predicates_applied: list[str] = field(default_factory=list)
    tables_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    disclosures: list[str] = field(default_factory=list)
    estimated_rows_scanned: int | None = None
    narrowing_options: list[str] = field(default_factory=list)
    seal: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == PASS

    def as_dict(self) -> dict:
        return {
            "status": self.status, "checks": [c.__dict__ for c in self.checks], "rewritten_sql": self.rewritten_sql,
            "rls_predicates_applied": self.rls_predicates_applied, "tables_used": self.tables_used,
            "errors": self.errors, "warnings": self.warnings, "disclosures": self.disclosures,
            "estimated_rows_scanned": self.estimated_rows_scanned, "narrowing_options": self.narrowing_options,
        }

    def user_message(self) -> str:
        if self.status == FAIL_UNSAFE:
            return "I can only run read-only analytical queries (a single SELECT), so this request was blocked."
        if self.status == FAIL_ACCESS:
            return "That query uses data outside your access: " + "; ".join(self.errors)
        if self.status == NEEDS_NARROWING:
            return " ".join(self.errors) + " Options: " + "; ".join(self.narrowing_options)
        return "The query needs fixing: " + "; ".join(self.errors)


def seal_for(sql: str, principal: Principal) -> str:
    return hmac.new(_SEAL_KEY, f"{principal.user_id}\x00{principal.role}\x00{sql}".encode(), hashlib.sha256).hexdigest()


def verify_seal(v: ValidationResult) -> bool:
    return bool(v.ok and v.seal and v.rewritten_sql is not None
                and hmac.compare_digest(v.seal, seal_for(v.rewritten_sql, v.principal)))


# --------------------------------------------------------------------------- parameter binding
def bind_params(sql: str, params: list | tuple | None) -> str:
    """Inline positional `?` parameters as properly escaped SQL literals (via sqlglot)."""
    if not params:
        return sql
    trees = sqlglot.parse(sql, read="duckdb")
    if len(trees) != 1:
        raise ValueError("bind_params expects one statement")
    tree = trees[0]
    # depth-first pre-order follows the text left to right (breadth-first does not: with nested
    # AND chains it would bind values to the wrong placeholders)
    holders = list(tree.find_all(exp.Placeholder, bfs=False))
    if all("line" in n.meta and "col" in n.meta for n in holders):
        holders.sort(key=lambda n: (n.meta["line"], n.meta["col"]))
    if len(holders) != len(params):
        raise ValueError(f"{len(holders)} placeholders but {len(params)} parameters")
    for node, value in zip(holders, params):
        node.replace(_literal(value))
    return tree.sql(dialect="duckdb")


def _literal(v) -> exp.Expression:
    if isinstance(v, bool):
        return exp.Boolean(this=v)
    if isinstance(v, datetime):
        return exp.cast(exp.Literal.string(v.isoformat(sep=" ")), "TIMESTAMP")
    if isinstance(v, date):
        return exp.cast(exp.Literal.string(v.isoformat()), "DATE")
    if v is None:
        return exp.Null()
    if isinstance(v, (int, float)):
        return exp.Literal.number(v)
    return exp.Literal.string(str(v))


# --------------------------------------------------------------------------- validator
class SqlValidator:
    def __init__(self, model: SemanticModel, policy: Policy, settings: dict, schema: dict[str, dict[str, str]],
                 explain=None):
        """`schema`: {table: {column: type}} of the warehouse. `explain(sql) -> plan json` for S12."""
        self.model, self.policy, self.settings = model, policy, settings
        self.schema = {t.lower(): {c.lower(): ty for c, ty in cols.items()} for t, cols in schema.items()}
        self.explain = explain
        self.cost = settings["cost_controls"]
        self.wh = settings["warehouse"]
        self.safety = settings["sql_safety"]
        self.denied_functions = {f.lower() for f in self.safety["denied_functions"]}
        self.facts = {t.lower(): [c.lower() for c in cols] for t, cols in self.cost["require_time_filter_on"].items()}
        # how each merchant-linked table reaches dim_merchant (for row-level security)
        self.merchant_links = {}
        for t, cols in self.schema.items():
            if t == "dim_merchant":
                self.merchant_links[t] = "self"
            elif "merchant_key" in cols:
                self.merchant_links[t] = "merchant_key"
            elif "merchant_id" in cols:
                self.merchant_links[t] = "merchant_id"

    # ---------------------------------------------------------------- public
    def validate(self, sql: str, principal: Principal, params=None, metrics_used: list[str] | None = None
                 ) -> ValidationResult:
        res = ValidationResult(status=PASS, original_sql=sql, principal=principal)
        role = self.policy.role_of(principal)

        def fail(rule, status, msg):
            res.checks.append(Check(rule, False, msg, status))
            res.errors.append(msg)

        def ok(rule, detail=""):
            res.checks.append(Check(rule, True, detail))

        try:
            sql_bound = bind_params(sql, params) if params else sql
        except (ValueError, ParseError) as e:
            fail("PARAMETERS", FAIL_REPAIRABLE, f"parameter binding failed: {e}")
            return self._finish(res)

        # S01 / S02 / S03 (parse level)
        try:
            trees = [t for t in sqlglot.parse(sql_bound, read="duckdb") if t is not None]
        except ParseError as e:
            if _DANGEROUS_LEAD.match(_strip_comments(sql_bound)):
                fail("S03_NO_DML_DDL", FAIL_UNSAFE, "statement type is not allowed")
            else:
                fail("S00_PARSES", FAIL_REPAIRABLE, f"SQL does not parse: {str(e).splitlines()[0]}")
            return self._finish(res)
        if len(trees) != 1:
            fail("S01_SINGLE_STATEMENT", FAIL_UNSAFE, f"expected exactly one statement, got {len(trees)}")
            return self._finish(res)
        ok("S01_SINGLE_STATEMENT")
        tree = trees[0]
        if not isinstance(tree, _SELECT_ROOTS):
            fail("S02_SELECT_ONLY", FAIL_UNSAFE, f"only SELECT queries are allowed (got {type(tree).__name__.upper()})")
            return self._finish(res)
        ok("S02_SELECT_ONLY")
        bad = [n for n in tree.walk() if isinstance(n, _FORBIDDEN_NODES)]
        if bad:
            fail("S03_NO_DML_DDL", FAIL_UNSAFE, f"forbidden construct: {type(bad[0]).__name__.upper()}")
            return self._finish(res)
        ok("S03_NO_DML_DDL")
        if any(isinstance(n, exp.Placeholder) for n in tree.walk()):
            fail("PARAMETERS", FAIL_REPAIRABLE, "query has unbound ? placeholders")
            return self._finish(res)

        # S04 file / URL / catalog functions
        funcs = self._function_names(tree)
        denied = sorted(f for f in funcs if f in self.denied_functions or f.startswith(("pragma_", "duckdb_")))
        file_tables = [t.name for t in tree.find_all(exp.Table)
                       if isinstance(t.this, exp.Identifier) and t.this.quoted and re.search(r"[./\\:]", t.name)]
        if denied or file_tables:
            fail("S04_NO_EXTERNAL_ACCESS", FAIL_UNSAFE,
                 f"external/system access is not allowed: {', '.join(denied + file_tables)}")
            return self._finish(res)
        ok("S04_NO_EXTERNAL_ACCESS")

        # S05 tables
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        shadow = sorted(cte_names & set(self.schema))
        if shadow:
            fail("S05_TABLE_ALLOWLIST", FAIL_UNSAFE, f"CTE name(s) shadow real tables: {', '.join(shadow)}")
            return self._finish(res)
        if any(w.args.get("recursive") for w in tree.find_all(exp.With)):
            fail("S05_TABLE_ALLOWLIST", FAIL_REPAIRABLE, "recursive CTEs are not supported")
            return self._finish(res)
        tables, problems_unsafe, problems_access, problems_repair = [], [], [], []
        for t in tree.find_all(exp.Table):
            name = t.name.lower()
            if not name:
                problems_unsafe.append(f"unsupported table expression {t.sql(dialect='duckdb')}")
                continue
            if (t.db or "").lower() in ("information_schema", "pg_catalog"):
                problems_access.append(f"system catalog {t.db}.{name} is not queryable")
                continue
            if t.args.get("catalog") or (t.args.get("db") and t.db.lower() != "main"):
                problems_unsafe.append(f"cross-database reference {t.sql(dialect='duckdb')}")
                continue
            if name in cte_names and not t.args.get("db"):
                continue
            if name in {x.lower() for x in self.safety.get("internal_tables", [])} or name.startswith(
                    ("information_schema", "pg_", "sqlite_", "duckdb_")) or (t.db or "").lower() == "information_schema":
                problems_access.append(f"system table {name} is not queryable")
                continue
            if name not in self.schema:
                problems_repair.append(f"unknown table {name}")
                continue
            tables.append(name)
            if not role.table_allowed(name):
                problems_access.append(f"role {role.name} has no access to table {name}")
        res.tables_used = sorted(set(tables))
        if problems_unsafe:
            fail("S05_TABLE_ALLOWLIST", FAIL_UNSAFE, "; ".join(problems_unsafe))
            return self._finish(res)
        if problems_access:
            fail("S05_TABLE_ALLOWLIST", FAIL_ACCESS, "; ".join(sorted(set(problems_access))))
            return self._finish(res)
        if problems_repair:
            fail("S05_TABLE_ALLOWLIST", FAIL_REPAIRABLE,
                 "; ".join(problems_repair) + f". Available: {', '.join(sorted(t for t in self.schema if role.table_allowed(t)))}")
            return self._finish(res)
        ok("S05_TABLE_ALLOWLIST", ", ".join(res.tables_used))

        # S08 SELECT * on facts (checked on the original tree, before star expansion)
        star_facts = self._star_on_facts(tree)

        # S06 columns (resolve every column to its base table through all scopes)
        try:
            qualified = qualify(tree.copy(), schema=self.schema, dialect="duckdb", validate_qualify_columns=True,
                                quote_identifiers=False, identify=False)
        except (OptimizeError, ParseError) as e:
            fail("S06_COLUMN_ALLOWLIST", FAIL_REPAIRABLE, f"cannot resolve columns: {e}")
            return self._finish(res)
        denied_cols = self._denied_columns(role)
        refs = self._base_column_refs(qualified)
        hits = sorted({f"{t}.{c}" for t, c in refs
                       if not role.column_allowed(t, c) or (t, c) in denied_cols})
        if hits:
            fail("S06_COLUMN_ALLOWLIST", FAIL_ACCESS, f"role {role.name} cannot read column(s) {', '.join(hits)}")
            return self._finish(res)
        ok("S06_COLUMN_ALLOWLIST", f"{len(refs)} column references")

        if star_facts:
            fail("S08_NO_SELECT_STAR_ON_FACTS", FAIL_REPAIRABLE,
                 f"SELECT * on fact table(s) {', '.join(star_facts)}: list the columns you need")
        else:
            ok("S08_NO_SELECT_STAR_ON_FACTS")

        # S07 time filter + window
        self._check_time(tree, res, fail, ok)
        # S09 joins
        joins = list(tree.find_all(exp.Join))
        cartesian = [j for j in joins if self._is_cartesian(j)]
        if len(joins) > self.cost["max_joins"]:
            fail("S09_JOINS", FAIL_REPAIRABLE, f"{len(joins)} joins exceeds the limit of {self.cost['max_joins']}")
        elif cartesian and self.cost.get("forbid_cross_join", True):
            fail("S09_JOINS", FAIL_REPAIRABLE, "cross join / join without a condition is not allowed: "
                                              + ", ".join(j.this.sql(dialect='duckdb') for j in cartesian))
        else:
            ok("S09_JOINS", f"{len(joins)} join(s)")

        # S13 / S14 / S16 warnings
        self._warnings(tree, res, metrics_used)

        # warnings (S13/S14/S16) never stop validation: RLS and the cost check must still run
        if any(not c.passed and c.severity != "warn" for c in res.checks):
            return self._finish(res)

        # S10 LIMIT (auto-fix) and S11 RLS (auto-fix) on the original tree
        rewritten = tree.copy()
        self._apply_limit(rewritten, role, res)
        try:
            self._apply_rls(rewritten, principal, role, res)
        except PermissionError as e:
            fail("S11_ROW_LEVEL_SECURITY", FAIL_ACCESS, str(e))
            return self._finish(res)
        res.rewritten_sql = rewritten.sql(dialect="duckdb")

        # S12 cost
        if self.explain is not None:
            self._check_cost(res, fail, ok)
        return self._finish(res)

    # ---------------------------------------------------------------- helpers
    def _finish(self, res: ValidationResult) -> ValidationResult:
        failed = {c.severity for c in res.checks if not c.passed}
        res.status = next((s for s in _SEVERITY if s in failed), PASS)
        if res.ok and res.rewritten_sql is None:      # never seal a query that skipped the rewrite
            res.status = FAIL_REPAIRABLE
            res.errors.append("internal: validation did not complete")
        if res.ok:
            res.seal = seal_for(res.rewritten_sql, res.principal)
        else:
            res.rewritten_sql = res.rewritten_sql if res.status == NEEDS_NARROWING else None
        return res

    @staticmethod
    def _function_names(tree: exp.Expression) -> set[str]:
        names = set()
        for f in tree.find_all(exp.Func):
            if isinstance(f, exp.Anonymous):
                names.add(str(f.this).lower())
            else:
                names.add(f.sql_name().lower())
                if isinstance(f, (exp.ReadCSV,)) or type(f).__name__.lower().startswith("read"):
                    names.add("read_csv" if isinstance(f, exp.ReadCSV) else type(f).__name__.lower())
        for t in tree.find_all(exp.Table):
            if isinstance(t.this, exp.Func):
                fn = t.this
                names.add(str(fn.this).lower() if isinstance(fn, exp.Anonymous) else fn.sql_name().lower())
        return names

    def _star_on_facts(self, tree) -> list[str]:
        out = []
        for sel in tree.find_all(exp.Select):
            stars = [e for e in sel.expressions if isinstance(e, exp.Star) or
                     (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))]
            if not stars:
                continue
            src = [t.name.lower() for t in sel.find_all(exp.Table) if t.find_ancestor(exp.Select) is sel]
            out += [t for t in src if t in self.facts or t.startswith("fact_")]
        return sorted(set(out))

    def _base_column_refs(self, qualified) -> set[tuple[str, str]]:
        refs = set()
        for scope in traverse_scope(qualified):
            for col in scope.columns:
                src = scope.sources.get(col.table)
                if isinstance(src, exp.Table):
                    refs.add((src.name.lower(), col.name.lower()))
        return refs

    def _denied_columns(self, role: Role) -> set[tuple[str, str]]:
        """Columns implied by denied dimensions: the dimension's own column and the fact-side join keys."""
        out = set()
        for dim in role.deny_dimensions:
            d = self.model.dimensions.get(dim)
            if not d:
                continue
            for ent in self.model.entities.values():
                for alias, col in columns_in(d.expr):
                    j = ent.joins.get(alias)
                    if j is None:
                        continue
                    out.add((j.table.lower(), col.lower()))
                    for a2, c2 in columns_in(j.on):
                        if a2 == ent.name:
                            out.add((ent.table.lower(), c2.lower()))
        return out

    def _check_time(self, tree, res, fail, ok):
        facts_used = [t for t in res.tables_used if t in self.facts]
        if not facts_used:
            ok("S07_TIME_FILTER", "no large fact tables")
            return
        preds = []
        for node in tree.walk():
            if isinstance(node, exp.Where) or (isinstance(node, exp.Join) and node.args.get("on")):
                preds.append(node if isinstance(node, exp.Where) else node.args["on"])
        filtered_cols = {c.name.lower() for p in preds for c in p.find_all(exp.Column)}
        missing = [t for t in facts_used if not filtered_cols & set(self.facts[t])]
        if missing:
            fail("S07_TIME_FILTER", FAIL_REPAIRABLE,
                 f"add a time filter on {', '.join(missing)} (e.g. date_key BETWEEN 20261001 AND 20261027)")
            return
        span = _date_key_span(preds)
        if span is not None and span > self.cost["max_time_window_days"]:
            res.narrowing_options.append(f"limit the window to {self.cost['max_time_window_days']} days or fewer")
            fail("S07_TIME_FILTER", NEEDS_NARROWING, f"time window of {span} days exceeds the "
                                                     f"{self.cost['max_time_window_days']}-day limit.")
            return
        ok("S07_TIME_FILTER", f"window {span} days" if span else "time filter present")

    @staticmethod
    def _is_cartesian(j: exp.Join) -> bool:
        kind = (j.args.get("kind") or "").upper()
        method = (j.args.get("method") or "").upper()
        if kind == "CROSS" or method == "POSITIONAL":
            return True
        if isinstance(j.this, (exp.Unnest, exp.Lateral)):
            return False
        if j.args.get("using"):
            return False
        on = j.args.get("on")
        # no condition, a constant condition (ON TRUE / ON 1=1), or one that compares no columns
        return on is None or not list(on.find_all(exp.Column))

    def _warnings(self, tree, res, metrics_used):
        # S13: division not guarded
        unguarded = []
        for d in tree.find_all(exp.Div):
            r = d.expression
            while isinstance(r, exp.Paren):
                r = r.this
            if isinstance(r, exp.Literal) and r.is_number and float(r.this) != 0:
                continue
            if isinstance(r, (exp.Nullif, exp.Coalesce)) or r.find(exp.Nullif):
                continue
            unguarded.append(d.sql(dialect="duckdb")[:80])
        if unguarded:
            res.checks.append(Check("S13_DIVISION_GUARDED", False, "; ".join(unguarded), "warn"))
            res.warnings.append("division without NULLIF guard: " + unguarded[0])
        else:
            res.checks.append(Check("S13_DIVISION_GUARDED", True))
        # S14: metric definitions match the semantic layer
        if metrics_used:
            canon_sql = _canonical(tree)
            diffs = []
            for ref in metrics_used:
                try:
                    r = self.model.resolve_metric(ref)
                except Exception:  # noqa: BLE001
                    diffs.append(f"{ref} (unknown metric)")
                    continue
                ent = self.model.entities[r.entity]
                tmpl = ratio_parts(r.variant.sql)[r.part] if r.part else r.variant.sql
                needed = re.findall(r"\{(\w+)\}", tmpl)
                frag = [_canonical(sqlglot.parse_one(ent.measures[m], read="duckdb")) for m in needed]
                missing = [m for m, f in zip(needed, frag) if f not in canon_sql]
                if missing:
                    diffs.append(f"{ref} (measures {', '.join(missing)} not found as defined)")
            if diffs:
                res.checks.append(Check("S14_METRIC_DEFINITIONS", False, "; ".join(diffs), "warn"))
                res.warnings.append("metric SQL differs from the semantic layer: " + "; ".join(diffs))
                res.disclosures.append("Some metrics were computed with SQL that differs from the standard "
                                       "definitions: " + "; ".join(diffs))
            else:
                res.checks.append(Check("S14_METRIC_DEFINITIONS", True))
        # S16: raw table with a curated view
        for t in res.tables_used:
            view = self.safety.get("prefer_views", {}).get(t)
            if view:
                res.checks.append(Check("S16_PREFER_CURATED_VIEW", False, f"{t} -> {view}", "warn"))
                res.warnings.append(f"{t} contains replayed duplicates; {view} is the deduplicated view")

    def _apply_limit(self, tree, role: Role, res):
        top = tree
        aggregated = isinstance(top, exp.Select) and (
            top.args.get("group") is not None or
            (top.expressions and all(e.find(exp.AggFunc) for e in top.expressions)))
        cap = self.wh["max_result_rows"]
        if not role.allow_row_level_output and not aggregated:
            cap = min(cap, self.policy.defaults.get("max_row_level_rows", 50))
            res.disclosures.append(f"Your role sees aggregated data; row-level output is capped at {cap} rows.")
        limit = top.args.get("limit")
        current = None
        if limit is not None:
            lit = limit.expression if isinstance(limit, exp.Limit) else None
            if isinstance(lit, exp.Literal) and lit.is_int:
                current = int(lit.this)
        if limit is None and not aggregated:
            n = min(self.wh["default_limit"], cap)
            top.limit(n, copy=False)
            res.checks.append(Check("S10_LIMIT", True, f"injected LIMIT {n}", "auto_fix"))
        elif current is not None and current > cap:
            top.limit(cap, copy=False)
            res.checks.append(Check("S10_LIMIT", True, f"LIMIT {current} reduced to {cap}", "auto_fix"))
        elif limit is not None and current is None:
            top.limit(cap, copy=False)
            res.checks.append(Check("S10_LIMIT", True, f"non-literal LIMIT replaced with {cap}", "auto_fix"))
        else:
            res.checks.append(Check("S10_LIMIT", True, "aggregated" if aggregated else f"LIMIT {current}"))

    def _apply_rls(self, tree, principal: Principal, role: Role, res):
        if not role.row_filters:
            res.checks.append(Check("S11_ROW_LEVEL_SECURITY", True, "no row-level scope for this role"))
            return
        preds = []          # (dim_merchant column, value)
        for rf in role.row_filters:
            d = self.model.dimensions.get(rf["dimension"])
            aliases = columns_in(d.expr) if d else []
            if not d or len(aliases) != 1 or aliases[0][0] != "merchant":
                raise PermissionError(f"row-level scope on {rf['dimension']} cannot be enforced on free-form SQL")
            if "value_from" in rf:
                val = principal.attr(rf["value_from"].removeprefix("user."))
                if val in (None, ""):
                    raise PermissionError(f"user {principal.user_id} lacks attribute {rf['value_from']}")
            else:
                val = rf["value"]
            if rf.get("op", "=") not in ("=", "in"):
                raise PermissionError(f"unsupported row filter operator {rf.get('op')}")
            preds.append((aliases[0][1], val))

        def merchant_where(current_only: bool) -> exp.Expression:
            conds = [exp.column("is_current")] if current_only else []
            for col, val in preds:
                if isinstance(val, (list, tuple)):
                    conds.append(exp.column(col).isin(*[_literal(v) for v in val]))
                else:
                    conds.append(exp.EQ(this=exp.column(col), expression=_literal(val)))
            return exp.and_(*conds)

        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        targets = [t for t in tree.find_all(exp.Table)
                   if t.name.lower() in self.merchant_links and not (t.name.lower() in cte_names and not t.args.get("db"))]
        applied = set()
        for t in targets:
            name = t.name.lower()
            link = self.merchant_links[name]
            # schema-qualified so a user CTE can never stand in for the real tables
            base, dm = exp.table_(name, db="main"), exp.table_("dim_merchant", db="main")
            if link == "self":
                inner = exp.select("*").from_(base).where(merchant_where(False))
            elif link == "merchant_key":
                sub = exp.select("merchant_key").from_(dm).where(merchant_where(False))
                inner = exp.select("*").from_(base).where(exp.column("merchant_key").isin(query=sub))
            else:
                sub = exp.select("merchant_id").from_(dm).where(merchant_where(True))
                inner = exp.select("*").from_(base).where(exp.column("merchant_id").isin(query=sub))
            alias = t.alias or t.name
            t.replace(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias))))
            applied.add(name)
        desc = " AND ".join(f"dim_merchant.{c} = {v!r}" for c, v in preds)
        res.rls_predicates_applied = [f"{t}: via {self.merchant_links[t]} -> {desc}" for t in sorted(applied)]
        res.checks.append(Check("S11_ROW_LEVEL_SECURITY", True,
                                f"scope injected into {len(targets)} table reference(s)" if targets
                                else "no merchant-linked tables referenced", "auto_fix" if targets else "info"))
        if targets:
            res.disclosures.append("Results limited to your access scope: "
                                   + ", ".join(f"{rf['dimension']} = {v}" for rf, (_, v) in zip(role.row_filters, preds))
                                   + ". Transactions without a mapped merchant are excluded.")

    def _check_cost(self, res, fail, ok):
        try:
            plan = self.explain(res.rewritten_sql)
        except Exception as e:  # noqa: BLE001
            fail("S12_COST_ESTIMATE", FAIL_REPAIRABLE, f"query does not plan: {str(e).splitlines()[0]}")
            return
        scans, biggest = _plan_estimates(plan)
        res.estimated_rows_scanned = scans
        if scans > self.cost["max_scanned_rows_estimate"] or biggest > self.cost["max_intermediate_rows_estimate"]:
            res.narrowing_options += ["narrow the time window (e.g. last 30 days)",
                                      "use a coarser grain (weekly or monthly instead of daily or hourly)",
                                      "filter to a merchant segment, region or payment method"]
            msg = (f"This query would process about {max(scans, biggest):,} rows, over the limit of "
                   f"{self.cost['max_scanned_rows_estimate']:,}.")
            fail("S12_COST_ESTIMATE", FAIL_UNSAFE if self.cost.get("on_exceed") == "REJECT" else NEEDS_NARROWING, msg)
        else:
            ok("S12_COST_ESTIMATE", f"~{scans:,} rows scanned, largest operator ~{biggest:,}")


# --------------------------------------------------------------------------- module helpers
def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def _canonical(node: exp.Expression) -> str:
    """Normalised SQL with table qualifiers removed, for comparing metric fragments."""
    n = node.copy()
    for c in n.find_all(exp.Column):
        c.set("table", None)
    return re.sub(r"\s+", "", n.sql(dialect="duckdb").lower())


def _date_key_span(preds) -> int | None:
    lo, hi = None, None
    for p in preds:
        for b in p.find_all(exp.Between):
            if isinstance(b.this, exp.Column) and "date_key" in b.this.name.lower():
                a, z = _int(b.args.get("low")), _int(b.args.get("high"))
                lo = a if a and (lo is None or a < lo) else lo
                hi = z if z and (hi is None or z > hi) else hi
        for cmp_ in list(p.find_all(exp.GTE)) + list(p.find_all(exp.GT)) + list(p.find_all(exp.LTE)) + list(p.find_all(exp.LT)):
            if isinstance(cmp_.this, exp.Column) and "date_key" in cmp_.this.name.lower():
                v = _int(cmp_.expression)
                if v is None:
                    continue
                if isinstance(cmp_, (exp.GTE, exp.GT)):
                    lo = v if lo is None or v < lo else lo
                else:
                    hi = v if hi is None or v > hi else hi
    if lo is None or hi is None:
        return None
    try:
        a = date(lo // 10000, lo // 100 % 100, lo % 100)
        z = date(hi // 10000, hi // 100 % 100, hi % 100)
    except ValueError:
        return None
    return (z - a).days + 1


def _int(node) -> int | None:
    if isinstance(node, exp.Literal) and node.is_int:
        v = int(node.this)
        return v if 19000101 <= v <= 30001231 else None
    return None


def _plan_estimates(plan) -> tuple[int, int]:
    """(sum of table-scan estimates, largest operator estimate) from DuckDB EXPLAIN (FORMAT JSON)."""
    nodes = json.loads(plan) if isinstance(plan, str) else plan
    scans, biggest = 0, 0

    def card(n):
        info = n.get("extra_info") or {}
        v = info.get("Estimated Cardinality") if isinstance(info, dict) else None
        try:
            return int(str(v).replace("~", "").strip()) if v is not None else 0
        except ValueError:
            return 0

    def walk(n):
        nonlocal scans, biggest
        c = card(n)
        biggest = max(biggest, c)
        if "SCAN" in str(n.get("name", "")).upper():
            scans += c
        for ch in n.get("children", []):
            walk(ch)
    for n in (nodes if isinstance(nodes, list) else [nodes]):
        walk(n)
    return scans, biggest
