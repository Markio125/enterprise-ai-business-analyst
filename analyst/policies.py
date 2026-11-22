"""Role-based access control on top of the semantic layer.

This is enforcement layer 1 (semantic pre-check) plus the row-level scope that the
compiler injects. The SQL validator re-checks generated SQL against the
same rules, so a request that slips past here is still caught.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from .semantic import (CompiledQuery, Filter, QuerySpec, SemanticError, SemanticModel, _join_order, aliases_in,
                       columns_in)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICIES = ROOT / "config" / "policies.yaml"
_LIST_KEYS = ("deny_tables", "deny_columns", "deny_dimensions", "deny_metrics", "row_filters")
SUGGESTED_METRICS = ["gmv_inr", "success_rate", "attempt_count", "order_count", "settled_gmv_inr",
                     "finance_gross_inr", "timeout_rate", "avg_ticket_inr"]


class AccessDenied(PermissionError):
    def __init__(self, auth: "Authorization"):
        super().__init__("; ".join(d.reason for d in auth.denials))
        self.auth = auth


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    role: str
    attrs: dict = field(default_factory=dict, hash=False)

    def attr(self, key: str):
        return self.name if key == "name" else self.attrs.get(key)


@dataclass(frozen=True)
class Denial:
    type: str        # metric | dimension | table | column | scope | entity
    name: str
    reason: str


@dataclass
class Role:
    name: str
    description: str
    lineage: tuple[str, ...]          # self first, then ancestors
    tables: tuple[str, ...]
    deny_tables: tuple[str, ...]
    deny_columns: tuple[str, ...]
    deny_dimensions: tuple[str, ...]
    deny_metrics: tuple[str, ...]
    row_filters: tuple[dict, ...]
    allow_row_level_output: bool

    def table_allowed(self, table: str) -> bool:
        if any(fnmatchcase(table, p) for p in self.deny_tables):
            return False
        return any(fnmatchcase(table, p) for p in self.tables)

    def column_allowed(self, table: str, column: str) -> bool:
        return self.table_allowed(table) and not any(fnmatchcase(f"{table}.{column}", p) for p in self.deny_columns)


@dataclass
class Authorization:
    principal: Principal
    allowed: bool
    denials: list[Denial]
    scope: list[Filter]
    disclosures: list[str]


class Policy:
    def __init__(self, raw: dict):
        self.raw = raw
        self.defaults = raw.get("defaults", {}) or {}
        self._roles_raw = raw["roles"]
        self.roles = {n: self._resolve(n, ()) for n in self._roles_raw}
        self.users = {uid: Principal(uid, u["name"], u["role"], {k: v for k, v in u.items() if k not in ("name", "role")})
                      for uid, u in (raw.get("users") or {}).items()}
        for p in self.users.values():
            if p.role not in self.roles:
                raise ValueError(f"user {p.user_id}: unknown role {p.role!r}")

    @classmethod
    def load(cls, path: Path | str = DEFAULT_POLICIES) -> "Policy":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    def _resolve(self, name: str, seen: tuple[str, ...]) -> Role:
        if name in seen:
            raise ValueError(f"role inheritance cycle: {' -> '.join(seen + (name,))}")
        if name not in self._roles_raw:
            raise ValueError(f"unknown role {name!r}")
        r = self._roles_raw[name] or {}
        parent = self._resolve(r["inherits"], seen + (name,)) if r.get("inherits") else None
        lists = {k: tuple(getattr(parent, k, ())) + tuple(r.get(k, []) or []) if parent else tuple(r.get(k, []) or [])
                 for k in _LIST_KEYS}
        return Role(
            name=name, description=r.get("description", parent.description if parent else ""),
            lineage=(name,) + (parent.lineage if parent else ()),
            tables=tuple(r["tables"]) if "tables" in r else (parent.tables if parent else ()),
            allow_row_level_output=r.get("allow_row_level_output",
                                         parent.allow_row_level_output if parent else False),
            **lists)

    def principal(self, user_id: str) -> Principal:
        if user_id not in self.users:
            raise KeyError(f"unknown user {user_id!r}")
        return self.users[user_id]

    def role_of(self, p: Principal) -> Role:
        return self.roles[p.role]

    # ---------------------------------------------------------------- checks
    def _alias_denials(self, model: SemanticModel, role: Role, entity: str, sql: str, kind: str,
                       name: str) -> list[Denial]:
        """Tables / columns an expression touches on `entity`, including the joins it needs."""
        ent = model.entities[entity]
        out = []
        aliases = aliases_in(sql) - {entity}
        try:
            joins = _join_order(ent, aliases)
        except SemanticError as e:
            return [Denial(kind, name, str(e))]
        tables = ent.tables_for(set(joins))
        for alias in [entity, *joins]:
            if not role.table_allowed(tables[alias]):
                out.append(Denial(kind, name, f"role {role.name} has no access to table {tables[alias]}"))
        for alias, col in columns_in(sql):
            t = tables.get(alias)
            if t and role.table_allowed(t) and not role.column_allowed(t, col):
                out.append(Denial(kind, name, f"role {role.name} has no access to column {t}.{col}"))
        return out

    def metric_denials(self, model: SemanticModel, role: Role, ref: str) -> list[Denial]:
        r = model.resolve_metric(ref)
        out = []
        if r.metric.name in role.deny_metrics:
            out.append(Denial("metric", r.metric.name, f"metric {r.metric.name} is not available to role {role.name}"))
        if r.metric.restricted_to and not set(r.metric.restricted_to) & set(role.lineage):
            out.append(Denial("metric", r.metric.name,
                              f"{r.metric.name} is restricted to {', '.join(r.metric.restricted_to)}"))
        out += self._alias_denials(model, role, r.entity, r.sql, "metric", r.metric.name)
        return _dedupe(out)

    def dimension_denials(self, model: SemanticModel, role: Role, dim: str, entity: str) -> list[Denial]:
        out = []
        if dim in role.deny_dimensions:
            out.append(Denial("dimension", dim, f"dimension {dim} is confidential for role {role.name}"))
        out += self._alias_denials(model, role, entity, model.dimension(dim).expr, "dimension", dim)
        return _dedupe(out)

    def scope_for(self, model: SemanticModel, principal: Principal, entity: str) -> tuple[list[Filter], list[Denial]]:
        """Row-level filters for this principal on this entity (fail closed)."""
        role = self.role_of(principal)
        filters, denials = [], []
        for rf in role.row_filters:
            if "value_from" in rf:
                key = rf["value_from"].removeprefix("user.")
                val = principal.attr(key)
                if val in (None, ""):
                    denials.append(Denial("scope", rf["dimension"],
                                          f"user {principal.user_id} has no {key!r} attribute required by role {role.name}"))
                    continue
            else:
                val = rf["value"]
            if not model.dimension_available(rf["dimension"], entity):
                denials.append(Denial("entity", entity, f"row-level scope on {rf['dimension']} cannot be applied to "
                                                        f"{entity} data, so role {role.name} cannot query it"))
                continue
            filters.append(Filter(rf["dimension"], rf.get("op", "="), val))
        return filters, denials

    def authorize(self, model: SemanticModel, principal: Principal, spec: QuerySpec) -> Authorization:
        role = self.role_of(principal)
        denials: list[Denial] = []
        entities = set()
        for ref in spec.metrics:
            try:
                entities.add(model.resolve_metric(ref).entity)
            except SemanticError as e:
                denials.append(Denial("metric", ref, str(e)))
                continue
            denials += self.metric_denials(model, role, ref)
        if denials or len(entities) != 1:
            return Authorization(principal, False, denials or [Denial("metric", ",".join(spec.metrics),
                                                                      "metrics span several entities")], [], [])
        entity = entities.pop()
        ent = model.entities[entity]
        if not role.table_allowed(ent.table):
            denials.append(Denial("table", ent.table, f"role {role.name} has no access to table {ent.table}"))
        for d in list(spec.dimensions) + [f.dimension for f in spec.filters]:
            if d not in model.dimensions:
                denials.append(Denial("dimension", d, f"unknown dimension {d!r}"))
            elif model.dimension_available(d, entity):
                denials += self.dimension_denials(model, role, d, entity)
        basis = spec.time_basis or ent.default_time
        if basis in ent.time_columns:
            denials += self._alias_denials(model, role, entity, ent.time_columns[basis].expr, "column", basis)

        scope, scope_denials = self.scope_for(model, principal, entity)
        denials += scope_denials
        disclosures = []
        if scope:
            desc = ", ".join(f"{f.dimension} = {f.value}" for f in scope)
            disclosures.append(f"Results limited to your access scope: {desc}.")
            if any(model.dimensions[f.dimension].expr.startswith("merchant.") for f in scope):
                disclosures.append("Transactions without a mapped merchant are excluded from scoped results.")
            # Explicitly asking about something outside the scope
            behaviour = self.defaults.get("out_of_scope_behaviour", "refuse")
            for uf in spec.filters:
                for sf in scope:
                    if uf.dimension != sf.dimension:
                        continue
                    asked = uf.value if isinstance(uf.value, (list, tuple, set)) else [uf.value]
                    outside = [v for v in asked if v != sf.value] if uf.op.lower() in ("=", "in") else asked
                    if outside:
                        if behaviour == "refuse":
                            denials.append(Denial("scope", uf.dimension,
                                                  f"{uf.dimension} {', '.join(map(str, outside))} is outside your "
                                                  f"access scope ({sf.dimension} = {sf.value})"))
                        else:
                            disclosures.append(f"You asked about {uf.dimension} {', '.join(map(str, outside))}, "
                                               f"which is outside your scope; those rows are not shown.")
        denials = _dedupe(denials)
        return Authorization(principal, not denials, denials, scope, disclosures)

    def build_query(self, model: SemanticModel, principal: Principal, spec: QuerySpec) -> CompiledQuery:
        """Authorize, then compile with the principal's row-level scope injected."""
        auth = self.authorize(model, principal, spec)
        if not auth.allowed:
            raise AccessDenied(auth)
        return model.compile(spec, scope=auth.scope, scope_disclosures=auth.disclosures)

    def refusal(self, model: SemanticModel, auth: Authorization) -> dict:
        """The REFUSED_ACCESS response contract (SPEC section 11.3)."""
        role = self.role_of(auth.principal)
        allowed = [m for m in SUGGESTED_METRICS
                   if m in model.metrics and not self.metric_denials(model, role, m)]
        scope = ", ".join(f"{f['dimension']} = "
                          f"{f['value'] if 'value' in f else auth.principal.attr(f['value_from'].removeprefix('user.'))}"
                          for f in role.row_filters)
        scope_d = [d for d in auth.denials if d.type == "scope"]
        other = sorted({d.name for d in auth.denials if d.type != "scope"})
        parts = [d.reason[0].upper() + d.reason[1:] + "." for d in scope_d]
        if other:
            parts.append(f"You don't have access to {', '.join(other)}.")
        msg = " ".join(parts)
        if scope_d and not other:
            msg += f" I can answer the same question for {scope}."
        elif allowed:
            msg += f" I can show {', '.join(allowed[:3])} instead" + (f" (within {scope})." if scope else ".")
        return {
            "status": "REFUSED_ACCESS",
            "message": msg,
            "denied": [d.__dict__ for d in auth.denials],
            "alternatives": allowed[:5],
            "role": role.name,
        }


def _dedupe(ds: list[Denial]) -> list[Denial]:
    seen, out = set(), []
    for d in ds:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out
