/* Odds tab: betting splits over time per game, with the line overlaid. Reads json/odds/<league>/<season>/W##.json */
async function oddsMain() {
  const { fmt, el } = App;
  const root = document.getElementById("odds");
  const m = await App.loadManifest();
  if (!m.odds) { root.replaceChildren(el("div", { class: "empty" }, "The odds payload hasn't been built yet. Run the Site workflow after ingesting splits.")); return; }
  const S = {
    league: localStorage.getItem("league") || (m.leagues.includes("CFB") ? "CFB" : m.leagues[0]),
    week: null, date: "all", conf: "all", ranked: false, fav: "all", status: "all",
    market: localStorage.getItem("odds.market") || "spread",
    period: localStorage.getItem("odds.period") || "FULL",
    metric: localStorage.getItem("odds.metric") || "both",
    onlyWithSplits: false,
  };
  const toolbar = document.getElementById("toolbar"), mt = document.getElementById("market-toolbar");

  function load() {
    S.week = S.week || m.current_week[S.league];
    const path = (m.odds[S.league] || {})[String(S.week)];
    buildToolbar();
    if (!path) { root.replaceChildren(el("div", { class: "empty" }, "No odds data for this week yet.")); return; }
    App.loadJSON(path).then(paint).catch(() => root.replaceChildren(el("div", { class: "empty" }, "This week's odds page hasn't been built yet.")));
  }

  function buildToolbar() {
    toolbar.replaceChildren();
    const seg = el("div", { class: "seg", role: "group", "aria-label": "League" });
    for (const lg of m.leagues) seg.append(el("button", { "aria-pressed": String(lg === S.league) }, lg === "CFB" ? "College" : "NFL"));
    [...seg.children].forEach((b, i) => b.addEventListener("click", () => { S.league = m.leagues[i]; localStorage.setItem("league", S.league); S.week = null; S.date = "all"; S.conf = "all"; load(); }));
    toolbar.append(seg);
    const wk = el("select", { "aria-label": "Week" });
    for (const w of (m.weeks[S.league] || [])) wk.append(el("option", { value: w, selected: w === S.week ? "" : null }, `Week ${w}`));
    wk.addEventListener("change", () => { S.week = Number(wk.value); S.date = "all"; load(); });
    toolbar.append(el("label", {}, "Week ", wk));
    toolbar.append(el("label", {}, "Date ", el("select", { id: "f-date", "aria-label": "Date" })));
    toolbar.append(el("label", {}, "Conference ", el("select", { id: "f-conf", "aria-label": "Conference" })));
    const fav = el("select", { "aria-label": "Favorite side" }, el("option", { value: "all" }, "Any side"), el("option", { value: "home" }, "Home favored"), el("option", { value: "away" }, "Away favored"));
    fav.value = S.fav; fav.addEventListener("change", () => { S.fav = fav.value; paint(window.__odds); });
    toolbar.append(el("label", {}, "Line ", fav));
    const st = el("select", { "aria-label": "Game status" }, el("option", { value: "all" }, "All games"), el("option", { value: "SCHEDULED" }, "Upcoming"), el("option", { value: "LOCKED" }, "In progress / locked"), el("option", { value: "FINAL" }, "Final"));
    st.value = S.status; st.addEventListener("change", () => { S.status = st.value; paint(window.__odds); });
    toolbar.append(el("label", {}, "Status ", st));
    const rk = el("input", { type: "checkbox" }); rk.checked = S.ranked; rk.addEventListener("change", () => { S.ranked = rk.checked; paint(window.__odds); });
    toolbar.append(el("label", {}, rk, S.league === "CFB" ? "Ranked teams only" : "Playoff-caliber (top-8 rated)"));
    const sp = el("input", { type: "checkbox" }); sp.checked = S.onlyWithSplits; sp.addEventListener("change", () => { S.onlyWithSplits = sp.checked; paint(window.__odds); });
    toolbar.append(el("label", {}, sp, "Only games with splits"));

    mt.replaceChildren();
    const mkt = el("div", { class: "seg", role: "group", "aria-label": "Market" });
    const markets = [["spread", "Spread"], ["total", "Total"], ["moneyline", "Moneyline"]];
    for (const [k, lab] of markets) mkt.append(el("button", { "aria-pressed": String(k === S.market) }, lab));
    [...mkt.children].forEach((b, i) => b.addEventListener("click", () => { S.market = markets[i][0]; localStorage.setItem("odds.market", S.market); paint(window.__odds); }));
    mt.append(el("label", {}, "Market ", mkt));
    const per = el("div", { class: "seg", role: "group", "aria-label": "Period" });
    for (const [k, lab] of [["FULL", "Full game"], ["1H", "First half"]]) per.append(el("button", { "aria-pressed": String(k === S.period) }, lab));
    [...per.children].forEach((b, i) => b.addEventListener("click", () => { S.period = ["FULL", "1H"][i]; localStorage.setItem("odds.period", S.period); paint(window.__odds); }));
    mt.append(el("label", {}, "Period ", per));
    const met = el("div", { class: "seg", role: "group", "aria-label": "Show" });
    for (const [k, lab] of [["both", "Tickets + money"], ["ticket", "Tickets"], ["money", "Money"]]) met.append(el("button", { "aria-pressed": String(k === S.metric) }, lab));
    [...met.children].forEach((b, i) => b.addEventListener("click", () => { S.metric = ["both", "ticket", "money"][i]; localStorage.setItem("odds.metric", S.metric); paint(window.__odds); }));
    mt.append(el("label", {}, "Show ", met));
  }

  function fillSelect(id, values, current, label) {
    const s = document.getElementById(id); if (!s) return;
    s.replaceChildren(el("option", { value: "all" }, label));
    for (const v of values) s.append(el("option", { value: v, selected: v === current ? "" : null }, id === "f-date" ? fmt.day(v + "T12:00:00") : v));
    s.value = current;
  }

  function paint(data) {
    if (!data) return; window.__odds = data;
    const games = data.games;
    fillSelect("f-date", [...new Set(games.map(g => g.filters.date).filter(Boolean))].sort(), S.date, "All dates");
    document.getElementById("f-date").onchange = e => { S.date = e.target.value; paint(data); };
    fillSelect("f-conf", [...new Set(games.flatMap(g => [g.filters.conf_home, g.filters.conf_away]).filter(Boolean))].sort(), S.conf, "All conferences");
    document.getElementById("f-conf").onchange = e => { S.conf = e.target.value; paint(data); };
    const shown = games.filter(g => (S.date === "all" || g.filters.date === S.date) && (S.conf === "all" || g.filters.conf_home === S.conf || g.filters.conf_away === S.conf)
      && (!S.ranked || g.filters.ranked) && (S.fav === "all" || g.filters.favorite === S.fav) && (S.status === "all" || g.status === S.status)
      && (!S.onlyWithSplits || (g.splits?.[S.period]?.available)));
    document.getElementById("coverage").textContent =
      `${data.coverage.with_splits} of ${data.coverage.total} games have splits this week. ${data.source_note}`;
    root.replaceChildren();
    if (!shown.length) { root.append(el("div", { class: "empty" }, "No games match these filters.")); return; }
    for (const g of shown) root.append(card(g));
    document.getElementById("built").textContent = `Odds page generated ${fmt.ago(data.generated_at)}.`;
  }

  function pctChip(label, v, side, cls) {
    if (v == null) return null;
    return el("div", { class: "chip" }, el("span", { class: "k" }, label), el("span", { class: "num v" }, (100 * v).toFixed(0) + "%"),
      side ? el("span", { class: "badge " + cls }, side) : null);
  }

  function card(g) {
    const sp = (g.splits || {})[S.period] || { available: false, notes: [], series: [], latest: {}, divergence: {}, rlm: {} };
    const head = el("div", { class: "og-head" },
      el("a", { class: "og-teams", href: `matchup.html?g=${encodeURIComponent(g.game_id)}` },
        el("span", {}, g.away.rank ? `#${g.away.rank} ` : "", g.away.short || g.away.name),
        el("span", { class: "at" }, "at"),
        el("span", {}, g.home.rank ? `#${g.home.rank} ` : "", g.home.short || g.home.name)),
      el("span", { class: "og-kick" }, fmt.kick(g.kickoff_utc, g.kickoff_is_tba)),
      el("span", { class: "og-line num" }, g.market ? `${g.home.abbr} ${fmt.spread(g.market.spread_home)} · O/U ${fmt.num(g.market.total, 1)}` : "no line"),
      g.model ? el("span", { class: "og-model num" }, `model ${fmt.spread(-g.model.proj_margin_home)}`) : null);

    if (!sp.available) {
      return el("section", { class: "og" }, head,
        el("div", { class: "og-empty" }, S.period === "1H"
          ? "No first-half splits entered for this game. First-half markets need either a licensed feed or a first-half splits paste."
          : "No betting splits entered for this game yet."));
    }
    const lat = sp.latest[S.market] || {};
    const div = sp.divergence[S.market];
    const rlm = sp.rlm[S.market];
    const chips = el("div", { class: "og-chips" },
      pctChip("tickets", lat.ticket_pct_home, lat.ticket_side, lat.ticket_pct_home >= 0.5 ? "home" : "away"),
      pctChip("money", lat.money_pct_home, lat.money_side, lat.money_pct_home >= 0.5 ? "home" : "away"),
      div ? el("div", { class: "chip" }, el("span", { class: "k" }, "gap"), el("span", { class: "num v" }, (div.points > 0 ? "+" : "") + div.points.toFixed(0) + " pts"),
        div.notable ? el("span", { class: "badge warn" }, "notable") : null) : null,
      rlm ? el("div", { class: "chip" }, el("span", { class: "badge warn" }, "line moved against the tickets")) : null,
      el("div", { class: "chip" }, el("span", { class: "k" }, "source"), el("span", { class: "badge mute" }, sp.book || "manual")));
    const notes = el("ul", { class: "og-notes" });
    for (const n of sp.notes) notes.append(el("li", {}, n));
    return el("section", { class: "og" }, head, chips, chart(sp, g), notes);
  }

  /* splits over time, with the line drawn on a second axis */
  function chart(sp, g) {
    const W = 720, H = 190, L = 40, R = 46, T = 14, B = 26;
    const pts = sp.series.filter(p => p[`${S.market}_ticket`] != null || p[`${S.market}_money`] != null);
    if (pts.length < 1) return el("div", { class: "og-empty" }, "Not enough snapshots to chart yet.");
    const ts = pts.map(p => new Date(p.t).getTime());
    const t0 = Math.min(...ts), t1 = Math.max(...ts), span = Math.max(t1 - t0, 1);
    const x = t => L + ((t - t0) / span) * (W - L - R);
    const y = v => T + (1 - v) * (H - T - B);
    const lineKey = S.market === "total" ? "line_total" : "line_spread_home";
    const lineVals = pts.map(p => p[lineKey]).filter(v => v != null);
    const lo = lineVals.length ? Math.min(...lineVals) : 0, hi = lineVals.length ? Math.max(...lineVals) : 1;
    const pad = (hi - lo) < 1 ? 1 : (hi - lo) * 0.35;
    const ly = v => T + (1 - (v - (lo - pad)) / ((hi + pad) - (lo - pad))) * (H - T - B);
    const path = (key, mapper) => {
      const seg = pts.filter(p => p[key] != null);
      if (!seg.length) return null;
      return seg.map((p, i) => `${i ? "L" : "M"}${x(new Date(p.t).getTime()).toFixed(1)},${mapper(p[key]).toFixed(1)}`).join(" ");
    };
    const svg = [];
    svg.push(`<line x1="${L}" y1="${y(0.5)}" x2="${W - R}" y2="${y(0.5)}" stroke="var(--rule)" stroke-dasharray="3 3"/>`);
    for (const v of [0, 0.25, 0.5, 0.75, 1]) {
      svg.push(`<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" font-size="10" fill="var(--mute)">${v * 100}%</text>`);
    }
    if (lineVals.length) {
      const d = path(lineKey, ly);
      if (d) svg.push(`<path d="${d}" fill="none" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="5 3" opacity=".55"/>`);
      svg.push(`<text x="${W - R + 6}" y="${ly(hi) + 4}" font-size="10" fill="var(--ink-2)">${hi.toFixed(1)}</text>`);
      svg.push(`<text x="${W - R + 6}" y="${ly(lo) + 4}" font-size="10" fill="var(--ink-2)">${lo.toFixed(1)}</text>`);
    }
    if (S.metric !== "money") {
      const d = path(`${S.market}_ticket`, y);
      if (d) svg.push(`<path d="${d}" fill="none" stroke="var(--away)" stroke-width="2.5"/>`);
    }
    if (S.metric !== "ticket") {
      const d = path(`${S.market}_money`, y);
      if (d) svg.push(`<path d="${d}" fill="none" stroke="var(--home)" stroke-width="2.5"/>`);
    }
    for (const p of pts) {
      const t = new Date(p.t).getTime();
      if (S.metric !== "money" && p[`${S.market}_ticket`] != null) svg.push(`<circle cx="${x(t).toFixed(1)}" cy="${y(p[`${S.market}_ticket`]).toFixed(1)}" r="2.5" fill="var(--away)"><title>${new Date(p.t).toLocaleString()} · tickets ${(100 * p[`${S.market}_ticket`]).toFixed(0)}%</title></circle>`);
      if (S.metric !== "ticket" && p[`${S.market}_money`] != null) svg.push(`<circle cx="${x(t).toFixed(1)}" cy="${y(p[`${S.market}_money`]).toFixed(1)}" r="2.5" fill="var(--home)"><title>${new Date(p.t).toLocaleString()} · money ${(100 * p[`${S.market}_money`]).toFixed(0)}%</title></circle>`);
    }
    svg.push(`<text x="${L}" y="${H - 8}" font-size="10" fill="var(--mute)">${new Date(t0).toLocaleString([], { month: "short", day: "numeric", hour: "numeric" })}</text>`);
    svg.push(`<text x="${W - R}" y="${H - 8}" text-anchor="end" font-size="10" fill="var(--mute)">${new Date(t1).toLocaleString([], { month: "short", day: "numeric", hour: "numeric" })}</text>`);
    const legend = el("div", { class: "og-legend" },
      S.metric !== "money" ? el("span", {}, el("i", { class: "sw away" }), `tickets on ${S.market === "total" ? "the over" : g.home.abbr}`) : null,
      S.metric !== "ticket" ? el("span", {}, el("i", { class: "sw home" }), `money on ${S.market === "total" ? "the over" : g.home.abbr}`) : null,
      el("span", {}, el("i", { class: "sw line" }), S.market === "total" ? "total" : "spread"));
    return el("div", { class: "og-chart" },
      el("div", { html: `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Betting splits over time">${svg.join("")}</svg>` }), legend);
  }

  load();
}
