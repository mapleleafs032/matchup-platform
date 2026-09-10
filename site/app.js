/* Shared helpers + landing board. No framework, no build step. Reads only prebuilt JSON. */
const App = (() => {
  let manifest = null;
  const fmt = {
    spread(x) { if (x == null) return "—"; if (x === 0) return "PK"; return (x > 0 ? "+" : "") + (Number.isInteger(x) ? x : x.toFixed(1)); },
    num(x, d = 1) { return x == null ? "—" : Number(x).toFixed(d); },
    pct(x, d = 0) { return x == null ? "—" : (100 * x).toFixed(d) + "%"; },
    kick(iso, tba) {
      if (!iso) return "TBA"; const d = new Date(iso);
      const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      return tba ? d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + " · time TBA" : t;
    },
    day(iso) { const d = new Date(iso); return d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" }); },
    ago(iso) { if (!iso) return "—"; const h = (Date.now() - new Date(iso)) / 36e5; return h < 1 ? Math.round(h * 60) + " min ago" : h < 48 ? h.toFixed(1) + " h ago" : Math.round(h / 24) + " d ago"; },
  };
  async function loadManifest() {
    if (manifest) return manifest;
    const r = await fetch("json/manifest.json", { cache: "no-store" });
    if (!r.ok) throw new Error("manifest missing");
    manifest = await r.json(); return manifest;
  }
  async function loadJSON(path) {
    const m = await loadManifest();
    const r = await fetch(`${path}?v=${m.version}`);
    if (!r.ok) throw new Error(`missing ${path}`);
    return r.json();
  }
  function el(tag, attrs = {}, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) { if (k === "class") e.className = v; else if (k === "html") e.innerHTML = v; else if (v != null) e.setAttribute(k, v); }
    for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(String(k)));
    return e;
  }
  return { fmt, loadManifest, loadJSON, el };
})();

/* ---------------- landing page ---------------- */
async function boardMain() {
  const { fmt, el } = App;
  const root = document.getElementById("board");
  const m = await App.loadManifest();
  const state = { league: localStorage.getItem("league") || (m.leagues.includes("CFB") ? "CFB" : m.leagues[0]), week: null, date: "all", conf: "all", ranked: false, fav: "all", status: "all" };
  const toolbar = document.getElementById("toolbar");
  function render() {
    state.week = state.week || m.current_week[state.league];
    App.loadJSON(m.slates[state.league][String(state.week)]).then(slate => paint(slate)).catch(() => { root.replaceChildren(el("div", { class: "empty" }, "This week's board hasn't been built yet. The site rebuilds every morning and hourly on game days.")); });
    buildToolbar();
  }
  function buildToolbar() {
    toolbar.replaceChildren();
    const seg = el("div", { class: "seg", role: "group", "aria-label": "League" });
    for (const lg of m.leagues) seg.append(el("button", { "aria-pressed": String(lg === state.league), onclick: null }, lg === "CFB" ? "College" : "NFL"));
    [...seg.children].forEach((b, i) => b.addEventListener("click", () => { state.league = m.leagues[i]; localStorage.setItem("league", state.league); state.week = null; state.date = "all"; state.conf = "all"; render(); }));
    toolbar.append(seg);
    const wk = el("select", { "aria-label": "Week" });
    for (const w of m.weeks[state.league]) wk.append(el("option", { value: w, selected: w === state.week ? "" : null }, `Week ${w}`));
    wk.addEventListener("change", () => { state.week = Number(wk.value); state.date = "all"; render(); });
    toolbar.append(el("label", {}, "Week ", wk));
    toolbar.append(el("label", {}, "Date ", el("select", { id: "f-date", "aria-label": "Date" })));
    toolbar.append(el("label", {}, "Conference ", el("select", { id: "f-conf", "aria-label": "Conference" })));
    const fav = el("select", { "aria-label": "Favorite or underdog" }, el("option", { value: "all" }, "Any side"), el("option", { value: "home" }, "Home favored"), el("option", { value: "away" }, "Away favored"));
    fav.value = state.fav; fav.addEventListener("change", () => { state.fav = fav.value; paint(window.__slate); });
    toolbar.append(el("label", {}, "Line ", fav));
    const st = el("select", { "aria-label": "Game status" }, el("option", { value: "all" }, "All games"), el("option", { value: "SCHEDULED" }, "Upcoming"), el("option", { value: "LOCKED" }, "In progress / locked"), el("option", { value: "FINAL" }, "Final"));
    st.value = state.status; st.addEventListener("change", () => { state.status = st.value; paint(window.__slate); });
    App.boardSplitMarket = App.boardSplitMarket || localStorage.getItem("board.splitMarket") || "spread";
    const segS = el("div", { class: "seg", role: "group", "aria-label": "Splits market" });
    for (const [k, lab] of [["spread", "Spread"], ["total", "Total"], ["moneyline", "ML"]]) segS.append(el("button", { "aria-pressed": String(k === App.boardSplitMarket) }, lab));
    [...segS.children].forEach((b, i) => b.addEventListener("click", () => {
      App.boardSplitMarket = ["spread", "total", "moneyline"][i];
      localStorage.setItem("board.splitMarket", App.boardSplitMarket);
      paint(window.__slate);
    }));
    toolbar.append(el("label", {}, "Splits ", segS));
    toolbar.append(el("label", {}, "Status ", st));
    const rk = el("input", { type: "checkbox" }); rk.checked = state.ranked; rk.addEventListener("change", () => { state.ranked = rk.checked; paint(window.__slate); });
    toolbar.append(el("label", {}, rk, state.league === "CFB" ? "Ranked teams only" : "Playoff-caliber (top-8 rated)"));
  }
  function fillSelect(id, values, current, label) {
    const s = document.getElementById(id); if (!s) return;
    s.replaceChildren(el("option", { value: "all" }, label));
    for (const v of values) s.append(el("option", { value: v, selected: v === current ? "" : null }, id === "f-date" ? fmt.day(v + "T12:00:00") : v));
    s.value = current;
  }
  function paint(slate) {
    if (!slate) return; window.__slate = slate;
    const games = slate.games;
    fillSelect("f-date", [...new Set(games.map(g => g.filters.date).filter(Boolean))].sort(), state.date, "All dates");
    document.getElementById("f-date").onchange = e => { state.date = e.target.value; paint(slate); };
    fillSelect("f-conf", [...new Set(games.flatMap(g => [g.filters.conf_home, g.filters.conf_away]).filter(Boolean))].sort(), state.conf, "All conferences");
    document.getElementById("f-conf").onchange = e => { state.conf = e.target.value; paint(slate); };
    const shown = games.filter(g => (state.date === "all" || g.filters.date === state.date) && (state.conf === "all" || g.filters.conf_home === state.conf || g.filters.conf_away === state.conf)
      && (!state.ranked || g.filters.ranked) && (state.fav === "all" || g.filters.favorite === state.fav) && (state.status === "all" || g.status === state.status));
    root.replaceChildren();
    if (!shown.length) { root.append(el("div", { class: "empty" }, "No games match these filters.")); return; }
    let lastDate = null;
    for (const g of shown) {
      if (g.filters.date !== lastDate) {
        lastDate = g.filters.date;
        root.append(el("div", { class: "date-head" }, lastDate ? fmt.day(lastDate + "T12:00:00") : "Date TBA", el("span", {}, `${shown.filter(x => x.filters.date === lastDate).length} games`)));
        root.append(el("div", { class: "col-head" }, el("div", {}, "Matchup"), el("div", {}, "Kickoff"), el("div", {}, "Spread (open)"), el("div", {}, "Total (open)"), el("div", {}, "Bets / Money" + (App.boardSplitMarket && App.boardSplitMarket !== "spread" ? " (" + (App.boardSplitMarket === "total" ? "total" : "ML") + ")" : "")), el("div", {}, "Model score"), el("div", {}, "Win probability"), el("div", {}, "Key edge")));
      }
      root.append(row(g));
    }
    document.getElementById("built").textContent = `Board generated ${fmt.ago(slate.generated_at)}. Lines are the latest snapshot per game; the model is ${games.find(g => g.model)?.model.model_version || "not yet fit"}.`;
  }
  function team(t, side) {
    return el("div", { class: "team" }, t.logo ? el("img", { src: t.logo, alt: "" }) : el("span", { style: "width:22px" }), t.rank ? el("span", { class: "rk" }, "#" + t.rank) : null,
      el("span", { class: "nm" }, t.is_fcs ? t.name + " (FCS)" : t.short || t.name), el("span", { class: "rec" }, t.record));
  }
  function row(g) {
    const mk = g.market || {}, md = g.model;
    const spreadCell = el("div", { class: "two mkt" },
      el("span", { class: "cell num" }, mk.spread_home == null ? "—" : fmt.spread(-mk.spread_home), el("span", { class: "sub" }, mk.spread_open_home == null ? "" : "open " + fmt.spread(-mk.spread_open_home))),
      el("span", { class: "cell num" }, mk.spread_home == null ? "—" : fmt.spread(mk.spread_home), el("span", { class: "sub" }, mk.spread_open_home == null ? "" : "open " + fmt.spread(mk.spread_open_home))));
    const totCell = el("div", { class: "cell num tot" }, mk.total == null ? "—" : fmt.num(mk.total), el("span", { class: "sub" }, mk.total_open == null ? "" : "open " + fmt.num(mk.total_open)));
    const modelCell = md ? el("div", { class: "two model" }, el("span", { class: "cell num" }, md.proj_away.toFixed(0)), el("span", { class: "cell num" }, md.proj_home.toFixed(0), el("span", { class: "sub" }, `spread ${fmt.spread(-md.proj_margin_home)} · total ${md.proj_total.toFixed(0)}`)))
      : el("div", { class: "cell model" }, el("span", { class: "badge warn" }, "No model yet"));
    const wp = md ? el("div", { class: "wp" }, el("span", { class: "num", style: "color:var(--away)" }, fmt.pct(1 - md.win_prob_home)), el("div", { class: "wpbar", title: `Home win probability ${fmt.pct(md.win_prob_home)}` }, el("i", { style: `width:${(100 * md.win_prob_home).toFixed(0)}%` })), el("span", { class: "num", style: "color:var(--home)" }, fmt.pct(md.win_prob_home)))
      : el("div", { class: "wp" }, "—");
    const sp = g.splits, ind = g.indicators || {};
    const splitCell = (() => {
      const mkey = App.boardSplitMarket || "spread";
      if (!sp) return el("div", { class: "cell splits mute" }, "no splits");
      const m = sp[mkey] || {};
      const t = m.ticket_pct_home, mo = m.money_pct_home;
      if (t == null && mo == null) return el("div", { class: "cell splits mute" }, "no splits");
      const box = el("div", { class: "cell splits" },
        App.splitsPair({ ticket: t, money: mo, market: mkey, homeAbbr: g.home.abbr, awayAbbr: g.away.abbr }));
      const disagree = t != null && mo != null && (t >= 0.5) !== (mo >= 0.5);
      const gap = t != null && mo != null ? Math.abs(t - mo) * 100 : 0;
      const marks = [];
      if (disagree) marks.push("money and tickets on opposite sides");
      else if (gap >= 12) marks.push(`${gap.toFixed(0)} point gap`);
      if ((ind.rlm || {})[mkey]) marks.push("RLM toward " + ind.rlm[mkey]);
      if ((ind.lopsided || {})[mkey]) marks.push("lopsided on " + ind.lopsided[mkey]);
      if ((ind.steam || []).includes(mkey)) marks.push("steam");
      if (marks.length) box.append(el("span", { class: "sp-flag" }, marks[0]));
      box.setAttribute("title", `${sp.book || "splits"} · ${sp.n} snapshot${sp.n === 1 ? "" : "s"}${marks.length ? " · " + marks.join(" · ") : ""}`);
      return box;
    })();
    const ke = g.key_edge ? el("div", { class: "edge-chip" }, el("span", { class: "badge " + g.key_edge.side }, g.key_edge.side === "home" ? g.home.abbr : g.away.abbr), " ", g.key_edge.label, " ", el("b", {}, Math.abs(g.key_edge.points).toFixed(1) + " pts"))
      : el("div", { class: "edge-chip" }, el("span", { class: "badge mute" }, "Edges not built"));
    const when = g.result ? el("div", { class: "when" }, el("span", { class: "final" }, `Final ${g.result.away}–${g.result.home}`), g.result.model_ats ? el("span", { class: "tv" }, `model vs spread: ${g.result.model_ats}`) : null)
      : el("div", { class: "when" }, fmt.kick(g.kickoff_utc, g.kickoff_is_tba), el("span", { class: "tv" }, [g.tv, g.venue && g.venue.city, g.neutral_site ? "neutral site" : null].filter(Boolean).join(" · ")));
    return el("a", { class: "row", href: `matchup.html?g=${g.game_id}` }, el("div", { class: "teams" }, team(g.away, "away"), team(g.home, "home")), when, spreadCell, totCell, splitCell, modelCell, wp, ke);
  }
  render();
}

/* Market indicator chips, shared by the odds tab and the matchup page. */
App.indicatorChips = function (state, market) {
  const el = App.el, out = [];
  if (!state) return out;
  const key = market === "moneyline" ? "spread" : (market || "spread");
  const act = (state.rlm_active || {})[key];
  const past = (state.rlm_ever || {})[key];
  if (act) {
    out.push(el("span", { class: "badge warn", title: `Moved ${Math.abs(act.move).toFixed(1)} toward ${act.toward} while ${(act.ticket_pct_majority * 100).toFixed(0)}% of tickets sat the other way` },
      `reverse line movement toward ${act.toward}`));
  } else if (past) {
    out.push(el("span", { class: "badge mute", title: past.detail || "" }, "reverse movement earlier, since rebounded"));
  }
  const lop = (state.lopsided || {})[key];
  if (lop) out.push(el("span", { class: "badge warn" }, `lopsided on ${lop}`));
  const steam = (state.events || []).filter(e => e.kind === "steam" && e.market === key);
  if (steam.length) {
    const last = steam[steam.length - 1];
    out.push(el("span", { class: "badge sig", title: last.detail }, `steam toward ${last.toward}`));
  }
  const kn = (state.events || []).filter(e => e.kind === "key_number" && e.market === key);
  if (kn.length) out.push(el("span", { class: "badge mute", title: kn[kn.length - 1].detail }, `crossed ${kn[kn.length - 1].key}`));
  const mv = (state.recent_move || {})[key];
  if (mv != null && Math.abs(mv) >= 0.5) {
    out.push(el("span", { class: "badge mute" }, `${Math.abs(mv).toFixed(1)} move in the last ${state.window_hours || 36}h`));
  }
  return out;
};

/* ------------------------------------------------------------------
   Market chart: the line for both sides over time, with event marks.
   Hover a dot for the number, the time, and the ticket/money split.
   Shared by the Odds tab and the matchup page.
   opts: { lineSeries, splitsSeries, events, market, homeAbbr, awayAbbr, book }
------------------------------------------------------------------- */
App.marketChart = function (opts) {
  const el = App.el;
  const market = opts.market || "spread";
  const home = opts.homeAbbr || "HOME", away = opts.awayAbbr || "AWAY";
  const splits = (opts.splitsSeries || []).slice().sort((a, b) => new Date(a.t) - new Date(b.t));
  /* One book only. The splits source carries its own line at every snapshot, so the chart shows the
     number people were actually betting into. Falling back to the odds feed, a single book is picked
     rather than every book at once, which would look like the line was oscillating when it was not. */
  const fromSplits = splits.filter(p => p.line_spread_home != null || p.line_total != null || p.line_ml_home != null)
    .map(p => ({ t: p.t, book: p.book, spread_home: p.line_spread_home, total: p.line_total, ml_home: p.line_ml_home, ml_away: p.line_ml_away }));
  let line = fromSplits;
  let bookUsed = fromSplits.length ? (splits[0] || {}).book : null;
  if (!line.length) {
    const raw = (opts.lineSeries || []).filter(p => p && p.t);
    const counts = {};
    for (const p of raw) counts[p.book] = (counts[p.book] || 0) + 1;
    const prefer = ["draftkings", "consensus", "fanduel", "betmgm", "caesars", "espnbet", "bovada"];
    bookUsed = prefer.find(b => counts[b]) || Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0] || null;
    line = raw.filter(p => p.book === bookUsed);
  }
  line = line.slice().sort((a, b) => new Date(a.t) - new Date(b.t));
  const events = (opts.events || []).filter(e => e.market === (market === "moneyline" ? "spread" : market));

  // the two plotted series, per market
  const spec = {
    spread: { a: p => (p.spread_home == null ? null : -p.spread_home), b: p => (p.spread_home == null ? null : p.spread_home),
              fmt: v => (v > 0 ? "+" : "") + v.toFixed(1), aLab: away, bLab: home, splitKey: "spread" },
    total: { a: p => (p.total == null ? null : p.total), b: () => null,
             fmt: v => v.toFixed(1), aLab: "total", bLab: null, splitKey: "total" },
    moneyline: { a: p => (p.ml_away == null ? null : p.ml_away), b: p => (p.ml_home == null ? null : p.ml_home),
                 fmt: v => (v > 0 ? "+" : "") + Math.round(v), aLab: away, bLab: home, splitKey: "moneyline" },
  }[market];

  const pts = line.filter(p => spec.a(p) != null || (spec.b && spec.b(p) != null));
  if (pts.length < 1) return el("p", { class: "sub-note" }, "No line history recorded for this market yet.");

  // nearest ticket/money split at or before each point, for the hover
  const splitAt = (t) => {
    let found = null;
    for (const s of splits) { if (new Date(s.t) <= new Date(t)) found = s; else break; }
    return found;
  };
  const pctTxt = (s, side) => {
    if (!s) return "no splits recorded";
    const tk = s[`${spec.splitKey}_ticket`], mn = s[`${spec.splitKey}_money`];
    if (tk == null && mn == null) return "no splits recorded";
    const v = x => (x == null ? "?" : Math.round((side === "home" ? x : 1 - x) * 100) + "%");
    return `Bet ${v(tk)} · Money ${v(mn)}`;
  };

  const W = 860, H = 260, L = 54, R = 18, T = 34, B = 34;
  const ts = pts.map(p => new Date(p.t).getTime());
  const t0 = Math.min(...ts), t1 = Math.max(...ts), span = Math.max(t1 - t0, 1);
  const x = t => L + ((t - t0) / span) * (W - L - R);
  const vals = [];
  for (const p of pts) { const a = spec.a(p), b = spec.b ? spec.b(p) : null; if (a != null) vals.push(a); if (b != null) vals.push(b); }
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.22;
  lo -= pad; hi += pad;
  const y = v => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);

  const out = [];
  const ticks = 5;
  for (let i = 0; i < ticks; i++) {
    const v = lo + (hi - lo) * (i / (ticks - 1));
    out.push(`<line x1="${L}" y1="${y(v).toFixed(1)}" x2="${W - R}" y2="${y(v).toFixed(1)}" stroke="var(--rule)" opacity=".6"/>`);
    out.push(`<text x="${L - 8}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" font-size="11" fill="var(--mute)" class="num">${spec.fmt(v)}</text>`);
  }

  // event marks: dashed verticals with a label at the top
  const EV = { line_move: { c: "var(--mute)", lab: "" }, steam: { c: "var(--steam)", lab: "STEAM" },
               rlm: { c: "var(--rlm)", lab: "RLM" }, divergence: { c: "var(--split)", lab: "$" },
               key_number: { c: "var(--mute)", lab: "KEY" }, lopsided: { c: "var(--split)", lab: "LOP" } };
  const placed = [];
  for (const e of events) {
    const t = new Date(e.t).getTime();
    if (t < t0 || t > t1) continue;
    const cfg = EV[e.kind] || EV.line_move;
    const xx = x(t);
    out.push(`<line x1="${xx.toFixed(1)}" y1="${T - 8}" x2="${xx.toFixed(1)}" y2="${H - B}" stroke="${cfg.c}" stroke-width="1.2" stroke-dasharray="4 4" opacity=".9"><title>${new Date(e.t).toLocaleString()} — ${e.kind.replace("_", " ")}: ${e.detail}</title></line>`);
    if (cfg.lab && !placed.some(px => Math.abs(px - xx) < 26)) {
      placed.push(xx);
      out.push(`<text x="${xx.toFixed(1)}" y="${T - 14}" text-anchor="middle" font-size="10" font-weight="700" fill="${cfg.c}">${cfg.lab}<title>${e.detail}</title></text>`);
    }
  }

  // the two series
  const draw = (getter, colour, sideKey, label) => {
    const seg = pts.filter(p => getter(p) != null);
    if (!seg.length) return;
    out.push(`<path d="${seg.map((p, i) => `${i ? "L" : "M"}${x(new Date(p.t).getTime()).toFixed(1)},${y(getter(p)).toFixed(1)}`).join(" ")}" fill="none" stroke="${colour}" stroke-width="2"/>`);
    for (const p of seg) {
      const t = new Date(p.t).getTime();
      const sp = splitAt(p.t);
      const tip = `${label} ${spec.fmt(getter(p))} · ${new Date(p.t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} · ${pctTxt(sp, sideKey)}${p.book ? " · " + p.book : ""}`;
      out.push(`<circle cx="${x(t).toFixed(1)}" cy="${y(getter(p)).toFixed(1)}" r="3.4" fill="${colour}"><title>${tip}</title></circle>`);
    }
  };
  draw(spec.a, "var(--away)", "away", spec.aLab);
  if (spec.b) draw(spec.b, "var(--home)", "home", spec.bLab);

  // team labels, top left
  out.push(`<text x="${L}" y="${T - 14}" font-size="12" font-weight="700" fill="var(--away)">${spec.aLab}</text>`);
  if (spec.bLab) out.push(`<text x="${L + 46}" y="${T - 14}" font-size="12" font-weight="700" fill="var(--home)">${spec.bLab}</text>`);
  const fmtT = t => new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  out.push(`<text x="${L}" y="${H - 10}" font-size="11" fill="var(--mute)">${fmtT(t0)}</text>`);
  out.push(`<text x="${W - R}" y="${H - 10}" text-anchor="end" font-size="11" fill="var(--mute)">${fmtT(t1)}</text>`);

  const wrap = el("div", { class: "mchart" });
  wrap.append(el("div", { html: `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Line movement with market events">${out.join("")}</svg>` }));
  wrap.append(el("p", { class: "mchart-key" },
    "Hover a dot for the number, time and the ticket/money split. Dashed marks: ",
    el("b", { style: "color:var(--mute)" }, "grey"), " line move, ",
    el("b", { style: "color:var(--steam)" }, "STEAM"), " fast move, ",
    el("b", { style: "color:var(--rlm)" }, "RLM"), " moved against the crowd, ",
    el("b", { style: "color:var(--split)" }, "$"), " tickets and money split.",
    bookUsed ? ` Prices from ${bookUsed === "draftkings" ? "DraftKings" : bookUsed}.` : ""));
  return wrap;
};

/* Spread / Total / Moneyline toggle bound to a redraw callback. */
App.marketToggle = function (current, onChange) {
  const el = App.el;
  const seg = el("div", { class: "seg", role: "group", "aria-label": "Market" });
  const keys = ["spread", "total", "moneyline"];
  for (const [i, lab] of ["Spread", "Total", "Moneyline"].entries()) {
    seg.append(el("button", { "aria-pressed": String(keys[i] === current) }, lab));
  }
  [...seg.children].forEach((b, i) => b.addEventListener("click", () => onChange(keys[i])));
  return seg;
};

/* ------------------------------------------------------------------
   Ticket and money shares for BOTH sides of a market, one format used
   everywhere (board and Odds tab):

       Bets:   SEA = 65% / ARI = 35%
       Money:  SEA = 40% / ARI = 60%

   For totals the two sides are Over and Under. The majority side of each
   line is emphasised, and when bets and money favour opposite sides the
   emphasised figures turn red, because that disagreement is the signal.
   opts: { ticket, money, labelA, labelB, market, homeAbbr, awayAbbr }
------------------------------------------------------------------- */
App.splitsPair = function (opts) {
  const el = App.el;
  const t = opts.ticket == null ? null : Number(opts.ticket);
  const m = opts.money == null ? null : Number(opts.money);
  const labA = opts.labelA || (opts.market === "total" ? "Over" : opts.homeAbbr || "Home");
  const labB = opts.labelB || (opts.market === "total" ? "Under" : opts.awayAbbr || "Away");
  const disagree = t != null && m != null && (t >= 0.5) !== (m >= 0.5);
  const line = (label, v) => {
    if (v == null) return el("span", { class: "sp-line" }, el("span", { class: "k" }, label), el("span", { class: "sp-val" }, "—"));
    const a = Math.round(v * 100), b = 100 - a;
    return el("span", { class: "sp-line" }, el("span", { class: "k" }, label),
      el("span", { class: "sp-val" },
        el("span", { class: a >= b ? "hi" : "" }, `${labA} = ${a}%`),
        el("span", { class: "sep" }, " / "),
        el("span", { class: b > a ? "hi" : "" }, `${labB} = ${b}%`)));
  };
  return el("div", { class: "splits-pair" + (disagree ? " disagree" : "") }, line("Bets:", t), line("Money:", m));
};
