/* Business Analyst UI: a no-build single page talking to the /api/v1 API.
 *
 * Mock sign-in: the selected user is sent as X-User-Id on every request (SPEC 1.2).
 * Progress arrives over SSE; EventSource cannot send headers, so the stream is read with
 * fetch() and parsed here. Charts are drawn as inline SVG from the composer's chart specs (SPEC 16).
 */
(() => {
  "use strict";
  const API = "/api/v1";
  const $ = (s, el = document) => el.querySelector(s);
  const state = { users: [], user: null, me: null, session: null, busy: false };

  const EXAMPLES = {
    exec: ["Why did our payment success rate drop in the last two weeks?", "What happened to card success on September 10?",
           "How is success rate trending lately?", "Show MDR revenue by merchant", "Delete the duplicates from Oct 5"],
    ops_analyst: ["Which acquirer had the worst card-not-present performance in late September and why?",
                  "What happened to card success on September 10?", "Show me MDR revenue by merchant last month",
                  "Success rate by payment method last week"],
    regional_manager_west: ["Top 5 merchants by GMV last month", "GMV for the NORTH region last month",
                            "Success rate by payment method last week"],
    finance: ["Why does finance's settled amount for October differ from the dashboard?", "What is MDR revenue by payment method last month?"],
    account_manager: ["Top 5 merchants by GMV last month", "Success rate by payment method last week"],
    merchant_user: ["What's our success rate last week?", "Success rate by payment method last week"],
  };
  const STAGE = {
    PARSING: "Understanding", ACCESS_PRECHECK: "Access check", AMBIGUITY_CHECK: "Ambiguity check", PLANNING: "Planning",
    EXECUTION: "Running queries", VALIDATION: "Validating SQL", SQL_GENERATION: "Writing SQL", DQ_CHECK: "Data quality",
    ANALYSIS: "Analysing", COMPOSITION: "Writing answer", GROUNDING_CHECK: "Checking numbers", DONE: "Done",
  };

  // ------------------------------------------------------------------ helpers
  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "html") el.innerHTML = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
    return el;
  }

  async function api(path, opts = {}) {
    const r = await fetch(API + path, {
      ...opts,
      headers: { "X-User-Id": state.user, ...(opts.body ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) },
    });
    const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new Error((body && body.detail) || `${r.status} ${r.statusText}`);
    return body;
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const nfIN = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
  function compact(v) {
    const a = Math.abs(v);
    if (a >= 1e7) return (v / 1e7).toFixed(a >= 1e9 ? 0 : 2) + " Cr";
    if (a >= 1e5) return (v / 1e5).toFixed(1) + " L";
    return nfIN.format(Math.round(v));
  }
  function fmt(v, format) {
    if (v == null || Number.isNaN(v)) return "–";
    if (format && format.includes("%")) return (v * 100).toFixed(1) + "%";
    return compact(v);
  }
  function fmtDelta(v, format) {
    if (v == null) return "–";
    const s = v > 0 ? "+" : v < 0 ? "−" : "";
    if (format && format.includes("%")) return s + Math.abs(v * 100).toFixed(2) + "pp";
    return s + compact(Math.abs(v));
  }
  function cell(v, col) {
    if (v == null) return "∅";
    if (typeof v === "number") {
      if (/rate|share|pct/i.test(col) && Math.abs(v) <= 1.5 && !Number.isInteger(v)) return (v * 100).toFixed(2) + "%";
      return nfIN.format(v);
    }
    return String(v);
  }

  // ------------------------------------------------------------------ boot / users
  async function boot() {
    const u = await fetch(API + "/users").then((r) => r.json());
    state.users = u.users;
    $("#asof").textContent = `data as of ${u.as_of}`;
    $("#llm").textContent = `Language model: ${u.llm}`;
    const sel = $("#user");
    sel.replaceChildren(...u.users.map((x) => h("option", { value: x.user_id }, `${x.name} · ${x.role}`)));
    const saved = store.get("analyst.user");
    sel.value = u.users.some((x) => x.user_id === saved) ? saved : u.users[0].user_id;
    sel.addEventListener("change", () => switchUser(sel.value));
    $("#newsession").addEventListener("click", () => switchUser(state.user, true));
    await switchUser(sel.value);
  }

  async function switchUser(uid, keepNote) {
    state.user = uid;
    store.set("analyst.user", uid);
    const [me, cat, sess] = await Promise.all([api("/me"), api("/catalog"), api("/sessions", { method: "POST", body: "{}" })]);
    state.me = me;
    state.session = sess.session_id;
    $("#role").textContent = me.role + (me.admin ? " · admin" : "");
    $("#role").title = me.description;
    renderCatalog(cat);
    renderExamples(me.role);
    $("#thread").replaceChildren(emptyState(me));
    loadDQ();
    $("#q").focus();
  }

  function emptyState(me) {
    const ex = (EXAMPLES[me.role] || EXAMPLES.exec).slice(0, 3);
    return h("div", { class: "empty", id: "empty" },
      h("h1", {}, `Ask a question about payments data`),
      h("p", { class: "muted" }, `Signed in as ${me.name} (${me.role}). ${me.description} ` +
        "Every figure in an answer is traced to a query you can inspect."),
      h("div", { class: "chips", style: "justify-content:center" }, ex.map((q) => h("button", { type: "button", onclick: () => ask(q) }, q))));
  }

  function renderExamples(role) {
    $("#examples").replaceChildren(...(EXAMPLES[role] || EXAMPLES.exec).map((q) =>
      h("li", {}, h("button", { type: "button", onclick: () => ask(q) }, q))));
  }

  function renderCatalog(c) {
    $("#scope").textContent = c.scope && c.scope.length ? `Row scope: ${c.scope.map((x) => `${x.dimension} ${x.op} ${Array.isArray(x.value) ? x.value.join(", ") : x.value}`).join("; ")}`
      : "No row restrictions for your role.";
    $("#nmetrics").textContent = c.metrics.length;
    $("#metrics").replaceChildren(...c.metrics.map((m) => h("li", { title: m.description }, h("code", {}, m.name), " ", h("span", { class: "muted" }, m.description.split(".")[0]))));
    $("#ndims").textContent = c.dimensions.length;
    $("#dims").replaceChildren(...c.dimensions.map((d) => h("li", {}, h("code", {}, d.name), d.synonyms?.length ? h("span", { class: "muted" }, " · " + d.synonyms.slice(0, 2).join(", ")) : "")));
    const na = c.not_accessible || [];
    $("#denied").textContent = na.length ? `Not available to your role: ${na.map((x) => x.name || x).join(", ")}` : "";
  }

  async function loadDQ() {
    const el = $("#dq");
    try {
      const dq = await api("/dq/summary");
      const bad = dq.checks.filter((c) => c.severity !== "OK" && c.severity !== "PASS");
      if (!bad.length) { el.hidden = true; return; }
      const block = bad.some((c) => c.severity === "BLOCK");
      el.className = "dq" + (block ? " block" : "");
      el.replaceChildren(h("details", {},
        h("summary", {}, `Data quality: ${bad.length} ${bad.length === 1 ? "issue" : "issues"} in ${dq.window[0]} – ${dq.window[1]} (data version ${dq.data_version}). Answers account for them; expand for details.`),
        h("ul", {}, bad.map((c) => h("li", {}, h("b", {}, c.check_id.replace(/_/g, " ")), ` (${c.severity}): ${c.impact}`)))));
      el.hidden = false;
    } catch { el.hidden = true; }
  }

  // ------------------------------------------------------------------ asking + streaming
  function setBusy(b) {
    state.busy = b;
    $("#send").disabled = b;
    $("#q").disabled = b;
    if (!b) $("#q").focus();
  }

  async function ask(text) {
    text = (text || "").trim();
    if (!text || state.busy) return;
    $("#empty")?.remove();
    $("#q").value = "";
    autosize();
    const node = $("#tpl-turn").content.firstElementChild.cloneNode(true);
    $(".you", node).textContent = text;
    $("#thread").append(node);
    const turn = { el: node, progress: $(".progress", node), body: $(".body", node), last: 0, chips: {} };
    node.scrollIntoView({ block: "end" });
    setBusy(true);
    try {
      const r = await api(`/sessions/${state.session}/questions`, { method: "POST", body: JSON.stringify({ text }) });
      turn.qid = r.question_id;
      await follow(turn);
    } catch (e) {
      turn.body.replaceChildren(h("div", { class: "notice failed" }, `Something went wrong: ${e.message}`));
      setBusy(false);
    }
  }

  async function follow(turn) {
    // read the SSE stream from the last event seen; the server closes it after the last `done`
    const r = await fetch(`${API}/questions/${turn.qid}/stream?after=${turn.last}`, { headers: { "X-User-Id": state.user } });
    if (!r.ok || !r.body) throw new Error(`stream failed (${r.status})`);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const data = raw.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
        if (!data) continue;                      // keep-alive comment
        const ev = JSON.parse(data);
        turn.last = Math.max(turn.last, ev.id || 0);
        await onEvent(turn, ev);
      }
    }
  }

  function chip(turn, key, label, cls) {
    let c = turn.chips[key];
    if (!c) { c = turn.chips[key] = h("li"); turn.progress.append(c); }
    c.textContent = label;
    c.className = cls || "";
    return c;
  }

  async function onEvent(turn, ev) {
    switch (ev.event) {
      case "run_started":
        turn.run = ev.run;
        turn.nexec = 0;
        turn.progress.classList.remove("collapsed");
        if (ev.run > 1) { turn.chips = {}; turn.progress.replaceChildren(); }
        break;
      case "stage_started":
        if (ev.stage === "EXECUTION") {
          turn.nexec = (turn.nexec || 0) + 1;
          chip(turn, "EXECUTION", `Running queries (${turn.nexec})…`, "run").title = ev.summary || "";
        } else chip(turn, ev.stage, `${STAGE[ev.stage] || ev.stage}…`, "run");
        break;
      case "stage_completed": {
        if (ev.stage === "DONE" || ev.stage === "SQL_GENERATION" || ev.stage === "VALIDATION") break;
        const key = ev.stage === "EXECUTION" ? "EXECUTION" : ev.stage;
        const ok = ["OK", "ANSWERED", "SKIPPED", "CACHED", "PROCEED", "ASSUMED", "PASS", "WARN"].includes(ev.status);
        const label = ev.stage === "EXECUTION" ? `Ran ${turn.nexec || 1} quer${(turn.nexec || 1) === 1 ? "y" : "ies"}` : STAGE[ev.stage] || ev.stage;
        const bad = /FAIL|REFUSED|REJECT|ERROR|BLOCK/.test(ev.status);
        chip(turn, key, `${label} · ${ok ? "✓" : ev.status.toLowerCase().replace(/_/g, " ")}`, ok ? "done" : bad ? "bad" : "warn").title = `${ev.ms} ms`;
        break;
      }
      case "clarification_needed":
        renderClarify(turn, ev);
        break;
      case "narrowing_needed":
        renderNarrow(turn, ev);
        break;
      case "answer": {
        if (/^AWAITING/.test(ev.status)) break;   // the run is waiting on the user; the form is already shown
        const q = await api(`/questions/${turn.qid}`);
        turn.progress.classList.add("collapsed");
        renderResult(turn, q);
        turn.el.scrollIntoView({ block: "start" });   // read the answer from its headline
        return;
      }
      case "error":
        turn.body.append(h("div", { class: "notice failed" }, ev.message));
        break;
      case "done":
        if (!ev.waiting) setBusy(false);
        break;
    }
    turn.el.scrollIntoView({ block: "end" });
  }

  // ------------------------------------------------------------------ clarification / narrowing
  function renderClarify(turn, ev) {
    const items = ev.items || [];
    const form = h("form", { class: "notice ask" },
      h("div", {}, h("b", {}, "I need one detail before answering.")),
      items.map((it) => h("fieldset", { style: "border:0;padding:0;margin:8px 0 0" },
        h("legend", { class: "small" }, it.question_to_user ? it.question_to_user.split(":")[0] + ":" : it.field),
        h("div", { class: "options" }, it.options.map((o) => h("label", {},
          h("input", { type: "radio", name: it.key, value: o.id, checked: o.id === it.recommended }),
          h("span", {}, o.label, o.id === it.recommended ? h("span", { class: "badge", style: "margin-left:6px" }, "default") : ""),
          o.display ? h("span", { class: "val" }, o.display) : ""))))),
      h("div", { class: "row-actions" }, h("button", { class: "btn", type: "submit" }, "Continue"),
        h("span", { class: "small muted" }, "Values shown are each definition's result for your period, so you can see why it matters.")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const choices = Object.fromEntries(items.map((it) => [it.key, new FormData(form).get(it.key) || it.recommended]));
      form.querySelectorAll("input,button").forEach((x) => (x.disabled = true));
      try {
        await api(`/questions/${turn.qid}/clarify`, { method: "POST", body: JSON.stringify({ choices }) });
        await follow(turn);
      } catch (err) { form.append(h("div", { class: "notice failed" }, err.message)); setBusy(false); }
    });
    turn.body.append(form);
  }

  function renderNarrow(turn, ev) {
    const opts = ev.options || (ev.narrowing && ev.narrowing.options) || [];
    const box = h("div", { class: "notice ask" }, h("div", {}, h("b", {}, "This would be too large to run as asked. "), ev.message || ""),
      h("div", { class: "chips" }, opts.map((o) => h("button", {
        type: "button", onclick: async (e) => {
          box.querySelectorAll("button").forEach((b) => (b.disabled = true));
          e.target.classList.add("on");
          try {
            await api(`/questions/${turn.qid}/narrow`, { method: "POST", body: JSON.stringify({ option_id: o.id }) });
            await follow(turn);
          } catch (err) { box.append(h("div", { class: "notice failed" }, err.message)); setBusy(false); }
        },
      }, o.label))));
    turn.body.append(box);
  }

  // ------------------------------------------------------------------ answer card
  function renderResult(turn, q) {
    const res = q.result || {};
    const a = res.answer || {};
    const status = a.status || q.status;
    const card = h("div", { class: "answer" });
    turn.body.replaceChildren(card);   // replaces the clarification form once the answer is in

    if (status === "REFUSED_ACCESS" || status === "REJECTED_UNSAFE" || status === "FAILED") {
      const cls = status === "REFUSED_ACCESS" ? "refused" : status === "REJECTED_UNSAFE" ? "unsafe" : "failed";
      const title = { refused: "Not available for your role", unsafe: "I can only read data", failed: "I couldn't answer that" }[cls];
      card.append(h("div", { class: `notice ${cls}` }, h("b", {}, title), h("div", {}, a.headline || res.message || "")));
      if (res.refusal?.denied?.length) {
        card.append(h("ul", { class: "small muted" }, [...new Set(res.refusal.denied.map((d) => d.reason))].map((r) => h("li", {}, r))));
      }
      card.append(followUps(a), footer(turn, q, a));
      return;
    }
    if (!a.facts?.length && !a.charts?.length) {         // direct answer (definitions, off-topic, not found)
      card.append(h("p", { class: "headline" }, a.headline || res.message || ""));
      for (const c of a.caveats || []) card.append(h("p", { class: "small muted" }, c.text));
      card.append(followUps(a), footer(turn, q, a));
      return;
    }

    card.append(h("p", { class: "headline" }, a.headline));
    const conf = a.confidence || {};
    const allCached = res.steps?.length && res.steps.every((s) => s.cache_hit);
    card.append(h("div", { class: "badges" },
      conf.level ? h("span", { class: `badge ${conf.level}`, title: (conf.reasons || []).join("\n") }, `${conf.level} confidence`) : "",
      status !== "ANSWERED" ? h("span", { class: "badge MEDIUM" }, status.replace(/_/g, " ").toLowerCase()) : "",
      a.grounding?.passed ? h("span", { class: "badge", title: `${a.grounding.checked} numbers checked against query results` }, `${a.grounding.checked} numbers verified`) : "",
      res.steps?.length ? h("span", { class: "badge" }, `${res.steps.length} quer${res.steps.length === 1 ? "y" : "ies"}${allCached ? " · from cache" : ""}`) : "",
      a.definitions_used?.scope && /limited/i.test(a.definitions_used.scope) ? h("span", { class: "badge", title: a.definitions_used.scope }, "row scope applied") : ""));

    const tabs = [["Answer", () => answerTab(a, res, show)], [`Charts (${(a.charts || []).filter((c) => c.type !== "kpi").length})`, () => chartsTab(a)],
                  [`Evidence (${(a.evidence || []).length})`, () => evidenceTab(a, res, q)], ["Steps", () => stepsTab(res)]];
    const panes = tabs.map(() => h("div", { hidden: true }));
    const built = new Set();
    const bar = h("div", { class: "tabs", role: "tablist" }, tabs.map(([label], i) =>
      h("button", { type: "button", role: "tab", "aria-selected": "false", onclick: () => show(i) }, label)));
    function show(i, focusId) {
      if (!built.has(i)) { panes[i].append(tabs[i][1]()); built.add(i); }
      panes.forEach((p, j) => (p.hidden = j !== i));
      [...bar.children].forEach((b, j) => b.setAttribute("aria-selected", String(j === i)));
      if (focusId) {
        const el = panes[i].querySelector(`[data-ev="${CSS.escape(focusId)}"]`);
        if (el) { el.open = true; el.classList.add("flash"); el.scrollIntoView({ block: "center" }); setTimeout(() => el.classList.remove("flash"), 1500); }
      }
    }
    show.evidence = (id) => show(2, id);
    card.append(bar, ...panes, followUps(a), footer(turn, q, a));
    show(0);
  }

  function refs(ids, show) {
    return (ids || []).map((id) => h("button", { type: "button", class: "ref", title: "Show evidence", onclick: () => show.evidence(id) }, id));
  }

  function answerTab(a, res, show) {
    const out = h("div");
    // charts that summarise the answer at a glance: KPI first
    const kpi = (a.charts || []).find((c) => c.type === "kpi");
    if (kpi) out.append(h("div", { class: "chart", style: "margin-bottom:10px" }, renderChart(kpi)));
    if (a.facts?.length) out.append(h("h3", {}, "Facts"), h("ul", { class: "items facts" },
      a.facts.map((f) => h("li", {}, h("span", { class: "tag" }, f.id), f.text, " ", refs(f.evidence, show)))));
    if (a.hypotheses?.length) out.append(h("h3", {}, "Hypotheses ", h("span", { class: "small muted" }, "(likely explanations, not proven)")),
      h("ul", { class: "items hyps" }, a.hypotheses.map((x) => h("li", {},
        h("span", { class: "tag" }, x.id), h("span", { class: `badge ${x.confidence}`, style: "margin-right:6px" }, x.confidence), x.text,
        x.how_to_validate ? h("div", { class: "sub" }, "How to check: ", x.how_to_validate) : "",
        x.supporting_facts?.length ? h("div", { class: "sub" }, "Based on ", x.supporting_facts.join(", ")) : ""))));
    if (a.recommendations?.length) out.append(h("h3", {}, "Recommendations"), h("ul", { class: "items recs" },
      a.recommendations.map((r) => h("li", {}, h("span", { class: "tag" }, r.id), r.text,
        h("div", { class: "sub" }, [r.impact_estimate && `Impact: ${r.impact_estimate}`, r.effort && `Effort: ${r.effort.toLowerCase()}`,
          r.owner_hint && `Owner: ${r.owner_hint}`, r.based_on?.length && `Based on ${r.based_on.join(", ")}`].filter(Boolean).join(" · "))))));
    if (a.caveats?.length) out.append(h("h3", {}, "Caveats"), h("ul", { class: "items cavs" }, a.caveats.map((c) => h("li", {}, c.text))));
    const conf = a.confidence || {};
    if (conf.level) out.append(h("h3", {}, `Confidence: ${conf.level.toLowerCase()} (${conf.score})`),
      h("ul", { class: "small", style: "margin:0;padding-left:18px" }, (conf.reasons || []).map((r) => h("li", {}, r)),
        (conf.limitations || []).map((r) => h("li", { class: "muted" }, r))));
    const d = a.definitions_used || {};
    out.append(h("h3", {}, "Definitions used"), h("dl", { class: "defs" },
      [["Metric", d.metric], ["Period", d.time_window], ["Scope", d.scope], ["Data version", d.data_version]]
        .filter(([, v]) => v).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])));
    if (res.disclosures?.length) out.append(h("details", { style: "margin-top:8px" }, h("summary", { class: "small" }, "How I read your question"),
      h("ul", { class: "small muted" }, res.disclosures.map((x) => h("li", {}, x)))));
    return out;
  }

  function chartsTab(a) {
    const cs = (a.charts || []).filter((c) => c.type !== "kpi");   // the KPI already leads the Answer tab
    if (!cs.length) return h("p", { class: "muted" }, "No charts for this answer.");
    return h("div", { class: "charts" }, cs.map((c) => h("div", { class: "chart" }, renderChart(c))));
  }

  function evidenceTab(a, res, q) {
    const steps = Object.fromEntries((res.steps || []).map((s) => [s.step_id, s]));
    // latest run's stored query per step (ids look like "<qid>:<run>:<step>")
    const stored = {};
    for (const x of q.queries || []) {
      const run = +(x.id.split(":")[1] || 0);
      if (!stored[x.step_ref] || run >= stored[x.step_ref].run) stored[x.step_ref] = { ...x, run };
    }
    const out = h("div");
    const ids = new Set((a.evidence || []).map((e) => e.id));
    const extra = (res.steps || []).filter((s) => !ids.has(s.step_id)).map((s) => ({ id: s.step_id, type: "query", purpose: s.purpose }));
    for (const e of [...(a.evidence || []), ...extra]) {
      if (e.type !== "query") {
        out.append(h("details", { class: "ev", "data-ev": e.id }, h("summary", {}, h("span", { class: "tag" }, e.id),
          h("b", {}, e.method ? e.method.replace(/_/g, " ").toLowerCase() : e.type), e.dimension ? ` by ${e.dimension}` : ""),
        h("div", { class: "inner small muted" }, "Computed in code from the query results above (deterministic, no language model).")));
        continue;
      }
      const s = steps[e.id] || {};
      const sq = stored[e.id];
      const final = s.sql || e.sql || sq?.final_sql || "";
      const gen = s.generated_sql || sq?.generated_sql;
      const rls = s.rls || e.rls || [];
      const inner = h("div", { class: "inner" });
      if (rls.length) inner.append(h("div", { class: "rls" }, h("b", {}, "Access rules applied: "), rls.join("; ")));
      inner.append(h("pre", { class: "sql" }, final));
      const actions = h("div", { class: "row-actions" },
        h("button", { class: "btn ghost", type: "button", onclick: (ev) => copy(final, ev.target) }, "Copy SQL"));
      if (gen && gen !== final) {
        const genPre = h("pre", { class: "sql", hidden: true }, gen);
        actions.append(h("button", { class: "btn ghost", type: "button", onclick: () => (genPre.hidden = !genPre.hidden) }, "SQL before access rules"));
        inner.append(genPre);
      }
      if (sq) actions.append(h("button", { class: "btn ghost", type: "button", onclick: (ev) => rerun(sq.id, ev.target, inner) }, "Re-run"));
      actions.append(h("span", { class: "small muted" }, [s.data_version || e.data_version, s.cache_hit ? `cached ${s.cached_at || ""}` : null,
        s.truncated ? "result truncated" : null].filter(Boolean).join(" · ")));
      inner.append(actions);
      const cols = s.columns || sq?.result_sample?.columns;
      const rows = s.rows || sq?.result_sample?.rows;
      if (cols && rows) inner.append(table(cols.map((c) => c.name || c), rows));
      (s.disclosures || []).forEach((d) => inner.append(h("div", { class: "small muted" }, d)));
      out.append(h("details", { class: "ev", "data-ev": e.id },
        h("summary", {}, h("span", { class: "tag" }, e.id), h("b", {}, e.purpose || s.purpose || "Query"),
          h("span", { class: "small muted" }, `${e.status || s.status || ""} · ${s.row_count ?? e.rows ?? "?"} rows · ${s.duration_ms ?? e.duration_ms ?? 0} ms`),
          rls.length ? h("span", { class: "badge" }, "RLS") : "", s.cache_hit ? h("span", { class: "badge" }, "cache") : ""),
        inner));
    }
    return out;
  }

  function stepsTab(res) {
    const tr = res.trace || [];
    let tin = 0, tout = 0;
    const list = h("ol", { class: "timeline" }, tr.map((t) => {
      if (t.usage) { tin += t.usage.input_tokens || 0; tout += t.usage.output_tokens || 0; }
      const det = t.detail ? Object.entries(t.detail).filter(([, v]) => v != null && !(Array.isArray(v) && !v.length))
        .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ") : "";
      return h("li", {}, h("span", {}, STAGE[t.stage] || t.stage, t.step_ref ? ` ${t.step_ref}` : ""),
        h("span", {}, h("span", { class: `st-${t.status}` }, t.status), det ? h("span", { class: "muted" }, " · " + det.slice(0, 220)) : ""),
        h("span", { class: "ms" }, `${t.ms} ms`));
    }));
    return h("div", {}, list, h("p", { class: "small muted" }, `Language model: ${res.llm || "?"} · tokens in ${tin}, out ${tout}`));
  }

  function followUps(a) {
    const f = a.suggested_follow_ups || [];
    if (!f.length) return "";
    return h("div", { class: "chips" }, f.map((x) => h("button", { type: "button", onclick: () => ask(x) }, x)));
  }

  function footer(turn, q, a) {
    const t = h("span", { class: "thumbs" });
    for (const [r, label] of [[1, "👍 Helpful"], [-1, "👎 Not helpful"]]) {
      t.append(h("button", { type: "button", onclick: async (e) => {
        t.querySelectorAll("button").forEach((b) => b.classList.remove("on"));
        e.target.classList.add("on");
        try { await api(`/questions/${q.question_id}/feedback`, { method: "POST", body: JSON.stringify({ rating: r }) }); } catch { /* ignore */ }
      } }, label), " ");
    }
    return h("div", { class: "foot" }, h("span", {}, `${q.question_id}${a.composed_by ? " · worded by " + a.composed_by : ""}`), t);
  }

  async function copy(text, btn) {
    try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; } catch { btn.textContent = "Copy failed"; }
    setTimeout(() => (btn.textContent = "Copy SQL"), 1200);
  }

  async function rerun(id, btn, inner) {
    btn.disabled = true;
    btn.textContent = "Running…";
    try {
      const r = await api(`/queries/${encodeURIComponent(id)}/rerun`, { method: "POST" });
      const box = h("div", { class: "small" }, h("b", {}, `Re-ran through the safety checks as ${state.me.name}: `),
        `${r.status}, ${r.row_count ?? 0} rows in ${r.duration_ms ?? 0} ms`, r.error ? ` (${r.error})` : "");
      inner.append(box);
      if (r.columns && r.rows) inner.append(table(r.columns.map((c) => c.name || c), r.rows));
    } catch (e) { inner.append(h("div", { class: "notice failed" }, e.message)); }
    btn.disabled = false;
    btn.textContent = "Re-run";
  }

  function table(cols, rows, max = 200) {
    const numeric = cols.map((_, j) => rows.some((r) => typeof r[j] === "number"));
    return h("div", { class: "tbl-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, cols.map((c, j) => h("th", { class: numeric[j] ? "num" : "" }, c)))),
      h("tbody", {}, rows.slice(0, max).map((r) => h("tr", {}, r.map((v, j) => h("td", { class: numeric[j] ? "num" : "" }, cell(v, cols[j]))))))));
  }

  // ------------------------------------------------------------------ charts (SPEC 16) as inline SVG
  const W = 560, H = 230, M = { l: 52, r: 12, t: 16, b: 34 };
  const svgNS = "http://www.w3.org/2000/svg";
  function s(tag, attrs = {}, text) {
    const el = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
    if (text != null) el.textContent = text;
    return el;
  }
  function frame(h0 = H) {
    return s("svg", { viewBox: `0 0 ${W} ${h0}`, role: "img" });
  }
  function niceDomain(vals, zeroIfClose = true) {
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (zeroIfClose && lo >= 0 && (hi === 0 || lo / hi < 0.6)) lo = 0;
    else { const pad = (hi - lo || Math.abs(hi) || 1) * 0.25; lo -= pad; hi += pad * 0.6; if (lo < 0 && Math.min(...vals) >= 0) lo = 0; }
    if (hi === lo) hi = lo + 1;
    return [lo, hi];
  }
  function yAxis(svg, lo, hi, format, h0 = H) {
    const y = (v) => M.t + (h0 - M.t - M.b) * (1 - (v - lo) / (hi - lo));
    for (let i = 0; i <= 4; i++) {
      const v = lo + ((hi - lo) * i) / 4, yy = y(v);
      svg.append(s("line", { x1: M.l, x2: W - M.r, y1: yy, y2: yy, class: "grid" }));
      svg.append(s("text", { x: M.l - 6, y: yy + 3, "text-anchor": "end" }, fmt(v, format)));
    }
    return y;
  }
  function legend(items) {
    return h("div", { class: "legend" }, items.map(([label, color]) => h("span", {}, h("i", { style: `background:${color}` }), label)));
  }

  function renderChart(c) {
    const box = h("div");
    box.append(h("h4", {}, c.title || c.type));
    try {
      const fn = { kpi: kpiChart, line: lineChart, waterfall: waterfallChart, grouped_bar: groupedBar, bar: barChart, table: tableChart }[c.type];
      box.append(fn ? fn(c) : h("pre", { class: "sql" }, JSON.stringify(c.data, null, 1).slice(0, 2000)));
    } catch (e) { box.append(h("p", { class: "small muted" }, `Could not draw this chart (${e.message}).`)); }
    if (c.caveat_badge) box.append(h("div", { class: "caveat" }, "⚠ " + c.caveat_badge));
    if (c.evidence) box.append(h("div", { class: "small muted" }, `Source: ${c.evidence}`));
    return box;
  }

  function kpiChart(c) {
    const d = c.data || {}, f = d.format;
    const delta = d.delta ?? (d.P1 != null && d.P0 != null ? d.P1 - d.P0 : null);
    const rel = !f?.includes("%") && d.P0 ? ` (${((d.P1 / d.P0 - 1) * 100).toFixed(1)}%)` : "";
    return h("div", { class: "kpi" }, h("span", { class: "big" }, fmt(d.P1, f)),
      delta != null ? h("span", { class: delta < 0 ? "neg" : "pos" }, fmtDelta(delta, f) + rel) : "",
      d.P0 != null ? h("span", { class: "muted" }, `vs ${fmt(d.P0, f)} in the previous period`) : "");
  }

  function lineChart(c) {
    const data = (c.data || []).filter((p) => p.value != null);
    const f = c.y?.format;
    const svg = frame();
    const xs = data.map((p) => p.t);
    const [lo, hi] = niceDomain(data.map((p) => p.value), false);
    const y = yAxis(svg, lo, hi, f);
    const x = (i) => M.l + ((W - M.l - M.r) * i) / Math.max(1, xs.length - 1);
    const xi = (t) => { let i = xs.indexOf(t); if (i < 0) i = xs.findIndex((v) => v >= t); return i < 0 ? xs.length - 1 : i; };
    for (const a of c.annotations || []) {
      if (a.type !== "period_band") continue;
      const x0 = x(xi(a.from)), x1 = x(xi(a.to));
      svg.append(s("rect", { x: x0, y: M.t, width: Math.max(2, x1 - x0), height: H - M.t - M.b, fill: a.label === "P1" ? "var(--s1)" : "var(--s0)", opacity: 0.08 }));
      svg.append(s("text", { x: (x0 + x1) / 2, y: M.t - 4, "text-anchor": "middle" }, a.label === "P1" ? "focus period" : a.label === "P0" ? "comparison" : a.label));
    }
    svg.append(s("path", { d: data.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(""), fill: "none", stroke: "var(--s1)", "stroke-width": 2 }));
    data.forEach((p, i) => { const dot = s("circle", { cx: x(i), cy: y(p.value), r: 2.5, fill: "var(--s1)" }); dot.append(s("title", {}, `${p.t}: ${fmt(p.value, f)}`)); svg.append(dot); });
    for (const a of c.annotations || []) {
      if (a.type !== "point") continue;
      const i = xi(a.x), p = data[i];
      if (!p) continue;
      const g = s("g");
      g.append(s("circle", { cx: x(i), cy: y(p.value), r: 5, fill: "none", stroke: "var(--neg)", "stroke-width": 1.5 }));
      g.append(s("title", {}, `${a.x}: ${a.label}`));
      svg.append(g);
    }
    const step = Math.ceil(xs.length / 7);
    xs.forEach((t, i) => { if (i % step === 0 || i === xs.length - 1) svg.append(s("text", { x: x(i), y: H - M.b + 14, "text-anchor": "middle" }, String(t).slice(5))); });
    const notes = (c.annotations || []).filter((a) => a.type === "point");
    return h("div", {}, svg, notes.length ? h("div", { class: "small muted" }, "Marked: " + notes.map((a) => `${a.x} ${a.label}`).join("; ")) : "");
  }

  function waterfallChart(c) {
    const f = c.y?.format;
    const data = c.data || [];
    const bars = [];
    let cur = 0;
    for (const d of data) {
      if (d.value != null) { bars.push({ label: d.label, a: 0, b: d.value, total: true, v: d.value }); cur = d.value; }
      else { bars.push({ label: d.label, a: cur, b: cur + d.delta, v: d.delta }); cur += d.delta; }
    }
    const levels = bars.flatMap((b) => (b.total ? [b.b] : [b.a, b.b]));
    const [lo, hi] = niceDomain(levels, false);
    const svg = frame();
    const y = yAxis(svg, lo, hi, f);
    const bw = (W - M.l - M.r) / bars.length;
    bars.forEach((b, i) => {
      const x0 = M.l + i * bw + bw * 0.18, w = bw * 0.64;
      const top = y(Math.max(b.total ? b.b : Math.max(b.a, b.b), lo)), bot = y(b.total ? lo : Math.min(b.a, b.b));
      const color = b.total ? "var(--s1)" : b.v < 0 ? "var(--neg)" : "var(--pos)";
      const r = s("rect", { x: x0, y: top, width: w, height: Math.max(1, bot - top), fill: color, rx: 2 });
      r.append(s("title", {}, `${b.label}: ${b.total ? fmt(b.v, f) : fmtDelta(b.v, f)}`));
      svg.append(r);
      svg.append(s("text", { x: x0 + w / 2, y: top - 4, "text-anchor": "middle", style: "fill:var(--ink)" }, b.total ? fmt(b.v, f) : fmtDelta(b.v, f)));
      const lab = b.label.length > 18 ? b.label.slice(0, 17) + "…" : b.label;
      svg.append(s("text", { x: x0 + w / 2, y: H - M.b + 14, "text-anchor": "middle" }, lab));
      if (i < bars.length - 1) svg.append(s("line", { x1: x0 + w, x2: x0 + bw, y1: y(b.b), y2: y(b.b), class: "grid" }));
    });
    return h("div", {}, svg, h("div", { class: "small muted" }, "Bars start at the axis minimum, not at zero, so small effects stay visible."));
  }

  function groupedBar(c) {
    const xf = c.x?.field, yf = c.y?.field, sf = c.series?.field, f = c.y?.format;
    const data = c.data || [];
    const cats = [...new Set(data.map((d) => String(d[xf])))];
    const series = sf ? [...new Set(data.map((d) => d[sf]))] : ["value"];
    const colors = ["var(--s0)", "var(--s1)", "var(--pos)", "var(--neg)"];
    const [lo, hi] = niceDomain(data.map((d) => d[yf]).filter((v) => v != null));
    const svg = frame();
    const y = yAxis(svg, lo, hi, f);
    const bw = (W - M.l - M.r) / cats.length;
    for (const a of c.annotations || []) {
      if (a.type !== "band") continue;
      const i0 = cats.findIndex((k) => +k >= +a.from), i1 = cats.findIndex((k) => +k >= +a.to);
      if (i0 < 0) continue;
      const x0 = M.l + i0 * bw, x1 = M.l + (i1 < 0 ? cats.length : i1) * bw;
      svg.append(s("rect", { x: x0, y: M.t, width: x1 - x0, height: H - M.t - M.b, fill: "var(--neg)", opacity: 0.08 }));
      svg.append(s("text", { x: (x0 + x1) / 2, y: M.t - 4, "text-anchor": "middle", style: "fill:var(--neg)" }, a.label));
    }
    cats.forEach((k, i) => {
      series.forEach((sv, j) => {
        const d = data.find((r) => String(r[xf]) === k && (!sf || r[sf] === sv));
        if (!d || d[yf] == null) return;
        const w = (bw * 0.8) / series.length, x0 = M.l + i * bw + bw * 0.1 + j * w;
        const top = y(d[yf]);
        const r = s("rect", { x: x0, y: top, width: Math.max(1, w - 1), height: Math.max(0.5, y(lo) - top), fill: colors[j % colors.length], rx: 1 });
        r.append(s("title", {}, `${k} · ${sv}: ${fmt(d[yf], f)}`));
        svg.append(r);
      });
      const step = Math.ceil(cats.length / 12);
      if (i % step === 0) svg.append(s("text", { x: M.l + i * bw + bw / 2, y: H - M.b + 14, "text-anchor": "middle" }, k.length > 12 ? k.slice(0, 11) + "…" : k));
    });
    return h("div", {}, svg, sf ? legend(series.map((sv, j) => [sv === "P0" ? "comparison (P0)" : sv === "P1" ? "focus (P1)" : sv, colors[j % colors.length]])) : "");
  }

  function barChart(c) {
    const f = c.x?.format;
    const data = (c.data || []).filter((d) => d.value != null).slice(0, 12);
    const rowH = 22, h0 = M.t + data.length * rowH + 10;
    const svg = frame(h0);
    const L = 150;
    const max = Math.max(...data.map((d) => d.value), 0) || 1;
    const x = (v) => L + ((W - L - 70) * v) / max;
    data.forEach((d, i) => {
      const yy = M.t + i * rowH;
      const lab = String(d.segment);
      svg.append(s("text", { x: L - 6, y: yy + 14, "text-anchor": "end", style: "fill:var(--ink)" }, lab.length > 22 ? lab.slice(0, 21) + "…" : lab));
      const r = s("rect", { x: L, y: yy + 3, width: Math.max(1, x(d.value) - L), height: rowH - 8, fill: d.low_volume ? "var(--s0)" : "var(--s1)", rx: 2 });
      r.append(s("title", {}, `${lab}: ${fmt(d.value, f)}${d.low_volume ? " (low volume)" : ""}`));
      svg.append(r);
      svg.append(s("text", { x: x(d.value) + 6, y: yy + 14 }, fmt(d.value, f)));
    });
    return svg;
  }

  function tableChart(c) {
    const rows = c.data || [];
    if (!rows.length) return h("p", { class: "muted" }, "No rows.");
    const cols = Object.keys(rows[0]).filter((k) => rows.some((r) => r[k] != null));
    return table(cols, rows.map((r) => cols.map((k) => r[k])));
  }

  // ------------------------------------------------------------------ composer
  function autosize() { const t = $("#q"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 140) + "px"; t.style.overflowY = t.scrollHeight > 140 ? "auto" : "hidden"; }
  $("#q").addEventListener("input", autosize);
  window.addEventListener("resize", autosize);
  $("#q").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("#q").value); } });
  $("#ask").addEventListener("submit", (e) => { e.preventDefault(); ask($("#q").value); });

  boot().catch((e) => {
    $("#thread").replaceChildren(h("div", { class: "notice failed" }, `Could not reach the API: ${e.message}`));
  });
})();
