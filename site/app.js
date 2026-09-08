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
        root.append(el("div", { class: "col-head" }, el("div", {}, "Matchup"), el("div", {}, "Kickoff"), el("div", {}, "Spread (open)"), el("div", {}, "Total (open)"), el("div", {}, "Model score"), el("div", {}, "Win probability"), el("div", {}, "Key edge")));
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
    const ke = g.key_edge ? el("div", { class: "edge-chip" }, el("span", { class: "badge " + g.key_edge.side }, g.key_edge.side === "home" ? g.home.abbr : g.away.abbr), " ", g.key_edge.label, " ", el("b", {}, Math.abs(g.key_edge.points).toFixed(1) + " pts"))
      : el("div", { class: "edge-chip" }, el("span", { class: "badge mute" }, "Edges not built"));
    const when = g.result ? el("div", { class: "when" }, el("span", { class: "final" }, `Final ${g.result.away}–${g.result.home}`), g.result.model_ats ? el("span", { class: "tv" }, `model vs spread: ${g.result.model_ats}`) : null)
      : el("div", { class: "when" }, fmt.kick(g.kickoff_utc, g.kickoff_is_tba), el("span", { class: "tv" }, [g.tv, g.venue && g.venue.city, g.neutral_site ? "neutral site" : null].filter(Boolean).join(" · ")));
    return el("a", { class: "row", href: `matchup.html?g=${g.game_id}` }, el("div", { class: "teams" }, team(g.away, "away"), team(g.home, "home")), when, spreadCell, totCell, modelCell, wp, ke);
  }
  render();
}
