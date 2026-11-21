"""Semantic layer: load semantic/model.yaml, validate it against the warehouse,
resolve business terms, and compile metric requests into parameterised SQL.

The compiler is deterministic. It is the "known-good" path used by the
analysis engine, and the reference the LLM-generated SQL is compared against.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = ROOT / "semantic" / "model.yaml"

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
FILTER_OPS = {"=", "!=", "<>", ">", ">=", "<", "<=", "in", "not in", "between", "is null", "is not null", "like"}


class SemanticError(ValueError):
    """Raised for invalid model definitions or unresolvable requests."""


def columns_in(sql: str) -> list[tuple[str, str]]:
    """(alias, column) pairs referenced by a SQL expression. Unqualified columns get alias ''."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    return [(c.table, c.name) for c in tree.find_all(exp.Column)]


def aliases_in(sql: str) -> set[str]:
    return {a for a, _ in columns_in(sql) if a}


# --------------------------------------------------------------------------- model objects
@dataclass(frozen=True)
class TimeColumn:
    name: str
    expr: str
    type: str          # date_key | date
    description: str = ""


@dataclass(frozen=True)
class Join:
    name: str
    table: str
    on: str
    requires: tuple[str, ...] = ()
    type: str = "left"
    description: str = ""


@dataclass
class Entity:
    name: str
    table: str
    grain: str
    description: str
    time_columns: dict[str, TimeColumn]
    default_time: str
    time_ambiguous: bool
    joins: dict[str, Join]
    measures: dict[str, str]

    def tables_for(self, aliases: set[str]) -> dict[str, str]:
        out = {self.name: self.table}
        out.update({a: self.joins[a].table for a in aliases if a in self.joins})
        return out


@dataclass(frozen=True)
class MetricVariant:
    name: str
    entity: str
    sql: str
    description: str = ""
    synonyms: tuple[str, ...] = ()


@dataclass
class Metric:
    name: str
    description: str
    variants: dict[str, MetricVariant]
    default_variant: str
    format: str = "decimal"
    better: str | None = None
    synonyms: tuple[str, ...] = ()
    restricted_to: tuple[str, ...] = ()
    ambiguity_flag: bool = False
    complement_of: str | None = None

    @property
    def has_variants(self) -> bool:
        return len(self.variants) > 1


@dataclass(frozen=True)
class Dimension:
    name: str
    expr: str
    synonyms: tuple[str, ...] = ()
    description: str = ""
    high_cardinality: bool = False
    time_grain: str | None = None


@dataclass(frozen=True)
class ResolvedMetric:
    """A metric bound to one variant, with measures expanded to SQL."""
    metric: Metric
    variant: MetricVariant
    sql: str

    @property
    def ref(self) -> str:
        return self.metric.name if not self.metric.has_variants else f"{self.metric.name}:{self.variant.name}"

    @property
    def entity(self) -> str:
        return self.variant.entity

    @property
    def disclosure(self) -> str | None:
        if not self.metric.has_variants:
            return None
        return f"{self.metric.name} = {self.variant.name}: {self.variant.description}"


@dataclass(frozen=True)
class Filter:
    dimension: str
    op: str
    value: Any = None

    def __post_init__(self):
        if self.op.lower() not in FILTER_OPS:
            raise SemanticError(f"unsupported filter operator {self.op!r}")


@dataclass
class QuerySpec:
    metrics: list[str]
    dimensions: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    time_range: tuple[date, date] | None = None
    time_basis: str | None = None              # which time column; entity default when None
    order_by: list[tuple[str, bool]] = field(default_factory=list)   # (name, descending)
    limit: int | None = None


@dataclass
class CompiledQuery:
    sql: str
    params: list
    entity: str
    metrics: list[ResolvedMetric]
    dimensions: list[Dimension]
    joins: list[str]
    tables: dict[str, str]            # alias -> physical table (incl. system-applied scope joins)
    disclosures: list[str]

    def run(self, con):
        return con.execute(self.sql, self.params).df()


# --------------------------------------------------------------------------- the model
class SemanticModel:
    def __init__(self, raw: dict, path: Path | None = None):
        self.raw = raw
        self.path = path
        self.timezone = raw.get("timezone", "Asia/Kolkata")
        self.as_of_cfg = raw.get("as_of", {}) or {}
        self.entities = {n: self._entity(n, e) for n, e in raw["entities"].items()}
        self.metrics: dict[str, Metric] = {}
        for n, m in raw["metrics"].items():
            if "complement_of" not in m:
                self.metrics[n] = self._metric(n, m)
        for n, m in raw["metrics"].items():      # complements after their base
            if "complement_of" in m:
                self.metrics[n] = self._complement(n, m)
        self.dimensions = {n: Dimension(name=n, expr=d["expr"], synonyms=tuple(d.get("synonyms", [])),
                                        description=d.get("description", ""),
                                        high_cardinality=d.get("high_cardinality", False),
                                        time_grain=d.get("time_grain"))
                           for n, d in raw["dimensions"].items()}
        self.ambiguous_terms: dict[str, dict] = raw.get("ambiguous_terms", {}) or {}
        self.time_expressions: dict = raw.get("time_expressions", {}) or {}
        self.glossary: dict[str, str] = raw.get("glossary", {}) or {}
        self._dim_aliases = {n: aliases_in(d.expr) for n, d in self.dimensions.items()}

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: Path | str = DEFAULT_MODEL) -> "SemanticModel":
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f), path)

    @staticmethod
    def _entity(name: str, e: dict) -> Entity:
        t = e["time"]
        joins = {}
        for jn, j in e.get("joins", {}).items():
            if True in j:                      # YAML 1.1 reads a bare `on:` key as boolean True
                j = {("on" if k is True else k): v for k, v in j.items()}
            deps = aliases_in(j["on"]) - {jn, name}
            joins[jn] = Join(name=jn, table=j["table"], on=j["on"],
                             requires=tuple(sorted(deps | set(j.get("requires", [])))),
                             type=j.get("type", "left"), description=j.get("description", ""))
        return Entity(
            name=name, table=e["table"], grain=e.get("grain", ""), description=e.get("description", "").strip(),
            time_columns={cn: TimeColumn(cn, c["expr"], c["type"], c.get("description", ""))
                          for cn, c in t["columns"].items()},
            default_time=t["default"], time_ambiguous=bool(t.get("ambiguous", False)),
            joins=joins, measures={k: str(v) for k, v in (e.get("measures") or {}).items()})

    @staticmethod
    def _metric(name: str, m: dict) -> Metric:
        if "variants" in m:
            variants = {vn: MetricVariant(vn, v["entity"], v["sql"], v.get("description", ""),
                                          tuple(v.get("synonyms", [])))
                        for vn, v in m["variants"].items()}
            default = m["default_variant"]
        else:
            variants = {"default": MetricVariant("default", m["entity"], m["sql"], m.get("description", ""))}
            default = "default"
        return Metric(name=name, description=str(m.get("description", "")).strip(), variants=variants,
                      default_variant=default, format=m.get("format", "decimal"), better=m.get("better"),
                      synonyms=tuple(m.get("synonyms", [])), restricted_to=tuple(m.get("restricted_to", [])),
                      ambiguity_flag=bool(m.get("ambiguity_flag", False)))

    def _complement(self, name: str, m: dict) -> Metric:
        base = self.metrics.get(m["complement_of"])
        if base is None:
            raise SemanticError(f"{name}: complement_of unknown metric {m['complement_of']!r}")
        variants = {vn: MetricVariant(vn, v.entity, f"1 - ({v.sql})", f"1 - {v.description}" if v.description else "")
                    for vn, v in base.variants.items()}
        return Metric(name=name, description=str(m.get("description", "")).strip(), variants=variants,
                      default_variant=base.default_variant, format=m.get("format", base.format),
                      better=m.get("better"), synonyms=tuple(m.get("synonyms", [])),
                      restricted_to=tuple(m.get("restricted_to", base.restricted_to)),
                      ambiguity_flag=base.ambiguity_flag, complement_of=base.name)

    # ---------------------------------------------------------------- resolution
    def resolve_metric(self, ref: str) -> ResolvedMetric:
        name, _, variant = ref.partition(":")
        m = self.metrics.get(name)
        if m is None:
            raise SemanticError(f"unknown metric {name!r}")
        vname = variant or m.default_variant
        if vname not in m.variants:
            raise SemanticError(f"metric {name!r} has no variant {vname!r} (have: {', '.join(m.variants)})")
        v = m.variants[vname]
        ent = self.entities[v.entity]

        def sub(match):
            key = match.group(1)
            if key not in ent.measures:
                raise SemanticError(f"metric {ref!r}: measure {key!r} not defined on entity {ent.name!r}")
            return f"({ent.measures[key]})"
        return ResolvedMetric(m, v, _PLACEHOLDER.sub(sub, v.sql))

    def dimension(self, name: str) -> Dimension:
        if name not in self.dimensions:
            raise SemanticError(f"unknown dimension {name!r}")
        return self.dimensions[name]

    def dimension_available(self, dim: str, entity: str) -> bool:
        ent = self.entities[entity]
        return all(a == entity or a in ent.joins for a in self._dim_aliases[dim])

    def dimensions_for(self, entity: str) -> list[str]:
        return [d for d in self.dimensions if self.dimension_available(d, entity)]

    def metric_variants(self) -> list[str]:
        """Every metric reference, e.g. 'gmv_inr', 'success_rate:order_level'."""
        out = []
        for m in self.metrics.values():
            out += [m.name] if not m.has_variants else [f"{m.name}:{v}" for v in m.variants]
        return out

    def lookup(self, term: str) -> list[dict]:
        """Map a business word to semantic objects. More than one hit means it is ambiguous."""
        t = term.strip().lower().replace("_", " ")
        if t in self.ambiguous_terms:
            a = self.ambiguous_terms[t]
            return [{"kind": "ambiguous", "term": t, "candidates": a["candidates"], "default": a.get("default"),
                     "note": a.get("note", "")}]
        hits = []
        for m in self.metrics.values():
            if t in {m.name.replace("_", " "), *map(str.lower, m.synonyms)}:
                hits.append({"kind": "metric", "name": m.name})
            for v in m.variants.values():
                if t in map(str.lower, v.synonyms):
                    hits.append({"kind": "metric", "name": f"{m.name}:{v.name}"})
        for d in self.dimensions.values():
            if t in {d.name.replace("_", " "), *map(str.lower, d.synonyms)}:
                hits.append({"kind": "dimension", "name": d.name})
        return hits

    # ---------------------------------------------------------------- compilation
    def compile(self, spec: QuerySpec, scope: list[Filter] | None = None,
                scope_disclosures: list[str] | None = None) -> CompiledQuery:
        """Compile a metric request. `scope` filters are system-applied (row-level security)."""
        if not spec.metrics:
            raise SemanticError("at least one metric is required")
        resolved = [self.resolve_metric(r) for r in spec.metrics]
        entities = {r.entity for r in resolved}
        if len(entities) > 1:
            raise SemanticError(f"metrics come from different entities {sorted(entities)}; query them separately")
        ent = self.entities[entities.pop()]
        dims = [self.dimension(d) for d in spec.dimensions]
        for d in dims:
            if not self.dimension_available(d.name, ent.name):
                raise SemanticError(f"dimension {d.name!r} is not available for {ent.name} metrics")

        basis = spec.time_basis or ent.default_time
        if basis not in ent.time_columns:
            raise SemanticError(f"entity {ent.name!r} has no time basis {basis!r} (have: {', '.join(ent.time_columns)})")
        tcol = ent.time_columns[basis]

        params: list = []
        where: list[str] = []
        if spec.time_range:
            lo, hi = spec.time_range
            if lo > hi:
                raise SemanticError(f"time range start {lo} is after end {hi}")
            where.append(f"{tcol.expr} BETWEEN ? AND ?")
            params += ([int(lo.strftime('%Y%m%d')), int(hi.strftime('%Y%m%d'))] if tcol.type == "date_key" else [lo, hi])
        for f in list(spec.filters) + list(scope or []):
            if f.dimension not in self.dimensions:
                raise SemanticError(f"filter on unknown dimension {f.dimension!r}")
            if not self.dimension_available(f.dimension, ent.name):
                raise SemanticError(f"cannot filter {ent.name} by {f.dimension!r}")
            sql, p = _filter_sql(self.dimensions[f.dimension].expr, f)
            where.append(sql)
            params += p

        # Aliases referenced anywhere -> joins needed (closure over join dependencies)
        refs = set()
        for r in resolved:
            refs |= aliases_in(r.sql)
        for d in dims:
            refs |= self._dim_aliases[d.name]
        refs |= aliases_in(tcol.expr)
        for f in spec.filters:
            refs |= self._dim_aliases[f.dimension]
        user_refs = set(refs)                         # what the user asked for (scope joins excluded)
        for f in scope or []:
            refs |= self._dim_aliases[f.dimension]
        joins = _join_order(ent, refs - {ent.name})

        select = [f"{d.expr} AS {d.name}" for d in dims] + [f"{r.sql} AS {_col_name(r)}" for r in resolved]
        sql = [f"SELECT {', '.join(select)}", f"FROM {ent.table} AS {ent.name}"]
        for jn in joins:
            j = ent.joins[jn]
            sql.append(f"{'LEFT JOIN' if j.type == 'left' else 'JOIN'} {j.table} AS {jn} ON {j.on}")
        if where:
            sql.append("WHERE " + "\n  AND ".join(where))
        if dims:
            sql.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims))))
        out_cols = {d.name for d in dims} | {_col_name(r) for r in resolved}
        if spec.order_by:
            parts = []
            by_base = {r.metric.name: _col_name(r) for r in resolved} | {r.ref: _col_name(r) for r in resolved}
            for name, desc in spec.order_by:
                name = by_base.get(name, name)
                if name not in out_cols:
                    raise SemanticError(f"cannot order by {name!r}; not in the output")
                parts.append(f"{name} {'DESC' if desc else 'ASC'} NULLS LAST")
            sql.append("ORDER BY " + ", ".join(parts))
        elif dims:
            sql.append("ORDER BY " + ", ".join(str(i + 1) for i in range(len(dims))))
        if spec.limit is not None:
            sql.append(f"LIMIT {int(spec.limit)}")

        disclosures = [r.disclosure for r in resolved if r.disclosure]
        if ent.time_ambiguous and spec.time_range and spec.time_basis is None:
            disclosures.append(f"Time filter uses {ent.name} {basis} ({tcol.description}). "
                               f"Other bases: {', '.join(c for c in ent.time_columns if c != basis)}.")
        if "merchant" in user_refs and "merchant_current" not in user_refs and ent.name != "finance_summary":
            disclosures.append("Merchant attributes are point-in-time (as of each transaction).")
        disclosures += scope_disclosures or []
        return CompiledQuery(sql="\n".join(sql), params=params, entity=ent.name, metrics=resolved, dimensions=dims,
                             joins=joins, tables=ent.tables_for(set(joins)), disclosures=disclosures)

    # ---------------------------------------------------------------- validation
    def validate(self, con=None) -> list[str]:
        """Return a list of problems (empty = valid). With a connection, also checks the warehouse."""
        problems: list[str] = []
        cols: dict[str, set[str]] | None = None
        if con is not None:
            rows = con.execute("SELECT table_name, column_name FROM information_schema.columns").fetchall()
            cols = {}
            for t, c in rows:
                cols.setdefault(t, set()).add(c)

        def check_expr(where: str, ent: Entity, sql: str, allowed: set[str]):
            try:
                refs = columns_in(sql)
            except sqlglot.errors.ParseError as e:
                problems.append(f"{where}: cannot parse SQL ({e})")
                return
            for alias, col in refs:
                if not alias:
                    problems.append(f"{where}: unqualified column {col!r}")
                    continue
                if alias not in allowed:
                    problems.append(f"{where}: alias {alias!r} is not the entity or one of its joins")
                    continue
                table = ent.table if alias == ent.name else ent.joins[alias].table
                if cols is not None and col not in cols.get(table, set()):
                    problems.append(f"{where}: column {table}.{col} does not exist")

        for en, ent in self.entities.items():
            if cols is not None and ent.table not in cols:
                problems.append(f"entity {en}: table {ent.table!r} does not exist")
            allowed = {en, *ent.joins}
            if ent.default_time not in ent.time_columns:
                problems.append(f"entity {en}: default time {ent.default_time!r} not defined")
            for tn, tc in ent.time_columns.items():
                check_expr(f"{en}.time.{tn}", ent, tc.expr, allowed)
            for jn, j in ent.joins.items():
                if cols is not None and j.table not in cols:
                    problems.append(f"{en}.joins.{jn}: table {j.table!r} does not exist")
                check_expr(f"{en}.joins.{jn}", ent, j.on, allowed)
                for r in j.requires:
                    if r not in ent.joins:
                        problems.append(f"{en}.joins.{jn}: requires unknown join {r!r}")
            try:
                _join_order(ent, set(ent.joins))
            except SemanticError as e:
                problems.append(f"entity {en}: {e}")
            for mn, msql in ent.measures.items():
                check_expr(f"{en}.measures.{mn}", ent, msql, allowed)

        for ref in self.metric_variants():
            try:
                r = self.resolve_metric(ref)
            except SemanticError as e:
                problems.append(str(e))
                continue
            ent = self.entities[r.entity]
            check_expr(f"metric {ref}", ent, r.sql, {ent.name, *ent.joins})

        for dn, d in self.dimensions.items():
            if not any(self.dimension_available(dn, e) for e in self.entities):
                problems.append(f"dimension {dn}: not reachable from any entity")
            for en, ent in self.entities.items():
                if self.dimension_available(dn, en):
                    check_expr(f"dimension {dn} on {en}", ent, d.expr, {en, *ent.joins})

        for t, a in self.ambiguous_terms.items():
            for c in a["candidates"]:
                name, _, v = c.partition(":")
                if name in self.metrics:
                    if v and v not in self.metrics[name].variants:
                        problems.append(f"ambiguous term {t!r}: unknown variant {c!r}")
                elif name not in self.dimensions:
                    problems.append(f"ambiguous term {t!r}: unknown candidate {c!r}")

        # A synonym must point to exactly one object (otherwise list it under ambiguous_terms)
        seen: dict[str, str] = {}
        for kind, objs in (("metric", self.metrics), ("dimension", self.dimensions)):
            for o in objs.values():
                syns = list(o.synonyms) + [o.name.replace("_", " ")]
                if kind == "metric":
                    for v in o.variants.values():
                        syns += [(s, f"{o.name}:{v.name}") for s in v.synonyms]
                for s in syns:
                    s, target = (s if isinstance(s, tuple) else (s, f"{kind}:{o.name}"))
                    s = s.lower()
                    if s in self.ambiguous_terms:
                        problems.append(f"synonym {s!r} of {target} is also an ambiguous term")
                    elif s in seen and seen[s] != target:
                        problems.append(f"synonym {s!r} maps to both {seen[s]} and {target}")
                    seen[s] = target

        if con is not None and not problems:
            for ref in self.metric_variants():
                try:
                    q = self.compile(QuerySpec(metrics=[ref]))
                    con.execute(f"SELECT * FROM ({q.sql}) LIMIT 0", q.params)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"metric {ref}: does not execute ({e})")
            for dn in self.dimensions:
                for en in self.entities:
                    if not self.dimension_available(dn, en):
                        continue
                    m = next(r for r in self.metric_variants() if self.resolve_metric(r).entity == en)
                    try:
                        q = self.compile(QuerySpec(metrics=[m], dimensions=[dn]))
                        con.execute(f"SELECT * FROM ({q.sql}) LIMIT 0", q.params)
                    except Exception as e:  # noqa: BLE001
                        problems.append(f"dimension {dn} on {en}: does not execute ({e})")
        return problems


# --------------------------------------------------------------------------- helpers
def _col_name(r: ResolvedMetric) -> str:
    return r.ref.replace(":", "__")


def _join_order(ent: Entity, needed: set[str]) -> list[str]:
    """Dependency-closed, topologically ordered join list."""
    unknown = needed - set(ent.joins)
    if unknown:
        raise SemanticError(f"entity {ent.name!r} cannot reach {sorted(unknown)}")
    closure, stack = set(), list(needed)
    while stack:
        a = stack.pop()
        if a in closure:
            continue
        closure.add(a)
        for r in ent.joins[a].requires:
            if r not in ent.joins:
                raise SemanticError(f"join {a!r} requires unknown join {r!r}")
            stack.append(r)
    order, done, visiting = [], set(), set()

    def visit(a):
        if a in done:
            return
        if a in visiting:
            raise SemanticError(f"join cycle at {a!r}")
        visiting.add(a)
        for r in ent.joins[a].requires:
            visit(r)
        visiting.discard(a)
        done.add(a)
        order.append(a)
    for a in sorted(closure):
        visit(a)
    return order


def _filter_sql(expr: str, f: Filter) -> tuple[str, list]:
    op = f.op.lower()
    if op in ("is null", "is not null"):
        return f"{expr} {op.upper()}", []
    if op in ("in", "not in"):
        vals = list(f.value) if isinstance(f.value, (list, tuple, set)) else [f.value]
        if not vals:
            return ("FALSE" if op == "in" else "TRUE"), []
        return f"{expr} {op.upper()} ({', '.join('?' * len(vals))})", vals
    if op == "between":
        lo, hi = f.value
        return f"{expr} BETWEEN ? AND ?", [lo, hi]
    return f"{expr} {'<>' if op == '!=' else op.upper()} ?", [f.value]
