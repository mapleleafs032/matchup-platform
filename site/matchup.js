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
  // Every section gets an id and an entry in a sticky jump bar, so the page can be navigated without
  // scrolling through it.
  const navItems = [];
  const slug = t => String(t).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const sec = (title, note, ...kids) => {
    const id = "s-" + slug(title);
    const s = el("section", { class: "block", id }, el("h2", {}, title));
    if (note) s.append(el("p", { class: "sub-note" }, note));
    s.append(...kids);
    root.append(s);
    navItems.push({ id, title });
    return s;
  };
  const buildNav = () => {
    if (!navItems.length) return;
    const nav = el("nav", { class: "sec-nav", "aria-label": "Sections on this page" });
    for (const it of navItems) {
      const a = el("a", { href: "#" + it.id }, it.title);
      a.addEventListener("click", ev => {
        ev.preventDefault();
        const t = document.getElementById(it.id);
        if (t && t.scrollIntoView) t.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      nav.append(a);
    }
    try {
      if (typeof root.prepend === "function") root.prepend(nav);
      else if (typeof root.insertBefore === "function") root.insertBefore(nav, root.firstChild || null);
      else root.append(nav);
    } catch (e) { root.append(nav); }
  };
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
  const esc = t => String(t == null ? "" : t).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function fmtQL(v, unit) {
    if (v == null) return "—";
    if (unit === "pct") return (v * 100).toFixed(2) + "%";
    if (unit === "rank") return "#" + v;
    if (unit === "qbr") return v.toFixed(1);
    return Math.abs(v) >= 50 ? v.toFixed(0) : v.toFixed(1);
  }
  function quickTableHTML(q) {
    const away = d.game.away, home = d.game.home;
    const aName = esc(away.short || away.name), hName = esc(home.short || home.name);
    const aTag = esc(away.abbr || aName), hTag = esc(home.abbr || hName);
    const title = `${d.game.league === "CFB" ? "College" : "NFL"} analysis — season to date, ${qlAdj.v === "RAW" ? "raw" : "opponent-adjusted"}`;
    const tip = r => r.metric_key === "__qbr"
      ? "ESPN QBR when both teams have it, otherwise NCAA passing efficiency. Both teams are always on the same scale."
      : r.metric_key === "__sos"
        ? "Strength of schedule rank. College uses the ESPN FPI resume rank; the NFL uses our own opponent-rating SOS. Lower means a tougher schedule."
        : (r.description || r.label);
    let h = `<table class="ql"><caption>${esc(title)}</caption><thead>`;
    h += `<tr class="ql-teams"><th class="ql-metric" rowspan="2" scope="col">Metric</th>`
       + `<th class="ql-team away" colspan="2" scope="colgroup">${aName}</th>`
       + `<th class="ql-team home" colspan="2" scope="colgroup">${hName}</th>`
       + `<th class="ql-edgehead" rowspan="2" scope="col">Edge</th></tr>`;
    h += `<tr class="ql-sub"><th scope="col">Value</th><th scope="col">Rk</th>`
       + `<th scope="col">Value</th><th scope="col">Rk</th></tr></thead><tbody>`;
    for (const r of q.rows) {
      const side = r.edge === "home" ? hTag : r.edge === "away" ? aTag : "";
      h += `<tr class="g-${esc(r.group.toLowerCase())}">`
         + `<th scope="row" title="${esc(tip(r))}">${esc(r.label)}</th>`
         + `<td class="num${r.edge === "away" ? " win" : ""}">${esc(fmtQL(r.away.v, r.unit))}</td>`
         + `<td class="rk">${r.away.rank == null ? "" : esc(r.away.rank)}</td>`
         + `<td class="num${r.edge === "home" ? " win" : ""}">${esc(fmtQL(r.home.v, r.unit))}</td>`
         + `<td class="rk">${r.home.rank == null ? "" : esc(r.home.rank)}</td>`
         + `<td class="ql-edge ${esc(r.edge || "")}">${esc(side)}</td></tr>`;
    }
    const w = q.winner;
    h += `</tbody><tfoot><tr class="ql-total"><th scope="row">Edge count</th>`
       + `<td class="num" colspan="2">${q.edge_count.away}</td>`
       + `<td class="num" colspan="2">${q.edge_count.home}</td>`
       + `<td class="ql-edge ${esc(w || "")}">${w === "home" ? hTag : w === "away" ? aTag : "even"}</td>`
       + `</tr></tfoot></table>`;
    return h;
  }
  function paintQuick() {
    [...qlToggle.children].forEach(b => b.setAttribute("aria-pressed", String((b.textContent === "Raw") === (qlAdj.v === "RAW"))));
    const q = (d.quick_look || {})[qlAdj.v];
    qlWrap.replaceChildren();
    if (!q || !q.rows || !q.rows.length) { qlWrap.append(el("p", { class: "note" }, "Quick look is unavailable for this game.")); return; }
    qlWrap.append(el("div", { html: quickTableHTML(q) }));
    qlWrap.append(el("p", { class: "note" }, q.edge_rule + " Ranks are among all teams in the league as of this week; a blank rank means the metric is not ranked."));
  }
  paintQuick();
  /* ---- schedules and head-to-head, beside the quick look ---- */
  const schedWrap = el("div", { class: "sched-wrap" });
  const schedState = { view: "schedule", team: "home" };
  const schedBar = el("div", { class: "toolbar sched-bar" });
  const segView = el("div", { class: "seg", role: "group", "aria-label": "Schedule or head to head" });
  for (const [k, lab] of [["schedule", "Schedules"], ["h2h", "Head-to-head"]]) segView.append(el("button", { "aria-pressed": String(k === schedState.view) }, lab));
  [...segView.children].forEach((b, i) => b.addEventListener("click", () => { schedState.view = ["schedule", "h2h"][i]; paintSched(); }));
  schedBar.append(segView);
  const segTeam = el("div", { class: "seg", role: "group", "aria-label": "Team" });
  for (const [k, lab] of [["away", A.identity.abbr], ["home", H.identity.abbr]]) segTeam.append(el("button", { "aria-pressed": String(k === schedState.team) }, lab));
  [...segTeam.children].forEach((b, i) => b.addEventListener("click", () => { schedState.team = ["away", "home"][i]; paintSched(); }));
  schedBar.append(segTeam);

  function gameLink(gid, label, cls) {
    const a = el("a", { class: cls || "sched-link", href: `matchup.html?g=${encodeURIComponent(gid)}` }, label);
    return a;
  }
  function paintSched() {
    [...segView.children].forEach((b, i) => b.setAttribute("aria-pressed", String(["schedule", "h2h"][i] === schedState.view)));
    [...segTeam.children].forEach((b, i) => b.setAttribute("aria-pressed", String(["away", "home"][i] === schedState.team)));
    segTeam.style.display = schedState.view === "schedule" ? "" : "none";
    schedWrap.replaceChildren();
    if (schedState.view === "schedule") {
      const rows = ((d.schedules || {})[schedState.team]) || [];
      if (!rows.length) { schedWrap.append(el("p", { class: "sub-note" }, "No schedule recorded.")); return; }
      const t = el("table", { class: "sched" });
      t.append(el("tr", { class: "sched-head" }, el("th", {}, "Wk"), el("th", {}, "Opponent"), el("th", {}, "Result")));
      for (const r of rows) {
        const opp = `${r.at} ${r.opponent.abbr}`;
        const score = r.us == null ? (r.kickoff_utc ? fmt.kick(r.kickoff_utc, false) : "—")
          : `${r.result} ${r.us}-${r.them}`;
        t.append(el("tr", { class: r.result ? "r-" + r.result : "" },
          el("td", { class: "num" }, String(r.week)),
          el("td", {}, r.has_page === false ? el("span", {}, opp) : gameLink(r.game_id, opp)),
          el("td", { class: "num sched-res" }, (r.us == null || r.has_page === false) ? score : gameLink(r.game_id, score, "sched-link plain"))));
      }
      schedWrap.append(t);
      schedWrap.append(el("p", { class: "sub-note" }, "Click a game for its page: completed games carry the final score, the graded pick and the stats as of that week."));
    } else {
      const rows = d.head_to_head || [];
      if (!rows.length) { schedWrap.append(el("p", { class: "sub-note" }, "No previous meetings in the seasons held (2021 onward).")); return; }
      const t = el("table", { class: "sched" });
      t.append(el("tr", { class: "sched-head" }, el("th", {}, "Season"), el("th", {}, "Matchup"), el("th", {}, "Score")));
      for (const r of rows) {
        const label = `${r.away.abbr} at ${r.home.abbr}`;
        const won = r.winner === H.identity.team_id ? "r-W" : r.winner === A.identity.team_id ? "r-L" : "";
        const score = `${r.away_score}-${r.home_score}`;
        t.append(el("tr", { class: won },
          el("td", { class: "num" }, `${r.season} W${r.week}`),
          el("td", {}, r.has_page ? gameLink(r.game_id, label) : el("span", {}, label)),
          el("td", { class: "num" }, r.has_page ? gameLink(r.game_id, score, "sched-link plain") : el("span", {}, score))));
      }
      schedWrap.append(t);
      schedWrap.append(el("p", { class: "sub-note" }, `Last ${rows.length} meeting${rows.length === 1 ? "" : "s"} held in the data (2021 onward). This season's games link to their page; earlier ones show the result only.`));
    }
  }
  paintSched();

  sec("Quick look", null,
    el("div", { class: "ql-row" },
      el("div", { class: "ql-col" }, el("div", { class: "toolbar" }, el("label", {}, "Basis ", qlToggle)), qlWrap),
      el("div", { class: "sched-col" }, schedBar, schedWrap)));
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
    const SP = { market: "spread", perspective: "home", metric: "both",
                 window: localStorage.getItem("odds.window") || "today" };
    const bar = el("div", { class: "toolbar sp-toolbar" });
    const segM = el("div", { class: "seg", role: "group", "aria-label": "Market" });
    for (const [k, lab] of [["spread", "Spread"], ["total", "Total"], ["moneyline", "Moneyline"]]) segM.append(el("button", { "aria-pressed": String(k === SP.market) }, lab));
    const segW = el("select", { "aria-label": "Time window" },
      el("option", { value: "all" }, "All"),
      el("option", { value: "week" }, "This week"),
      el("option", { value: "today" }, "Today"),
      el("option", { value: "12" }, "Last 12 hours"),
      el("option", { value: "4" }, "Last 4 hours"),
      el("option", { value: "2" }, "Last 2 hours"));
    segW.value = SP.window;
    segW.addEventListener("change", () => { SP.window = segW.value; localStorage.setItem("odds.window", SP.window); redraw(); });
    [...segM.children].forEach((b, i) => b.addEventListener("click", () => { SP.market = ["spread", "total", "moneyline"][i]; redraw(); }));
    bar.append(el("label", {}, "Market ", segM));
    bar.append(el("label", {}, "Window ", segW));

    spSec.append(bar);
    const chips = el("div", { class: "og-chips" });
    spSec.append(chips);
    const spChart = el("div", { class: "og-chart" });
    spSec.append(spChart);
    const spNotes = el("ul", { class: "market-notes" });
    spSec.append(spNotes);
    const moveBox = el("div", {});
    spSec.append(moveBox);
    const evList = el("div", {});
    spSec.append(evList);
    const redraw = () => {
      const key = SP.market === "moneyline" ? "spread" : SP.market;
      chips.replaceChildren();
      for (const c of App.indicatorChips(mstate, SP.market)) chips.append(c);
      [...segM.children].forEach((b, i) => b.setAttribute("aria-pressed", String(["spread", "total", "moneyline"][i] === SP.market)));
      spChart.replaceChildren(splitsChart(spFull, mstate, SP));
      spNotes.replaceChildren();
      for (const n of spFull.notes || []) spNotes.append(el("li", {}, n));
      moveBox.replaceChildren(App.movementTable(spFull.series || [], SP.market, H.identity.abbr, A.identity.abbr, spFull.book));
      evList.replaceChildren(App.eventLog(mstate.events || [], SP.market));
    };
    redraw();
  }

  /* Snapshots bunch up near kickoff: the week before is hourly, game day is every 15 minutes, so on a
     full-history axis today's pulls collapse into the right-hand edge. Narrowing the window spreads
     them out. Matches the Odds page so both read the same way. */
  function chartWindowStart(series, wsel) {
    if (wsel === "all" || !series.length) return null;
    const last = new Date(series[series.length - 1].t).getTime();
    if (/^\d+$/.test(wsel)) return last - Number(wsel) * 3600e3;
    const midnight = new Date(Math.min(Date.now(), last));
    midnight.setHours(0, 0, 0, 0);
    if (wsel === "today") return midnight.getTime();
    if (wsel === "week") return midnight.getTime() - 6 * 86400e3;
    return null;
  }
  function splitsChart(sp, mstate, SP) {
    const splits = sp.series || [];
    const from = chartWindowStart(splits.length ? splits : ((d.market && d.market.series) || []), SP.window || "today");
    const clip = arr => (from == null ? arr : (arr || []).filter(x => new Date(x.t).getTime() >= from));
    const cs = clip(splits);
    const cl = clip((d.market && d.market.series) || []);
    return App.marketChart({
      lineSeries: cl.length ? cl : (from == null ? ((d.market && d.market.series) || []) : []),
      splitsSeries: cs,
      events: clip(mstate.events || []),
      market: SP.market,
      homeAbbr: H.identity.abbr, awayAbbr: A.identity.abbr,
      book: sp.book || (d.market && d.market.primary_book) || null,
    });
  }

  // ---- how the game actually played out (completed games only)
  if (d.box) {
    const B = d.box, aT = A.identity.abbr, hT = H.identity.abbr;
    const n1 = v => (v == null ? "—" : (Math.abs(v) >= 100 ? Math.round(v) : v.toFixed(1)));
    const pc = v => (v == null ? "—" : (v * 100).toFixed(1) + "%");
    const td = (side, key) => {
      const x = B[side][key];
      return x ? `${x.conv}/${x.att} (${pc(x.pct)})` : "—";
    };
    const ROWS = [
      ["Points", s => n1(B[s].points), "scoring"],
      ["Total yards", s => n1(B[s].total_yards), "eff"],
      ["Plays", s => n1(B[s].plays), "eff"],
      ["Yards per play", s => n1(B[s].yards_per_play), "eff"],
      ["Passing yards", s => `${n1(B[s].pass_yards)} (${n1(B[s].pass_cmp)}/${n1(B[s].pass_att)})`, "pass"],
      ["Yards per pass", s => n1(B[s].yards_per_pass), "pass"],
      ["Rushing yards", s => `${n1(B[s].rush_yards)} (${n1(B[s].rush_att)} att)`, "rush"],
      ["Yards per rush", s => n1(B[s].yards_per_rush), "rush"],
      ["First downs", s => n1(B[s].first_downs), "sit"],
      ["Third down", s => td(s, "third_down"), "sit"],
      ["Red zone TD%", s => pc(B[s].redzone_td_rate), "rz"],
      ["Turnovers", s => n1(B[s].turnovers), "other"],
      ["Takeaways", s => n1(B[s].takeaways), "other"],
      ["Turnover margin", s => (B[s].turnover_margin == null ? "—" : (B[s].turnover_margin > 0 ? "+" : "") + n1(B[s].turnover_margin)), "other"],
      ["Sacks made / taken", s => `${n1(B[s].sacks_made)} / ${n1(B[s].sacks_taken)}`, "trench"],
      ["Penalties", s => `${n1(B[s].penalties)} for ${n1(B[s].penalty_yds)}`, "other"],
      ["Time of possession", s => (B[s].possession || "—"), "other"],
      ["Yards allowed", s => n1(B[s].yards_allowed), "eff"],
      ["Yards per play allowed", s => n1(B[s].yards_per_play_allowed), "eff"],
      ["Pass yards allowed", s => n1(B[s].pass_yards_allowed), "pass"],
      ["Rush yards allowed", s => n1(B[s].rush_yards_allowed), "rush"],
      ["Third down allowed", s => pc(B[s].third_down_allowed_pct), "sit"],
    ];
    const t = el("table", { class: "ql boxtbl" });
    const thead = el("thead", {},
      el("tr", { class: "ql-teams" },
        el("th", { class: "ql-metric", scope: "col" }, "Statistic"),
        el("th", { class: "ql-team away", scope: "col" }, aT),
        el("th", { class: "ql-team home", scope: "col" }, hT)));
    t.append(thead);
    const tb = el("tbody", {});
    for (const [label, fn, grp] of ROWS) {
      tb.append(el("tr", { class: "g-" + grp },
        el("th", { scope: "row" }, label),
        el("td", { class: "num" }, fn("away")),
        el("td", { class: "num" }, fn("home"))));
    }
    t.append(tb);
    sec("How it played out", "Actual production in this game, from the official box score.",
        el("div", { class: "ql-wrap" }, t));
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
  buildNav();
}