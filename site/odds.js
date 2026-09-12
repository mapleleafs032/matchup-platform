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
    window: localStorage.getItem("odds.window") || "today",   // how far back the chart shows
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
    const win = el("select", { "aria-label": "Time window" },
      el("option", { value: "all" }, "All"),
      el("option", { value: "week" }, "This week"),
      el("option", { value: "today" }, "Today"),
      el("option", { value: "12" }, "Last 12 hours"));
    win.value = S.window;
    win.addEventListener("change", () => { S.window = win.value; localStorage.setItem("odds.window", S.window); paint(window.__odds); });
    mt.append(el("label", {}, "Window ", win));

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
    const winLabel = { all: "the full history", week: "this week", today: "today", "12": "the last 12 hours" }[S.window] || "the full history";
    document.getElementById("coverage").textContent =
      `${data.coverage.with_splits} of ${data.coverage.total} games have splits this week. Charts show ${winLabel}. ${data.source_note}`;
    root.replaceChildren();
    if (!shown.length) { root.append(el("div", { class: "empty" }, "No games match these filters.")); return; }
    for (const g of shown) root.append(card(g));
    document.getElementById("built").textContent = `Odds page generated ${fmt.ago(data.generated_at)}.`;
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
    const pair = App.splitsPair({ ticket: lat.ticket_pct_home, money: lat.money_pct_home,
                                  market: S.market, homeAbbr: g.home.abbr, awayAbbr: g.away.abbr });
    const chips = el("div", { class: "og-chips" },
      div ? el("div", { class: "chip" }, el("span", { class: "k" }, "gap"), el("span", { class: "num v" }, (div.points > 0 ? "+" : "") + div.points.toFixed(0) + " pts"),
        div.notable ? el("span", { class: "badge warn" }, "notable") : null) : null,
      rlm ? el("div", { class: "chip" }, el("span", { class: "badge warn" }, "line moved against the tickets")) : null,
      el("div", { class: "chip" }, el("span", { class: "k" }, "source"), el("span", { class: "badge mute" }, sp.book || "manual")));
    for (const chip of App.indicatorChips(g.market_state, S.market)) chips.append(chip);
    const notes = el("ul", { class: "og-notes" });
    for (const n of sp.notes) notes.append(el("li", {}, n));
    return el("section", { class: "og" }, head, pair, chips, chart(sp, g), notes);
  }

  /* Snapshots bunch up near kickoff: the week before is hourly, game day is every 15 minutes, so on a
     full-history axis today's pulls collapse into the right-hand edge. Narrowing the window spreads
     them out. Kickoff anchors "game day" so the window means the same thing for every game. */
  function windowStart(g, series) {
    if (S.window === "all" || !series.length) return null;
    const last = new Date(series[series.length - 1].t).getTime();
    if (S.window === "12") return last - 12 * 3600e3;
    // "Today" and "This week" anchor to the local calendar, so they mean the same thing all day rather
    // than sliding with whenever the most recent pull happened to land.
    const midnight = new Date(Math.min(Date.now(), last));
    midnight.setHours(0, 0, 0, 0);
    if (S.window === "today") return midnight.getTime();
    if (S.window === "week") return midnight.getTime() - 6 * 86400e3;
    return null;
  }

  function chart(sp, g) {
    const splits = sp.series || [];
    const from = windowStart(g, splits);
    const clip = arr => (from == null ? arr : arr.filter(p => new Date(p.t).getTime() >= from));
    const cs = clip(splits), cl = clip(g.line_series || []), ce = clip(g.events || []);
    const shown = App.marketChart({
      lineSeries: cl.length ? cl : (from == null ? (g.line_series || []) : []),
      splitsSeries: cs,
      events: ce,
      market: S.market,
      homeAbbr: g.home.abbr, awayAbbr: g.away.abbr,
      book: sp.book || (g.market && g.market.book) || null,
    });
    if (from != null && cs.length < 2) {
      return el("div", {},
        el("p", { class: "og-empty" }, `Only ${cs.length} snapshot${cs.length === 1 ? "" : "s"} in this window — widen it to see the movement.`),
        shown);
    }
    return shown;
  }

  load();
}
