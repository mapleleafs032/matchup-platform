/* AI Picks: tiered plays with the measured hit rate for each tier. Reads json/picks/<league>/<season>/W##.json */
async function picksMain() {
  const { fmt, el } = App;
  const root = document.getElementById("picks");
  const m = await App.loadManifest();
  if (!m.picks) { root.replaceChildren(el("div", { class: "empty" }, "The picks payload hasn't been built yet. Run the Picks workflow, then the Site workflow.")); return; }
  const S = {
    league: localStorage.getItem("league") || (m.leagues.includes("CFB") ? "CFB" : m.leagues[0]),
    week: null, market: "all", tier: "all", signalsOnly: false, sort: "score",
  };
  const toolbar = document.getElementById("toolbar");

  function load() {
    S.week = S.week || m.current_week[S.league];
    const path = (m.picks[S.league] || {})[String(S.week)];
    buildToolbar();
    if (!path) { root.replaceChildren(el("div", { class: "empty" }, "No picks for this week yet.")); return; }
    App.loadJSON(path).then(paint).catch(() => root.replaceChildren(el("div", { class: "empty" }, "This week's picks haven't been built yet.")));
  }

  function buildToolbar() {
    toolbar.replaceChildren();
    const seg = el("div", { class: "seg", role: "group", "aria-label": "League" });
    for (const lg of m.leagues) seg.append(el("button", { "aria-pressed": String(lg === S.league) }, lg === "CFB" ? "College" : "NFL"));
    [...seg.children].forEach((b, i) => b.addEventListener("click", () => { S.league = m.leagues[i]; localStorage.setItem("league", S.league); S.week = null; load(); }));
    toolbar.append(seg);
    const wk = el("select", { "aria-label": "Week" });
    for (const w of (m.weeks[S.league] || [])) wk.append(el("option", { value: w, selected: w === S.week ? "" : null }, `Week ${w}`));
    wk.addEventListener("change", () => { S.week = Number(wk.value); load(); });
    toolbar.append(el("label", {}, "Week ", wk));
    const mk = el("select", { "aria-label": "Market" }, el("option", { value: "all" }, "All markets"),
      el("option", { value: "SPREAD" }, "Spread"), el("option", { value: "TOTAL" }, "Total"), el("option", { value: "MONEYLINE" }, "Moneyline"));
    mk.value = S.market; mk.addEventListener("change", () => { S.market = mk.value; paint(window.__picks); });
    toolbar.append(el("label", {}, "Market ", mk));
    const tr = el("select", { "aria-label": "Tier" }, el("option", { value: "all" }, "All tiers"),
      el("option", { value: "A+" }, "A+ only"), el("option", { value: "A" }, "A and up"), el("option", { value: "B" }, "B and up"));
    tr.value = S.tier; tr.addEventListener("change", () => { S.tier = tr.value; paint(window.__picks); });
    toolbar.append(el("label", {}, "Tier ", tr));
    const sg = el("input", { type: "checkbox" }); sg.checked = S.signalsOnly;
    sg.addEventListener("change", () => { S.signalsOnly = sg.checked; paint(window.__picks); });
    toolbar.append(el("label", {}, sg, "Only plays with market signals"));
    const st = el("select", { "aria-label": "Sort" }, el("option", { value: "score" }, "By score"), el("option", { value: "edge" }, "By raw edge"), el("option", { value: "kick" }, "By kickoff"));
    st.value = S.sort; st.addEventListener("change", () => { S.sort = st.value; paint(window.__picks); });
    toolbar.append(el("label", {}, "Sort ", st));
  }

  function honestyBanner(cal) {
    const box = document.getElementById("honesty");
    box.replaceChildren();
    const be = (cal.break_even * 100).toFixed(1);
    const tiers = cal.tiers || {};
    const measured = Object.entries(tiers).filter(([, v]) => v.hit_rate != null);
    const anyBeats = measured.some(([, v]) => v.beats_break_even);
    box.append(el("h2", {}, "What these tiers have actually done"));
    const row = el("div", { class: "hon-row" });
    for (const t of ["A+", "A", "B"]) {
      const v = tiers[t];
      if (!v) continue;
      const has = v.hit_rate != null;
      row.append(el("div", { class: "hon-card " + (has ? (v.beats_break_even ? "good" : "bad") : "unk") },
        el("div", { class: "hon-tier" }, t),
        el("div", { class: "hon-rate num" }, has ? (v.hit_rate * 100).toFixed(1) + "%" : "unmeasured"),
        el("div", { class: "hon-n" }, has ? `on ${v.n} graded plays` : `${v.n} graded, need more`)));
    }
    box.append(row);
    box.append(el("p", { class: "hon-note" },
      `Break-even at -110 pricing is ${be}%. `,
      measured.length
        ? (anyBeats ? "Some tiers cleared that bar historically; on samples this size that is not yet evidence of an edge."
                    : "No tier has cleared that bar historically, so these plays would have lost money at standard pricing.")
        : "Not enough graded history yet to say how these tiers perform.",
      cal.note ? " " + cal.note : ""));
    if (cal.live_total) {
      box.append(el("p", { class: "hon-note" }, `This season, graded live: ${(cal.live_total.hit_rate * 100).toFixed(1)}% on ${cal.live_total.n} plays.`));
    }
  }

  function record(data) {
    const box = document.getElementById("record");
    box.replaceChildren();
    const r = data.season_record;
    if (!r || !r.n) return;
    box.append(el("div", { class: "rec" },
      el("span", {}, el("b", {}, "Season record: "), `${r.wins}-${r.losses}${r.pushes ? "-" + r.pushes : ""}`),
      el("span", { class: "num" }, `${(r.hit_rate * 100).toFixed(1)}%`),
      el("span", { class: "num " + (r.profit_units >= 0 ? "pos" : "neg") }, `${r.profit_units >= 0 ? "+" : ""}${r.profit_units.toFixed(2)} units`),
      el("span", { class: "note" }, "flat 1 unit per play, graded at the line and price recorded when the pick was made")));
  }

  function paint(data) {
    if (!data) return; window.__picks = data;
    honestyBanner(data.calibration || { break_even: 0.5238, tiers: {} });
    record(data);
    const order = { "A+": 0, A: 1, B: 2 };
    let plays = data.picks.filter(p => (S.market === "all" || p.market === S.market)
      && (S.tier === "all" || order[p.tier] <= order[S.tier])
      && (!S.signalsOnly || (p.signals && p.signals.length)));
    plays.sort((a, b) => S.sort === "edge" ? b.edge_points - a.edge_points
      : S.sort === "kick" ? String(a.kickoff_utc).localeCompare(String(b.kickoff_utc))
      : b.score - a.score);
    root.replaceChildren();
    if (!plays.length) {
      root.append(el("div", { class: "empty" }, data.picks.length
        ? "No plays match these filters."
        : "No plays cleared the minimum edge this week. That is a normal outcome when the model and the market agree."));
      document.getElementById("built").textContent = `Picks generated ${fmt.ago(data.generated_at)}.`;
      return;
    }
    for (const t of ["A+", "A", "B"]) {
      const group = plays.filter(p => p.tier === t);
      if (!group.length) continue;
      const cal = (data.calibration.tiers || {})[t];
      root.append(el("h3", { class: "tier-head tier-" + t.replace("+", "plus") },
        el("span", {}, `Tier ${t}`),
        el("span", { class: "tier-count" }, `${group.length} play${group.length > 1 ? "s" : ""}`),
        cal && cal.hit_rate != null
          ? el("span", { class: "tier-rate " + (cal.beats_break_even ? "good" : "bad") }, `historical ${(cal.hit_rate * 100).toFixed(1)}% · n=${cal.n}`)
          : el("span", { class: "tier-rate unk" }, "historical rate unmeasured")));
      for (const p of group) root.append(card(p));
    }
    document.getElementById("built").textContent = `Picks generated ${fmt.ago(data.generated_at)} from model ${data.picks[0].model_version}.`;
  }

  function sideLabel(p) {
    if (p.market === "SPREAD") return `${p.side} ${p.line > 0 ? "+" : ""}${fmt.num(p.line, 1)}`;
    if (p.market === "TOTAL") return `${p.side} ${fmt.num(p.line, 1)}`;
    return `${p.side} ${p.price > 0 ? "+" : ""}${p.price}`;
  }

  function card(p) {
    const head = el("div", { class: "pk-head" },
      el("a", { class: "pk-play", href: `matchup.html?g=${encodeURIComponent(p.game_id)}` }, sideLabel(p)),
      el("span", { class: "pk-game" }, `${p.away} at ${p.home}`),
      el("span", { class: "pk-kick" }, fmt.kick(p.kickoff_utc, false)),
      el("span", { class: "pk-score num" }, `score ${fmt.num(p.score, 1)}`));
    const compare = el("div", { class: "pk-compare" },
      el("div", {}, el("span", { class: "k" }, "model"), el("span", { class: "num v" },
        p.market === "MONEYLINE" ? (p.model_number * 100).toFixed(0) + "%" : fmt.num(p.model_number, 1))),
      el("div", {}, el("span", { class: "k" }, "market"), el("span", { class: "num v" },
        p.market === "MONEYLINE" ? (p.market_number * 100).toFixed(0) + "%" : fmt.num(p.market_number, 1))),
      el("div", {}, el("span", { class: "k" }, "edge"), el("span", { class: "num v" },
        p.market === "MONEYLINE" ? `${(p.expected_value * 100).toFixed(1)}% EV` : `${fmt.num(p.edge_points, 1)} pts`)),
      el("div", {}, el("span", { class: "k" }, "data quality"), el("span", { class: "num v" }, (p.data_quality * 100).toFixed(0) + "%")));
    const split = (p.tickets_pct_side != null || p.money_pct_side != null)
      ? el("div", { class: "pk-split" },
          p.tickets_pct_side != null ? el("span", {}, `${(p.tickets_pct_side * 100).toFixed(0)}% of tickets on this side`) : null,
          p.money_pct_side != null ? el("span", {}, `${(p.money_pct_side * 100).toFixed(0)}% of money`) : null)
      : el("div", { class: "pk-split mute" }, "No betting splits recorded for this game yet.");
    const sig = el("div", { class: "pk-signals" });
    for (const s of (p.signals || "").split(",").filter(Boolean)) {
      sig.append(el("span", { class: "badge sig" }, {
        rlm_agrees: "line moved our way against the tickets", money_agrees: "money leans our way",
        key_number: "key number", line_agrees: "line moved our way",
      }[s] || s));
    }
    return el("section", { class: "pk pk-" + p.tier.replace("+", "plus") }, head, compare, split,
      (p.signals || "").length ? sig : null,
      p.signal_notes ? el("p", { class: "pk-notes" }, p.signal_notes) : null);
  }

  load();
}
