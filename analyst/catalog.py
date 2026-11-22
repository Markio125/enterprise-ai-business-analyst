"""Role-filtered catalog: what a given user may ask about.

The same structure backs the GET /api/v1/catalog endpoint and the
grounding block in the LLM prompt: the model only ever sees metrics,
dimensions and values the user is allowed to use.
"""
from __future__ import annotations

from datetime import date

from .policies import Policy, Principal
from .semantic import QuerySpec, SemanticModel

MAX_VALUES = 40


def dimension_values(model: SemanticModel, con, dims: list[str] | None = None) -> dict[str, list]:
    """Distinct values of low-cardinality dimensions (helps map "Meridian" -> issuer_bank)."""
    out = {}
    for name in dims or list(model.dimensions):
        d = model.dimensions[name]
        if d.high_cardinality or d.time_grain == "day":
            continue
        entity = next((e for e in model.entities if model.dimension_available(name, e)), None)
        metric = next(r for r in model.metric_variants() if model.resolve_metric(r).entity == entity)
        q = model.compile(QuerySpec(metrics=[metric], dimensions=[name]))
        vals = [r[0] for r in con.execute(f"SELECT {name} FROM ({q.sql}) t", q.params).fetchall() if r[0] is not None]
        if len(vals) <= MAX_VALUES:
            out[name] = [str(v) for v in vals]
    return out


def build_catalog(model: SemanticModel, policy: Policy, principal: Principal, con=None,
                  as_of: date | None = None) -> dict:
    role = policy.role_of(principal)
    metrics, hidden = [], []
    for m in model.metrics.values():
        refs = [m.name] if not m.has_variants else [f"{m.name}:{v}" for v in m.variants]
        ok_refs = [r for r in refs if not policy.metric_denials(model, role, r)]
        if not ok_refs:
            hidden.append({"type": "metric", "name": m.name})
            continue
        variants = [{"name": r.partition(":")[2], "description": m.variants[r.partition(":")[2]].description,
                     "default": r.partition(":")[2] == m.default_variant} for r in ok_refs] if m.has_variants else []
        metrics.append({
            "name": m.name, "description": m.description, "format": m.format, "better": m.better,
            "synonyms": list(m.synonyms), "entity": model.resolve_metric(ok_refs[0]).entity,
            **({"variants": variants, "always_disclose_variant": m.ambiguity_flag} if variants else {}),
        })

    usable_entities = {}
    for en, ent in model.entities.items():
        if not role.table_allowed(ent.table):
            continue
        scope, denials = policy.scope_for(model, principal, en)
        if denials:
            continue
        usable_entities[en] = {
            "grain": ent.grain, "description": ent.description,
            "time_bases": {k: v.description for k, v in ent.time_columns.items()},
            "default_time_basis": ent.default_time, "time_basis_ambiguous": ent.time_ambiguous,
        }
    metrics = [m for m in metrics if m["entity"] in usable_entities]

    dims = []
    for dn, d in model.dimensions.items():
        ents = [e for e in usable_entities
                if model.dimension_available(dn, e) and not policy.dimension_denials(model, role, dn, e)]
        if ents:
            dims.append({"name": dn, "description": d.description, "synonyms": list(d.synonyms),
                         "available_for": ents, "high_cardinality": d.high_cardinality})
        else:
            hidden.append({"type": "dimension", "name": dn})

    cat = {
        "user": {"id": principal.user_id, "name": principal.name, "role": role.name,
                 "role_description": role.description},
        "scope": [{"dimension": f["dimension"], "op": f.get("op", "="),
                   "value": f["value"] if "value" in f else principal.attr(f["value_from"].removeprefix("user."))}
                  for f in role.row_filters],
        "allow_row_level_output": role.allow_row_level_output,
        "as_of": as_of.isoformat() if as_of else None,
        "timezone": model.timezone,
        "entities": usable_entities,
        "metrics": metrics,
        "dimensions": dims,
        "ambiguous_terms": _visible_ambiguities(model, metrics, dims),
        "time_expressions": model.time_expressions,
        "glossary": model.glossary,
        "not_accessible": hidden,
    }
    if con is not None:
        cat["dimension_values"] = dimension_values(model, con, [d["name"] for d in dims])
        if cat["scope"]:
            # values outside the user's scope must not leak through the catalog
            cat["dimension_values"] = {k: v for k, v in cat["dimension_values"].items()
                                       if k not in ("merchant", "merchant_id", "account_manager", "region", "city")}
    return cat


def _visible_ambiguities(model: SemanticModel, metrics: list[dict], dims: list[dict]) -> dict:
    """Only candidates the user can use; a term with one usable candidate is no longer ambiguous."""
    usable = {m["name"] for m in metrics} | {d["name"] for d in dims}
    out = {}
    for term, a in model.ambiguous_terms.items():
        cands = [c for c in a["candidates"] if c.partition(":")[0] in usable]
        if len(cands) > 1:
            out[term] = a | {"candidates": cands, "default": a.get("default") if a.get("default") in cands else cands[0]}
    return out


def render_markdown(cat: dict) -> str:
    """Compact text form for the LLM system prompt."""
    u = cat["user"]
    lines = [f"# Data catalog for {u['name']} ({u['role']})", ""]
    if cat["scope"]:
        lines.append("Scope (applied automatically): " + ", ".join(f"{s['dimension']} {s['op']} {s['value']}"
                                                                  for s in cat["scope"]))
    lines += [f"As-of date: {cat['as_of']} ({cat['timezone']}). Relative dates resolve against it.", "", "## Metrics"]
    for m in cat["metrics"]:
        syn = f" (aka {', '.join(m['synonyms'])})" if m["synonyms"] else ""
        lines.append(f"- `{m['name']}` [{m['format']}, {m['entity']}]{syn}: {m['description']}")
        for v in m.get("variants", []):
            lines.append(f"    - variant `{v['name']}`{' (default)' if v['default'] else ''}: {v['description']}")
    lines += ["", "## Dimensions"]
    for d in cat["dimensions"]:
        vals = cat.get("dimension_values", {}).get(d["name"])
        extra = f" values: {', '.join(vals[:15])}{'...' if len(vals) > 15 else ''}" if vals else ""
        syn = f" (aka {', '.join(d['synonyms'])})" if d["synonyms"] else ""
        lines.append(f"- `{d['name']}`{syn}{':' if extra else ''}{extra}")
    if cat["ambiguous_terms"]:
        lines += ["", "## Ambiguous terms (clarify or disclose)"]
        for t, a in cat["ambiguous_terms"].items():
            lines.append(f"- \"{t}\": {', '.join(a['candidates'])} (default {a.get('default')}). {a.get('note', '')}".rstrip())
    if cat["not_accessible"]:
        lines += ["", "## Not accessible to this user",
                  ", ".join(f"{h['type']} {h['name']}" for h in cat["not_accessible"])]
    return "\n".join(lines)
