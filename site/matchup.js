async function matchupMain() {
  const { fmt, el } = App;
  const gid = new URLSearchParams(location.search).get("g");
  const root = document.getElementById("matchup");
  if (!gid) { root.replaceChildren(el("div", { class: "empty" }, "No game selected. Go back to the board and pick a matchup.")); return; }
  let d;
  try { d = await App.loadJSON(`json/matchup/${gid}.json`); } catch { root.replaceChildren(el("div", { class: "empty" }, "This matchup page hasn't been built yet. Pages are generated every morning and hourly on game days.")); return; }
  document.title = `${d.game.away.short} at ${d.game.home.short} — Matchup`;
  const A = d.teams.away, H = d.teams.home, g = d.game, md = d.model, mk = d.market;
  const abbrA = g.away.abbr, abbrH = g.home.abbr;
  root.replaceChildren();

  // ---- hero
  const side = (t, cls) => el("div", { class: "side " + cls }, t.identity.logo ? el("img", { src: t.identity.logo, alt: "" }) : null,
    el("div", {}, el("h1", {}, t.identity.rank ? `#${t.identity.rank} ` : "", t.identity.name), el("div", { class: "meta" }, `${t.identity.record}${t.identity.conf ? " · " + t.identity.conf : ""} · ${t.games_n} games of data`)));
  const spine = el("div", { class: "spine" }, el("div", { class: "kick" }, d.result ? `Final` : fmt.kick(g.kickoff_utc, g.kickoff_is_tba), " · ", (g.venue && g.venue.name) || "", g.neutral_site ? " (neutral)" : ""));
  if (d.result) spine.append(el("div", { class: "score" }, `${d.result.away} – ${d.result.home}`, el("small", {}, md ? `model had ${md.proj_away.toFixed(0)} – ${md.proj_home.toFixed(0)}` : "")));
  else if (md) spine.append(el("div", { class: "score" }, `${md.proj_away.toFixed(0)} – ${md.proj_home.toFixed(0)}`, el("small", {}, "model projection")));
  else spine.append(el("div", { class: "score" }, "—", el("small", {}, "no projection yet")));
  const t = el("table", {});
  const mkSpread = mk && mk.current ? mk.current.spread_home : (md ? md.market_spread_home : null);
  const mkOpen = mk && mk.open ? mk.open.spread_home : null;
  const rowT = (k, v) => t.append(el("tr", {}, el("td", {}, k), el("td", {}, v)));
  rowT("Market spread", mkSpread == null ? "unavailable" : `${abbrH} ${fmt.spread(mkSpread)}${mkOpen != null ? ` (open ${fmt.spread(mkOpen)})` : ""}`);
  if (md) { rowT("Model spread", `${abbrH} ${fmt.spread(-md.proj_margin_home)}`); rowT("Difference", md.spread_diff == null ? "—" : `${Math.abs(md.spread_diff).toFixed(1)} toward ${md.spread_diff > 0 ? abbrH : abbrA}`); rowT("Win probability", `${abbrA} ${fmt.pct(1 - md.win_prob_home)} · ${abbrH} ${fmt.pct(md.win_prob_home)}`); }
  rowT("Total", `${mk && mk.current && mk.current.total != null ? "market " + fmt.num(mk.current.total) : "market unavailable"}${md ? " · model " + md.proj_total.toFixed(1) : ""}`);
  spine.append(t);
  if (d.frozen_from_snapshot) spine.append(el("div", { style: "margin-top:6px" }, el("span", { class: "badge mute" }, `Pregame record frozen at kickoff`)));
  root.append(el("div", { class: "hero" }, side(A, "away"), spine, side(H, "home")));

  // ---- edges (diverging chart) + why
  const sec = (title, note, ...kids) => { const s = el("section", { class: "block" }, el("h2", {}, title)); if (note) s.append(el("p", { class: "sub-note" }, note)); s.append(...kids); root.append(s); return s; };
  const avail = d.edges.filter(e => !e.unavailable && e.points_home != null);
  const maxPts = Math.max(1.5, ...avail.map(e => Math.abs(e.points_home)));
  const state = { window: d.metrics.default_window, adj: "OPP_ADJ" };

  /* ---- quick look: the stats worth seeing first, value + national rank, with an edge tally ---- */
  const qlAdj = { v: "OPP_ADJ" };
  const qlWrap = el("div", { class: "ql-wrap" });
  const qlToggle = el("div", { class: "seg", role: "group", "aria-label": "Quick look basis" });
  for (const [k, lab] of [["OPP_ADJ", "Opponent-adjusted"], ["RAW", "Raw"]]) {
    const b = el("button", { "aria-pressed": String(k === qlAdj.v) }, lab);
    b.addEventListener("click", () => { qlAdj.v = k; paintQuick(); });
    qlToggle.append(b);
  }
  function fmtQL(v, unit) {
    if (v == null) return "—";
    if (unit === "pct") return (v * 100).toFixed(2) + "%";
    if (unit === "rank") return "#" + v;
    if (unit === "qbr") return v.toFixed(1);
    return Math.abs(v) >= 50 ? v.toFixed(0) : v.toFixed(1);
  }
  function paintQuick() {
    [...qlToggle.children].forEach(b => b.setAttribute("aria-pressed", String((b.textContent === "Raw") === (qlAdj.v === "RAW"))));
    const q = (d.quick_look || {})[qlAdj.v];
    qlWrap.replaceChildren();
    if (!q || !q.rows || !q.rows.length) { qlWrap.append(el("p", { class: "note" }, "Quick look is unavailable for this game.")); return; }
    const aAbbr = d.game.away.abbr, hAbbr = d.game.home.abbr;
    const tbl = el("table", { class: "ql" });
    tbl.append(el("tr", { class: "ql-head" },
      el("th", {}, "Metric"),
      el("th", { colspan: "2" }, d.game.away.short || d.game.away.name),
      el("th", { colspan: "2" }, d.game.home.short || d.game.home.name),
      el("th", {}, "Edge")));
    for (const r of q.rows) {
      const side = r.edge === "home" ? (d.game.home.short || hAbbr) : r.edge === "away" ? (d.game.away.short || aAbbr) : "";
      tbl.append(el("tr", { class: "g-" + r.group.toLowerCase() },
        el("th", { scope: "row" }, r.label),
        el("td", { class: "num" }, fmtQL(r.away.v, r.unit)),
        el("td", { class: "num rk" }, r.away.rank == null ? "" : String(r.away.rank)),
        el("td", { class: "num" }, fmtQL(r.home.v, r.unit)),
        el("td", { class: "num rk" }, r.home.rank == null ? "" : String(r.home.rank)),
        el("td", { class: "ql-edge " + (r.edge || "") }, side)));
    }
    const w = q.winner;
    tbl.append(el("tr", { class: "ql-total" },
      el("th", { scope: "row" }, "Edge Count"),
      el("td", { class: "num", colspan: "2" }, String(q.edge_count.away)),
      el("td", { class: "num", colspan: "2" }, String(q.edge_count.home)),
      el("td", { class: "ql-edge " + (w || "") }, w === "home" ? (d.game.home.short || hAbbr) : w === "away" ? (d.game.away.short || aAbbr) : "even")));
    qlWrap.append(tbl);
    qlWrap.append(el("p", { class: "note" }, q.edge_rule + " Ranks are among all teams in the league this week; blank means the metric has no rank."));
  }
  paintQuick();
  sec("Quick look", el("div", {}, el("div", { class: "toolbar" }, el("label", {}, "Basis ", qlToggle)), qlWrap));
  const tabs = el("div", { class: "tabs" });
  for (const w of d.metrics.windows) { const b = el("button", { "aria-pressed": String(w === state.window) }, ({ SEASON: "Season", LAST5: "Last 5", LAST3: "Last 3", HOME: "Home", AWAY: "Away", CONF: "Conference" })[w] || w); b.addEventListener("click", () => { state.window = w; paintGrid(); }); tabs.append(b); }
  const adjTabs = el("div", { class: "tabs" });
  for (const [k, lab] of [["OPP_ADJ", "Opponent-adjusted"], ["RAW", "Raw"]]) { const b = el("button", { "aria-pressed": String(k === state.adj) }, lab); b.addEventListener("click", () => { state.adj = k; paintGrid(); }); adjTabs.append(b); }
  const gridWrap = el("div", {});
  sec("Team comparison", "Value, national rank, and percentile for each team. Opponent-adjusted values account for who each team has played; early in the season they blend in last year's adjusted numbers (shown by the data-quality flags above). Tap a metric name for its definition.", tabs, adjTabs, gridWrap);
  const edgesEl = el("div", { class: "edges" });
  for (const e of d.edges.sort((x, y) => Math.abs(y.points_home || 0) - Math.abs(x.points_home || 0))) {
    if (e.unavailable) { edgesEl.append(el("div", { class: "edge" }, el("div", { class: "lab na" }, e.label), el("div", { class: "bar" }), el("div", { class: "pts" }, el("span", { class: "badge warn" }, "n/a")))); continue; }
    const p = e.points_home, w = (50 * Math.abs(p) / maxPts).toFixed(1) + "%";
    edgesEl.append(el("div", { class: "edge", title: explainEdge(e) }, el("div", { class: "lab" }, e.label), el("div", { class: "bar" }, el("i", { class: p >= 0 ? "h" : "a", style: `width:${w}` })),
      el("div", { class: "pts " + (p >= 0 ? "h" : "a") }, (p >= 0 ? "+" : "−") + Math.abs(p).toFixed(1), el("span", { style: "font-weight:400;font-size:12px;color:var(--mute)" }, ` ${e.score > 0 ? "+" : ""}${e.score}`))));
  }
  sec("Where the edges are", `Each bar is that category's contribution to the projected margin, in points, from the fitted model. Bars reach toward the team they favor; the small number is the −3…+3 edge score. "n/a" means the inputs are unavailable for this game, not that the category is neutral.`,
    el("div", { class: "legend" }, el("span", {}, el("i", { style: "background:var(--away)" }), abbrA), el("span", {}, el("i", { style: "background:var(--home)" }), abbrH)), edgesEl);
  if (md && md.why && md.why.length) {
    const why = el("div", { class: "why" });
    for (const w of md.why.slice(0, 12)) why.append(el("div", {}, el("span", {}, labelFor(w.category)), el("b", { style: `color:${w.points >= 0 ? "var(--home)" : "var(--away)"}` }, `${w.points >= 0 ? "+" : ""}${w.points.toFixed(1)}`)));
    sec("Why the projection", `Projected margin ${abbrH} ${fmt.spread(-md.proj_margin_home)} (${md.model_version}, ${fmt.ago(md.predicted_at)}). Largest contributors in points toward the home team; league-average baseline plus these adds up to the projection.`, why,
      el("p", { class: "sub-note", style: "margin-top:8px" }, `Data quality ${fmt.pct(md.data_quality)}${md.quality_flags ? " · " + md.quality_flags : ""}${d.metrics.quality_flags.length ? " · " + d.metrics.quality_flags.join(", ").toLowerCase() : ""}`));
  }

  // ---- comparison grid
  function paintGrid() {
    [...tabs.children].forEach(b => b.setAttribute("aria-pressed", String(b.textContent === ({ SEASON: "Season", LAST5: "Last 5", LAST3: "Last 3", HOME: "Home", AWAY: "Away", CONF: "Conference" })[state.window])));
    [...adjTabs.children].forEach(b => b.setAttribute("aria-pressed", String((b.textContent === "Raw") === (state.adj === "RAW"))));
    const key = `${state.window}:${state.adj}`;
    const tbl = el("table", { class: "grid" }, el("thead", {}, el("tr", {}, el("th", {}, "Metric"), el("th", { class: "away" }, abbrA), el("th", { class: "home" }, abbrH), el("th", {}, "Edge"))));
    const tb = el("tbody", {}); let group = null;
    for (const r of d.metrics.rows) {
      if (r.group !== group) { group = r.group; tb.append(el("tr", { class: "group" }, el("td", { colspan: 4 }, group))); }
      const a = r.away[key], h = r.home[key];
      const better = (x, y) => x && y && x.v !== y.v ? ((x.v > y.v) === r.higher_is_better ? "a" : "h") : null;
      const b = better(a, h);
      const cell = (c, sideCls) => c == null ? el("td", { class: "v" }, el("span", { class: "badge mute" }, "n/a")) : el("td", { class: "v" + (b === sideCls ? " better" : "") }, fmtMetric(c.v, r.unit), el("span", { class: "rk" }, c.rank ? `#${c.rank}` : "", c.low_n ? " · low sample" : ""));
      const edge = !a || !h ? "" : ((r.higher_is_better ? h.v - a.v : a.v - h.v) === 0 ? "even" : "");
      const pctGap = a && h && a.pct != null && h.pct != null ? (h.pct - a.pct) * (r.higher_is_better ? 1 : -1) : null;
      const edgeCell = el("td", { class: "edge-cell" }, pctGap == null ? "" : el("span", { class: pctGap >= 0 ? "h" : "a" }, `${pctGap >= 0 ? abbrH : abbrA} ${(Math.abs(pctGap) * 100).toFixed(0)}`));
      const btn = el("button", { type: "button", "aria-expanded": "false" }, r.label);
      const tr = el("tr", {}, el("td", { class: "metric" }, btn), cell(a, "a"), cell(h, "h"), edgeCell);
      const desc = el("tr", { class: "desc-row", hidden: "" }, el("td", { colspan: 4 }, r.description));
      btn.addEventListener("click", () => { const open = desc.hasAttribute("hidden"); if (open) desc.removeAttribute("hidden"); else desc.setAttribute("hidden", ""); btn.setAttribute("aria-expanded", String(open)); });
      tb.append(tr, desc);
    }
    tbl.append(tb); gridWrap.replaceChildren(tbl, el("p", { class: "sub-note", style: "margin-top:8px" }, "Edge column: percentile gap in favor of the named team (0–100). Percentiles are within league for the selected window."));
  }
  paintGrid();

  // ---- QB / injuries / weather / rest
  const qbPanel = (tm, cls) => { const q = tm.qb; const p = el("div", { class: "panel " + cls }, el("h3", {}, tm.identity.abbr + " quarterback")); if (!q || !q.name) { p.append(el("span", { class: "badge warn" }, "Insufficient reliable data on the starter")); return p; }
    const dl = el("dl", { class: "kv" }); const kv = (k, v) => dl.append(el("dt", {}, k), el("dd", {}, v));
    kv("Projected starter", q.name); kv("Basis", ({ last_game_starter: "started last game", depth_chart: "depth chart", prior_season_starter: "returning starter", transfer_prior_production: "transfer with prior starts", injury_replacement: "injury replacement", UNKNOWN: "unknown" })[q.basis] || q.basis);
    kv("Confidence", fmt.pct(q.confidence)); kv("Career (2021+)", q.career_att ? `${q.career_games} games with 10+ att · ${fmt.num(q.career_ypa, 1)} yds/att · ${fmt.pct(q.career_cmp_pct)} · ${q.career_td} TD / ${q.career_int} INT` : "no games in our tables");
    if (q.career_epa_dropback != null) kv("EPA per dropback", fmt.num(q.career_epa_dropback, 3)); if (q.season_att) kv("This season", `${q.season_att} attempts`);
    p.append(dl); const flags = (q.flags || []).filter(f => f && f !== "nan"); if (flags.length) p.append(el("div", { style: "margin-top:6px" }, ...flags.map(f => el("span", { class: "badge " + (f.includes("INJUR") || f.includes("UNKNOWN") ? "warn" : "mute"), style: "margin-right:4px" }, f.toLowerCase().replace(/_/g, " "))))); return p; };
  const injPanel = (tm, cls) => { const p = el("div", { class: "panel " + cls }, el("h3", {}, tm.identity.abbr + " injuries")); if (!tm.injuries.length) { p.append(el("span", { class: "badge mute" }, d.sources.injuries === "manual entries" ? "No manual entries — no official CFB report exists" : "No designations")); return p; }
    const ul = el("ul", { class: "plain" }); for (const i of tm.injuries) ul.append(el("li", {}, `${i.player || "?"} (${i.position || "?"}) — `, el("span", { class: "badge " + (i.status === "OUT" || i.status === "IR" ? "bad" : "warn") }, i.status.toLowerCase()), i.desc ? ` ${i.desc}` : "", i.source === "manual" ? el("span", { class: "badge mute", style: "margin-left:6px" }, "manual") : null)); p.append(ul); return p; };
  sec("Quarterbacks", null, el("div", { class: "duo" }, qbPanel(A, "away"), qbPanel(H, "home")));
  sec("Injuries", null, el("div", { class: "duo" }, injPanel(A, "away"), injPanel(H, "home")));
  const wx = d.weather; const restE = d.edges.find(e => e.category === "REST");
  const wxText = !wx ? "Forecast unavailable." : wx.is_indoor ? "Indoor venue — weather does not apply." : `${fmt.num(wx.temp_f, 0)}°F, wind ${fmt.num(wx.wind_mph, 0)} mph${wx.wind_gust_mph ? ` (gusts ${fmt.num(wx.wind_gust_mph, 0)})` : ""}, precipitation chance ${fmt.pct(wx.precip_prob)}. Forecast retrieved ${fmt.ago(wx.retrieved_at)}${wx.source === "open_meteo_archive" ? " (archived actual)" : ""}.`;
  const restText = restE && restE.inputs && restE.inputs.home ? `${abbrA}: ${restE.inputs.away.first_game ? "first game" : restE.inputs.away.rest_days + " days rest"}${restE.inputs.away.short_week ? ", short week" : ""}${restE.inputs.away.off_bye ? ", off a bye" : ""}${restE.inputs.away.consecutive_road_before ? `, ${restE.inputs.away.consecutive_road_before} straight road games before this` : ""}. ${abbrH}: ${restE.inputs.home.first_game ? "first game" : restE.inputs.home.rest_days + " days rest"}${restE.inputs.home.short_week ? ", short week" : ""}${restE.inputs.home.off_bye ? ", off a bye" : ""}.` : "Rest context unavailable.";
  sec("Weather and rest", null, el("p", { style: "margin:0 0 6px;font-size:15px" }, wxText), el("p", { style: "margin:0;font-size:15px" }, restText));

  // ---- market
  const mkSec = sec("Market", mk && mk.available ? `${mk.n_snapshots} snapshots, last ${fmt.ago(mk.last_snapshot)}, primary book ${mk.primary_book}.` : "No market history captured for this game yet.");
  if (mk && mk.available) {
    const notes = el("ul", { class: "market-notes" }); for (const n of mk.notes || []) notes.append(el("li", {}, n));
    mkSec.append(notes);
    const chart = el("svg", { class: "chart", viewBox: "0 0 800 190", preserveAspectRatio: "none", role: "img", "aria-label": "Spread movement over time" });
    mkSec.append(chart);
    App.loadJSON(d.market_history_url).then(hist => drawChart(chart, hist.series || [], abbrH)).catch(() => chart.replaceWith(el("p", { class: "sub-note" }, "Line history not available.")));
    if (mk.model_vs_market) { const v = mk.model_vs_market; mkSec.append(el("p", { class: "sub-note", style: "margin-top:8px" }, `Model win probability ${fmt.pct(v.model_win_prob_home)} vs market no-vig ${v.market_win_prob_home == null ? "unavailable" : fmt.pct(v.market_win_prob_home)} for ${abbrH}. ${mk.public_note || ""}`)); }
  }

  // ---- betting splits and market indicators
  const spFull = (d.splits || {}).FULL || { available: false };
  const mstate = d.market_state || { events: [], rlm_active: {}, rlm_ever: {}, lopsided: {}, recent_move: {} };
  const spSec = sec("Betting splits and market signals",
    spFull.available ? `${spFull.n_snapshots} snapshot${spFull.n_snapshots === 1 ? "" : "s"} of DraftKings ticket and money shares, last ${fmt.ago(spFull.last_snapshot)}.`
                     : "No betting splits recorded for this game yet.");
  if (spFull.available) {
    const SP = { market: "spread", perspective: "home", metric: "both" };
    const bar = el("div", { class: "toolbar sp-toolbar" });
    const segM = el("div", { class: "seg", role: "group", "aria-label": "Market" });
    for (const [k, lab] of [["spread", "Spread"], ["total", "Total"], ["moneyline", "Moneyline"]]) segM.append(el("button", { "aria-pressed": String(k === SP.market) }, lab));
    [...segM.children].forEach((b, i) => b.addEventListener("click", () => { SP.market = ["spread", "total", "moneyline"][i]; redraw(); }));
    bar.append(el("label", {}, "Market ", segM));

    spSec.append(bar);
    const chips = el("div", { class: "og-chips" });
    spSec.append(chips);
    const spChart = el("div", { class: "og-chart" });
    spSec.append(spChart);
    const spNotes = el("ul", { class: "market-notes" });
    spSec.append(spNotes);
    const evList = el("ul", { class: "market-notes evt" });
    spSec.append(evList);
    const redraw = () => {
      const key = SP.market === "moneyline" ? "spread" : SP.market;
      chips.replaceChildren();
      for (const c of App.indicatorChips(mstate, SP.market)) chips.append(c);
      [...segM.children].forEach((b, i) => b.setAttribute("aria-pressed", String(["spread", "total", "moneyline"][i] === SP.market)));
      spChart.replaceChildren(splitsChart(spFull, mstate, SP));
      spNotes.replaceChildren();
      for (const n of spFull.notes || []) spNotes.append(el("li", {}, n));
      evList.replaceChildren();
      const evs = (mstate.events || []).filter(e => e.market === key);
      if (evs.length) {
        evList.append(el("li", {}, el("b", {}, "What moved, and when:")));
        for (const e of evs.slice(-8)) evList.append(el("li", {}, `${fmt.kick(e.t, false)} — ${e.kind.replace("_", " ")}: ${e.detail}`));
      }
    };
    redraw();
  }

  function splitsChart(sp, mstate, SP) {
    return App.marketChart({
      lineSeries: (d.market && d.market.series) || [],
      splitsSeries: sp.series || [],
      events: mstate.events || [],
      market: SP.market,
      homeAbbr: H.identity.abbr, awayAbbr: A.identity.abbr,
      book: sp.book || (d.market && d.market.primary_book) || null,
    });
  }

  // ---- AI
  if (d.ai && !d.ai.withheld && d.ai.sections) {
    const s = d.ai.sections; const ai = el("div", { class: "ai" });
    const order = [["model_projection", "Model projection"], ["offensive_matchup", "Offensive matchups"], ["quarterback_edge", "Quarterback edge"], ["trenches", "Trenches"], ["explosive_play_edge", "Explosive plays"], ["third_down_red_zone", "Third down and red zone"], ["roster_talent", "Roster and talent"], ["recent_form", "Recent form"], ["market_movement", "Market movement"], ["key_advantages", "Key advantages"], ["key_concerns", "Key concerns"], ["expected_game_script", "Expected game script"]];
    for (const [k, lab] of order) { if (!s[k]) continue; ai.append(el("h3", {}, lab)); if (Array.isArray(s[k])) { const ul = el("ul", {}); s[k].forEach(x => ul.append(el("li", {}, x))); ai.append(ul); } else ai.append(el("p", {}, s[k])); }
    ai.append(el("p", { class: "prov" }, `Written by ${d.ai.model} from this page's data package only (${fmt.ago(d.ai.generated_at)}); every number was checked against the package before publishing.`));
    sec("Analysis", null, ai);
  } else if (d.ai && d.ai.withheld) sec("Analysis", null, el("span", { class: "badge warn" }, d.ai.reason));
  else sec("Analysis", null, el("span", { class: "badge mute" }, "Not generated yet for this version of the data."));

  // ---- result
  if (d.result && d.result.evaluation) { const e = d.result.evaluation; sec("How the projection did", null, el("p", { style: "font-size:15px" }, `Final ${d.result.away}–${d.result.home}. Margin error ${fmt.num(e.margin_error, 1)} (model ${e.winner_correct ? "had" : "did not have"} the winner)${e.model_ats_result ? `; against the closing spread: ${e.model_ats_result}` : ""}${e.model_ou_result ? `; total: ${e.model_ou_result}` : ""}.`)); }
  root.append(el("footer", { class: "foot" }, `Sources — metrics: ${d.sources.metrics}. Lines: ${d.sources.lines}. Weather: ${d.sources.weather}. Injuries: ${d.sources.injuries}. ${d.sources.note} Page generated ${fmt.ago(d.generated_at)}.`));

  function fmtMetric(v, unit) { if (v == null) return "—"; if (unit === "pct") return (100 * v).toFixed(1) + "%"; if (unit === "points" && Math.abs(v) < 5) return v.toFixed(3); return Number.isInteger(v) ? String(v) : v.toFixed(unit === "seconds" ? 1 : 2); }
  function labelFor(c) { return ({ home_field: "Home field", rating_diff_blend: "Team rating gap" })[c] || (d.edges.find(e => e.category === c) || {}).label || c; }
  function explainEdge(e) { const i = e.inputs || {}; const bits = []; for (const [k, v] of Object.entries(i)) if (typeof v === "number") bits.push(`${k}: ${v.toFixed(2)}`); return bits.slice(0, 8).join(" · "); }
  function drawChart(svg, series, homeAbbr) {
    const pts = series.filter(s => s.spread_home != null); if (pts.length < 2) { svg.replaceWith(el("p", { class: "sub-note" }, "Not enough snapshots for a chart yet.")); return; }
    const books = [...new Set(pts.map(p => p.book))]; const t0 = new Date(pts[0].t).getTime(), t1 = new Date(pts[pts.length - 1].t).getTime();
    const ys = pts.map(p => p.spread_home); let lo = Math.min(...ys) - 1, hi = Math.max(...ys) + 1;
    const X = t => 40 + 740 * ((new Date(t).getTime() - t0) / Math.max(1, t1 - t0)), Y = v => 20 + 140 * ((hi - v) / (hi - lo));
    const ns = "http://www.w3.org/2000/svg"; const mk = (tag, a) => { const e = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); return e; };
    for (const k of [3, 7, 10, 14]) for (const s of [k, -k]) if (s > lo && s < hi) { svg.append(mk("line", { x1: 40, x2: 780, y1: Y(s), y2: Y(s), stroke: "#D9DBD3", "stroke-dasharray": "3 3" })); const tx = mk("text", { x: 4, y: Y(s) + 4 }); tx.textContent = (s > 0 ? "+" : "") + s; svg.append(tx); }
    const colors = ["#1F4E9A", "#B8541E", "#8A8F86", "#2F7D4F", "#E4C24D"];
    books.slice(0, 5).forEach((b, i) => { const p = pts.filter(x => x.book === b); const path = p.map((x, j) => `${j ? "L" : "M"}${X(x.t).toFixed(1)},${Y(x.spread_home).toFixed(1)}`).join(" "); svg.append(mk("path", { d: path, fill: "none", stroke: colors[i], "stroke-width": 2 })); const tx = mk("text", { x: 44 + i * 120, y: 182 }); tx.setAttribute("fill", colors[i]); tx.textContent = b; svg.append(tx); });
    const cap = mk("text", { x: 780, y: 14, "text-anchor": "end" }); cap.textContent = `${homeAbbr} spread · negative = home favored`; svg.append(cap);
  }
}
