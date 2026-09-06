"""
Matchup engine (master prompt §7-9, §20-22, §34-35).

Every category produces edge_raw in league standard-deviation units from the HOME team's perspective
(+ = home advantage), an integer edge_score in -3..+3 for display, and a preliminary points contribution
= weight * edge_raw using config.MATCHUP_WEIGHTS_INIT (stated priors). Phase 8 fits the weights on
2021-2025 and replaces the prelim contributions; the edge_raw definitions here are the model's features.

Interaction principle: a unit-vs-unit category is
    z(home unit) + z(opponent's allowed metric)         (for the home unit)
  - z(away unit) - z(home's allowed metric)              (for the away unit)
so a strong offense facing a strong defense nets toward zero instead of "both are good".

Inputs (all as-of the game, produced by earlier phases):
  team_metrics_asof  windows BLEND (efficiency, OPP_ADJ) and SEASON (style, RAW); ranks/pct per league
  team_ratings       overall/off/def ratings, sos, league HFA (as-of week)
  qb_status, continuity, talent_scores, injuries, weather_snapshots, depth_charts, games (rest/travel)
Unavailable inputs produce is_unavailable=True for that category, never a silent zero.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from pipeline import storage

AN = config.TABLES / "analytics"
ROSTER = config.TABLES / "roster"

CATEGORIES = ["OVERALL_OFF", "OVERALL_DEF", "PASS_OFF", "PASS_DEF", "RUSH_OFF", "RUSH_DEF", "QB", "OFFENSIVE_LINE", "DEFENSIVE_FRONT",
              "EXPLOSIVE", "SUCCESS", "THIRD_DOWN", "RED_ZONE", "TURNOVER", "SPECIAL_TEAMS", "COACHING", "TALENT", "RETURNING_PROD",
              "RECENT_FORM", "SOS", "HOME_FIELD", "STYLE_FIT", "INJURY", "WEATHER", "REST"]

# metric_key -> sign so that "higher = better for the team it describes" (registry higher_is_better)
def _score(raw: float | None) -> int | None:
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None
    a = abs(raw)
    s = 0 if a < 0.25 else 1 if a < 0.75 else 2 if a < 1.5 else 3
    return int(np.sign(raw) * s)


class Week:
    """Loads everything for one league-week once; z-scores computed across the teams in that build."""

    def __init__(self, league: str, season: int, week: int):
        self.league, self.season, self.week = league, season, week
        m = storage.read_table(AN / "team_metrics_asof" / league / str(season) / f"W{week:02d}.parquet")
        self.metrics = m
        self.reg = storage.read_table(config.TABLES / "ref" / "metric_definitions.csv")
        self.hib = dict(zip(self.reg.metric_key, self.reg.higher_is_better)) if not self.reg.empty else {}
        rat = storage.read_table(AN / "team_ratings" / league / f"{season}.parquet")
        self.rat = rat[rat.as_of_week == week].set_index("team_id") if not rat.empty and (rat.as_of_week == week).any() else pd.DataFrame()
        self.qb = _latest(ROSTER / "qb_status" / league / str(season), week)
        self.cont = storage.read_table(ROSTER / "continuity" / league / f"{season}.parquet")
        self.talent = storage.read_table(ROSTER / "talent_scores.parquet") if league == "CFB" else pd.DataFrame()
        self.inj = storage.read_table(ROSTER / "injuries" / league / f"{season}.csv")
        self.depth = _latest(ROSTER / "depth_charts" / league / str(season), week)
        self.games = storage.read_table(storage.games_path(league, season))
        self.venues = storage.read_table(config.TABLES / "ref" / "venues.parquet")
        wdir = config.TABLES / "context" / "weather_snapshots" / league / str(season)
        self.weather = pd.read_csv(wdir / f"W{week:02d}.csv") if (wdir / f"W{week:02d}.csv").exists() else pd.DataFrame()
        self._z_cache: dict = {}

    # ---- metric access ----------------------------------------------------------------
    def _table(self, window: str, adj: str) -> pd.DataFrame:
        key = (window, adj)
        if key in self._z_cache:
            return self._z_cache[key]
        sub = self.metrics[(self.metrics.window == window) & (self.metrics.adjustment == adj)]
        rows = {}
        for _, r in sub.iterrows():
            d = json.loads(r.metrics)
            rows[(r.team_id, r.as_of_game_id)] = {k: (v["v"] if v else None) for k, v in d.items()}
        df = pd.DataFrame.from_dict(rows, orient="index")
        # one row per team for z-scoring (a team appears once per week)
        df.index = pd.MultiIndex.from_tuples(df.index, names=["team_id", "game_id"])
        self._z_cache[key] = df
        return df

    def z(self, team_id: str, game_id: str, key: str, window: str = "BLEND", adj: str = "OPP_ADJ") -> float | None:
        """Standardized value; sign flipped so that + is good for the team described."""
        df = self._table(window, adj)
        if key not in df.columns or (team_id, game_id) not in df.index:
            return None
        col = df[key].astype(float)
        v = col.loc[(team_id, game_id)]
        if pd.isna(v):
            return None
        mu, sd = col.mean(), col.std(ddof=0)
        if not sd or np.isnan(sd):
            return None
        zv = (v - mu) / sd
        return float(zv if self.hib.get(key, True) else -zv)

    def val(self, team_id: str, game_id: str, key: str, window: str = "BLEND", adj: str = "OPP_ADJ"):
        df = self._table(window, adj)
        if key not in df.columns or (team_id, game_id) not in df.index:
            return None
        v = df[key].loc[(team_id, game_id)]
        return None if pd.isna(v) else float(v)


def _latest(d, week: int) -> pd.DataFrame:
    """Most recent weekly file at or before `week`."""
    if not d.exists():
        return pd.DataFrame()
    files = sorted(p for p in d.glob("W*.parquet") if int(p.stem[1:]) <= week)
    return pd.read_parquet(files[-1]) if files else pd.DataFrame()


def _mean(vals: list[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return float(np.mean(v)) if v else None


def _sub(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


# ---- category builders -------------------------------------------------------------------
def unit_vs_unit(w: Week, g, off_keys: list[str], allowed_keys: list[str]) -> tuple[float | None, float | None, dict]:
    """Returns (home unit edge, away unit edge, inputs). Each = z(unit) + z(opponent allowed)."""
    h, a = g.home_team_id, g.away_team_id
    h_off = _mean([w.z(h, g.game_id, k) for k in off_keys]); a_def = _mean([w.z(a, g.game_id, k) for k in allowed_keys])
    a_off = _mean([w.z(a, g.game_id, k) for k in off_keys]); h_def = _mean([w.z(h, g.game_id, k) for k in allowed_keys])
    # allowed metrics are "higher_is_better=False" so z already means "+ = good defense"; opponent weakness = -z
    home_edge = None if h_off is None or a_def is None else h_off - a_def
    away_edge = None if a_off is None or h_def is None else a_off - h_def
    inputs = {"home_off_z": h_off, "away_def_z": a_def, "away_off_z": a_off, "home_def_z": h_def,
              "home_vals": {k: w.val(h, g.game_id, k) for k in off_keys + allowed_keys}, "away_vals": {k: w.val(a, g.game_id, k) for k in off_keys + allowed_keys}}
    return home_edge, away_edge, inputs


def cat_qb(w: Week, g) -> tuple[float | None, dict, bool]:
    if w.qb.empty:
        return None, {}, True
    q = w.qb.set_index("team_id")
    def qb_val(t):
        if t not in q.index:
            return None, {}
        r = q.loc[t]
        parts = [x for x in (r.career_ppa_dropback, ) if pd.notna(x)]
        base = float(parts[0]) if parts else None
        info = {"player": r.player_name, "basis": r.projection_basis, "confidence": float(r.confidence), "flags": r["flags"],
                "career_games": int(r.career_games_10att), "career_ypa": None if pd.isna(r.career_ypa) else round(float(r.career_ypa), 2),
                "career_ppa_dropback": None if base is None else round(base, 3), "season_att": float(r.season_att)}
        return base, info
    hv, hi = qb_val(g.home_team_id); av, ai = qb_val(g.away_team_id)
    inputs = {"home": hi, "away": ai}
    if hv is None or av is None:
        # no career EPA for one side (rookie / unknown starter): use the team's passing EPA as the only evidence, flag it
        h = w.z(g.home_team_id, g.game_id, "off_ppa_dropback"); a = w.z(g.away_team_id, g.game_id, "off_ppa_dropback")
        inputs["fallback"] = "team_pass_epa"
        return _sub(h, a), inputs, (h is None or a is None)
    # scale: 0.10 EPA/dropback ~ one SD of QB quality
    conf = min(hi.get("confidence", 0.5), ai.get("confidence", 0.5))
    return (hv - av) / 0.10 * (0.5 + 0.5 * conf), inputs, False


def cat_line(w: Week, g, offense_home: bool) -> tuple[float | None, dict]:
    """OFFENSIVE_LINE (home OL vs away front) or DEFENSIVE_FRONT (home front vs away OL)."""
    ol_team, front_team = (g.home_team_id, g.away_team_id) if offense_home else (g.away_team_id, g.home_team_id)
    prot = _mean([w.z(ol_team, g.game_id, k) for k in ("sack_rate_allowed", "off_pressure_rate_allowed", "off_havoc_allowed")])
    run_block = _mean([w.z(ol_team, g.game_id, k) for k in ("off_line_yards", "off_opportunity_rate", "off_stuff_rate_allowed", "off_power_success")])
    rush = _mean([w.z(front_team, g.game_id, k) for k in ("sack_rate", "def_pressure_rate", "def_havoc_front")])
    run_def = _mean([w.z(front_team, g.game_id, k) for k in ("def_line_yards_allowed", "def_stuff_rate")])
    pass_pro_edge = _sub(prot, rush); run_edge = _sub(run_block, run_def)
    edge = _mean([pass_pro_edge, run_edge])
    if edge is None:
        return None, {}
    signed = edge if offense_home else -edge
    return signed, {"pass_protection_vs_rush": pass_pro_edge, "run_blocking_vs_run_defense": run_edge, "ol_team": ol_team, "front_team": front_team,
                    "protection_z": prot, "pass_rush_z": rush, "run_block_z": run_block, "run_def_z": run_def}


def cat_style_fit(w: Week, g) -> tuple[float | None, dict]:
    """Does each offense attack what the other defense does badly? Product of tendency and weakness, netted."""
    def fit(off, deff):
        pr = w.z(off, g.game_id, "off_pass_rate", "SEASON", "RAW")                 # + = passes more than average
        pass_weak = w.z(deff, g.game_id, "def_ppa_pass")                            # + = good pass D -> weakness = -z
        rush_weak = w.z(deff, g.game_id, "def_ppa_rush")
        expl_t = w.z(off, g.game_id, "off_explosive_play_rate", "SEASON", "RAW")
        expl_weak = w.z(deff, g.game_id, "def_explosive_play_rate_allowed")
        terms = {}
        if pr is not None and pass_weak is not None and rush_weak is not None:
            terms["pass_lean_vs_pass_d"] = pr * (-pass_weak)
            terms["rush_lean_vs_run_d"] = (-pr) * (-rush_weak)
        if expl_t is not None and expl_weak is not None:
            terms["explosive_vs_explosive_d"] = expl_t * (-expl_weak)
        if w.league == "NFL":
            pa = w.z(off, g.game_id, "off_play_action_rate", "SEASON", "RAW"); nb = w.z(deff, g.game_id, "def_pressure_no_blitz_rate")
            if pa is not None and nb is not None:
                terms["play_action_vs_no_blitz_pressure"] = pa * (-nb)
        return (float(np.mean(list(terms.values()))) if terms else None), terms
    hf, ht = fit(g.home_team_id, g.away_team_id); af, at = fit(g.away_team_id, g.home_team_id)
    return _sub(hf, af), {"home_fit": hf, "away_fit": af, "home_terms": ht, "away_terms": at}


def cat_injury(w: Week, g) -> tuple[float | None, dict, bool]:
    """Position-weighted OUT/DOUBTFUL starters, per team, from injuries + depth charts. Net = away burden - home burden."""
    if w.inj.empty:
        return None, {}, True
    inj = w.inj[(w.inj.season == w.season) & (w.inj.week == w.week) & w.inj.status.isin(["OUT", "DOUBTFUL", "IR"])]
    if inj.empty:
        return 0.0, {"home": [], "away": []}, False
    depth = w.depth
    starters = set()
    if not depth.empty:
        starters = set(zip(depth[depth.rank_in_slot == 1].team_id, depth[depth.rank_in_slot == 1].player_id))
    def burden(t):
        rows = inj[inj.team_id == t]
        tot, items = 0.0, []
        for _, r in rows.iterrows():
            pos = str(r.position) if pd.notna(r.position) else "UNK"
            wgt = config.INJURY_POSITION_WEIGHTS.get(pos, 0.2)
            is_starter = (t, r.player_id) in starters if pd.notna(r.get("player_id")) else True   # manual CFB rows are entered for starters
            mult = 1.0 if is_starter else 0.35
            sev = 1.0 if r.status in ("OUT", "IR") else 0.6
            tot += wgt * mult * sev
            items.append({"player": r.get("player_name") if pd.notna(r.get("player_name")) else r.get("player_id"), "pos": pos, "status": r.status, "starter": bool(is_starter), "impact": round(wgt * mult * sev, 2)})
        return tot, sorted(items, key=lambda x: -x["impact"])[:8]
    hb, hi = burden(g.home_team_id); ab, ai = burden(g.away_team_id)
    return (ab - hb) / config.INJURY_SD_POINTS, {"home_burden": hb, "away_burden": ab, "home": hi, "away": ai}, False


def cat_weather(w: Week, g) -> tuple[float | None, dict, bool]:
    if w.weather.empty:
        return None, {}, True
    ws = w.weather[w.weather.game_id == g.game_id].sort_values("retrieved_at")
    if ws.empty:
        return None, {}, True
    r = ws.iloc[-1]
    if bool(r.is_indoor):
        return 0.0, {"indoor": True}, False
    wind = float(r.wind_mph) if pd.notna(r.wind_mph) else None
    if wind is None:
        return None, {"forecast_missing": True}, True
    # wind mainly suppresses passing; the run-heavier team gains a little. Margin effect small by design; totals handled in Phase 8.
    wind_factor = max(0.0, (wind - config.WIND_PASS_THRESHOLD_MPH) / 10.0)
    h_pr = w.z(g.home_team_id, g.game_id, "off_pass_rate", "SEASON", "RAW"); a_pr = w.z(g.away_team_id, g.game_id, "off_pass_rate", "SEASON", "RAW")
    raw = 0.0 if (h_pr is None or a_pr is None) else wind_factor * (a_pr - h_pr) * 0.5
    return raw, {"wind_mph": wind, "gust_mph": None if pd.isna(r.wind_gust_mph) else float(r.wind_gust_mph), "temp_f": None if pd.isna(r.temp_f) else float(r.temp_f),
                 "precip_prob": None if pd.isna(r.precip_prob) else float(r.precip_prob), "hours_to_kickoff": float(r.hours_to_kickoff), "wind_factor": wind_factor}, False


def rest_context(w: Week, g) -> dict:
    gm = w.games[w.games.kickoff_utc.notna()].copy(); gm["k"] = pd.to_datetime(gm.kickoff_utc, utc=True)
    k = pd.Timestamp(g.kickoff_utc)
    out = {}
    for side, t in (("home", g.home_team_id), ("away", g.away_team_id)):
        prev = gm[((gm.home_team_id == t) | (gm.away_team_id == t)) & (gm.k < k)].sort_values("k")
        rest = int((k - prev.k.iloc[-1]).days) if not prev.empty else None
        road_streak = 0
        for _, p in prev.iloc[::-1].iterrows():
            if p.away_team_id == t and not p.neutral_site:
                road_streak += 1
            else:
                break
        out[side] = {"rest_days": rest, "off_bye": bool(rest is not None and rest >= 13), "short_week": bool(rest is not None and rest <= 5),
                     "consecutive_road_before": road_streak, "first_game": prev.empty}
    return out


def cat_rest(w: Week, g) -> tuple[float | None, dict]:
    ctx = rest_context(w, g)
    h, a = ctx["home"], ctx["away"]
    if h["rest_days"] is None or a["rest_days"] is None:
        return 0.0, ctx
    diff = np.clip(h["rest_days"] - a["rest_days"], -7, 7) / 7.0
    raw = diff + (0.15 if a["consecutive_road_before"] >= 2 else 0.0)
    return float(raw), ctx


# ---- assemble one game -------------------------------------------------------------------
def build_game(w: Week, g, weights: dict) -> list[dict]:
    rows = []
    built_at = datetime.now(timezone.utc).isoformat()
    def add(cat, raw, inputs, unavailable=False, quality=1.0):
        raw_f = None if raw is None or (isinstance(raw, float) and np.isnan(raw)) else float(np.clip(raw, -4, 4))
        rows.append({"game_id": g.game_id, "category": cat, "edge_score": _score(raw_f) if raw_f is not None else 0, "edge_raw": raw_f,
                     "weight": weights.get(cat, 0.0), "margin_contribution": None if raw_f is None else round(weights.get(cat, 0.0) * raw_f, 3),
                     "inputs": json.dumps(inputs, default=str), "data_quality": quality, "is_unavailable": bool(unavailable or raw_f is None),
                     "model_version": config.MATCHUP_MODEL_VERSION, "built_at": built_at})
    gid = g.game_id; H, A = g.home_team_id, g.away_team_id
    # overall efficiency (interaction)
    ho, ao, inp = unit_vs_unit(w, g, ["off_ppa_play", "off_success_rate"], ["def_ppa_play", "def_success_rate"])
    add("OVERALL_OFF", ho, inp); add("OVERALL_DEF", None if ao is None else -ao, inp)
    hp, ap, inp = unit_vs_unit(w, g, ["off_ppa_pass", "off_success_pass"], ["def_ppa_pass", "def_success_pass"])
    add("PASS_OFF", hp, inp); add("PASS_DEF", None if ap is None else -ap, inp)
    hr, ar, inp = unit_vs_unit(w, g, ["off_ppa_rush", "off_success_rush"], ["def_ppa_rush", "def_success_rush"])
    add("RUSH_OFF", hr, inp); add("RUSH_DEF", None if ar is None else -ar, inp)
    raw, inp, un = cat_qb(w, g); add("QB", raw, inp, un)
    raw, inp = cat_line(w, g, True); add("OFFENSIVE_LINE", raw, inp)
    raw, inp = cat_line(w, g, False); add("DEFENSIVE_FRONT", raw, inp)
    he, ae, inp = unit_vs_unit(w, g, ["off_explosive_play_rate", "off_explosiveness"], ["def_explosive_play_rate_allowed", "def_explosiveness"])
    add("EXPLOSIVE", _sub(he, ae), inp)
    hs, as_, inp = unit_vs_unit(w, g, ["off_success_std_downs", "off_success_pass_downs"], ["def_success_std_downs", "def_success_pass_downs"])
    add("SUCCESS", _sub(hs, as_), inp)
    h3, a3, inp = unit_vs_unit(w, g, ["third_down_pct_off"], ["third_down_pct_def"]); add("THIRD_DOWN", _sub(h3, a3), inp)
    hz, az, inp = unit_vs_unit(w, g, ["off_rz_td_rate", "off_pts_per_scoring_opp"], ["def_rz_td_rate_allowed", "def_pts_per_scoring_opp_allowed"]); add("RED_ZONE", _sub(hz, az), inp)
    tm = _sub(w.z(H, gid, "turnover_margin", "SEASON", "RAW"), w.z(A, gid, "turnover_margin", "SEASON", "RAW"))
    add("TURNOVER", None if tm is None else tm * config.TURNOVER_REGRESSION, {"home": w.val(H, gid, "turnover_margin", "SEASON", "RAW"), "away": w.val(A, gid, "turnover_margin", "SEASON", "RAW"), "regression": config.TURNOVER_REGRESSION})
    add("SPECIAL_TEAMS", None, {"note": "special-teams metrics not yet ingested (Phase 4D)"}, unavailable=True)
    # coaching / roster / talent
    cont = w.cont.set_index("team_id") if not w.cont.empty else pd.DataFrame()
    hc = lambda t: (None if cont.empty or t not in cont.index or pd.isna(cont.loc[t].hc_changed) else (-1.0 if cont.loc[t].hc_changed else 0.0))
    add("COACHING", _sub(hc(H), hc(A)), {"home_hc_changed": None if hc(H) is None else hc(H) < 0, "away_hc_changed": None if hc(A) is None else hc(A) < 0}, unavailable=(hc(H) is None or hc(A) is None))
    if w.league == "CFB" and not w.talent.empty:
        tl = w.talent[w.talent.season == w.season].set_index("team_id")
        ht = tl.talent_score.get(H); at = tl.talent_score.get(A)
        add("TALENT", None if pd.isna(ht) or pd.isna(at) else (float(ht) - float(at)) * 2.0, {"home_talent_pct": ht, "away_talent_pct": at, "home_blue_chip": tl.blue_chip_ratio_4yr.get(H), "away_blue_chip": tl.blue_chip_ratio_4yr.get(A)})
    else:
        add("TALENT", None, {"note": "NFL talent not modeled" if w.league == "NFL" else "talent table missing"}, unavailable=True)
    ci = lambda t: (None if cont.empty or t not in cont.index else cont.loc[t].continuity_index)
    add("RETURNING_PROD", None if ci(H) is None or ci(A) is None else (ci(H) - ci(A)) * 2.0, {"home_continuity": ci(H), "away_continuity": ci(A)}, unavailable=(ci(H) is None or ci(A) is None))
    # recent form: blend vs season on net EPA
    def form(t):
        b = _sub(w.val(t, gid, "off_ppa_play", "BLEND"), w.val(t, gid, "def_ppa_play", "BLEND"))
        s = _sub(w.val(t, gid, "off_ppa_play", "SEASON"), w.val(t, gid, "def_ppa_play", "SEASON"))
        return None if b is None or s is None else (b - s) / 0.10
    add("RECENT_FORM", _sub(form(H), form(A)), {"home_form_shift": form(H), "away_form_shift": form(A)})
    if not w.rat.empty and H in w.rat.index and A in w.rat.index:
        sos = w.rat.sos; sd = sos.std(ddof=0) or 1.0
        add("SOS", float((w.rat.loc[H].sos - w.rat.loc[A].sos) / sd), {"home_sos": float(w.rat.loc[H].sos), "away_sos": float(w.rat.loc[A].sos), "home_sos_rank": int(w.rat.loc[H].sos_rank), "away_sos_rank": int(w.rat.loc[A].sos_rank)})
        hfa = float(w.rat.hfa_league.iloc[0])
    else:
        add("SOS", None, {}, unavailable=True); hfa = config.HFA_DEFAULT_POINTS[w.league]
    neutral = bool(g.neutral_site)
    add("HOME_FIELD", 0.0 if neutral else 1.0, {"neutral_site": neutral, "hfa_points_league": hfa}, quality=1.0)
    rows[-1]["weight"] = hfa; rows[-1]["margin_contribution"] = 0.0 if neutral else round(hfa, 3)   # HFA is in points already
    raw, inp = cat_style_fit(w, g); add("STYLE_FIT", raw, inp)
    raw, inp, un = cat_injury(w, g); add("INJURY", raw, inp, un)
    raw, inp, un = cat_weather(w, g); add("WEATHER", raw, inp, un)
    raw, inp = cat_rest(w, g); add("REST", raw, inp)
    return rows


def build_week(league: str, season: int, week: int) -> pd.DataFrame:
    w = Week(league, season, week)
    if w.metrics.empty:
        return pd.DataFrame()
    wk = w.games[(w.games.week == week) & (w.games.season_type == "REG") & w.games.kickoff_utc.notna()]
    wk = wk[~wk.home_team_id.str.startswith("CFB_FCS") & ~wk.away_team_id.str.startswith("CFB_FCS")]
    weights = config.MATCHUP_WEIGHTS_INIT[league]
    rows = []
    for _, g in wk.iterrows():
        rows.extend(build_game(w, g, weights))
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # weighted matchup advantage per game (prelim points, HOME perspective) for the status report / UI
    tot = df[~df.is_unavailable].groupby("game_id").margin_contribution.sum()
    df["prelim_margin_home"] = df.game_id.map(tot)
    return df
