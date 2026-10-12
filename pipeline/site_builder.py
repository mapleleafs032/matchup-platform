"""
Site builder (master prompt §4-5, §49, §52, §57; Phase 3 §8 JSON contracts). The browser only ever reads these files.

  site/json/manifest.json                      current week per league, slate paths, version stamp (cache-busting)
  site/json/slate/{league}/{season}/W{ww}.json every game card for the week (one fetch for the landing page)
  site/json/matchup/{game_id}.json             everything the matchup page shows
  site/json/status.json                        admin/debug page

LOCKED or FINAL games are rendered from data/snapshots/pregame_{game_id}.json (frozen at kickoff), never from live tables.
"""
from __future__ import annotations
import glob
import hashlib
import json
from datetime import datetime, timezone

import pandas as pd

import config
from pipeline import storage
from pipeline.matchup_engine import CATEGORIES

AN = config.TABLES / "analytics"
ROSTER = config.TABLES / "roster"
MODEL = config.TABLES / "model"
OUT = config.SITE_JSON

GRID_ROWS = [  # §57 comparison grid, in display order
    ("SCORING", ["points_per_game", "points_allowed_per_game", "scoring_margin"]),
    ("EFFICIENCY", ["yards_per_play_off", "yards_per_play_def", "off_ppa_play", "def_ppa_play", "off_success_rate", "def_success_rate"]),
    ("RUSHING", ["rush_yds_per_game", "opp_rush_yds_per_game", "yards_per_rush", "opp_yards_per_rush", "off_ppa_rush", "def_ppa_rush"]),
    ("PASSING", ["pass_yds_per_game", "opp_pass_yds_per_game", "yards_per_pass", "opp_yards_per_pass", "completion_pct", "off_ppa_pass", "def_ppa_pass"]),
    ("TRENCHES", ["sack_rate_allowed", "sack_rate", "off_pressure_rate_allowed", "def_pressure_rate", "off_line_yards", "def_line_yards_allowed", "off_havoc_allowed", "def_havoc"]),
    ("SITUATIONAL", ["third_down_pct_off", "third_down_pct_def", "off_rz_td_rate", "def_rz_td_rate_allowed", "off_pts_per_scoring_opp", "def_pts_per_scoring_opp_allowed"]),
    ("EXPLOSIVE", ["off_explosive_play_rate", "def_explosive_play_rate_allowed", "off_explosiveness", "def_explosiveness"]),
    ("TURNOVERS", ["turnover_margin", "giveaways_per_game", "takeaways_per_game", "int_rate"]),
    ("STYLE / PACE", ["off_pass_rate", "off_neutral_pass_rate", "off_sec_per_play", "plays_per_game", "off_play_action_rate", "def_blitz_rate"]),
]
WINDOWS_SHOWN = ["SEASON", "LAST5", "LAST3", "HOME", "AWAY", "CONF"]
CATEGORY_LABELS = {"OVERALL_OFF": "Overall offense", "OVERALL_DEF": "Overall defense", "PASS_OFF": "Passing offense", "PASS_DEF": "Passing defense", "RUSH_OFF": "Rushing offense",
                   "RUSH_DEF": "Rushing defense", "QB": "Quarterback", "OFFENSIVE_LINE": "Offensive line", "DEFENSIVE_FRONT": "Defensive front", "EXPLOSIVE": "Explosive plays",
                   "SUCCESS": "Down-and-distance efficiency", "THIRD_DOWN": "Third down", "RED_ZONE": "Red zone", "TURNOVER": "Turnovers", "SPECIAL_TEAMS": "Special teams",
                   "COACHING": "Coaching", "TALENT": "Roster talent", "RETURNING_PROD": "Roster continuity", "RECENT_FORM": "Recent form", "SOS": "Strength of schedule",
                   "HOME_FIELD": "Home field", "STYLE_FIT": "Style fit", "INJURY": "Injuries", "WEATHER": "Weather", "REST": "Rest and travel"}


def _now():
    return datetime.now(timezone.utc)


def _json_default(o):
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if pd.isna(o) if not isinstance(o, (list, dict, str)) else False:
        return None
    return str(o)


def _clean(v):
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    return v


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, default=_json_default, separators=(",", ":")))


# ---- shared loaders ----------------------------------------------------------------------
class Season:
    def __init__(self, league: str, season: int):
        self.league, self.season = league, season
        self.games = storage.read_table(storage.games_path(league, season))
        tm = storage.read_table(config.TABLES / "ref" / "teams.parquet")
        self.teams = tm.set_index("team_id") if not tm.empty else pd.DataFrame(columns=["display_name", "school_or_city", "mascot", "abbr", "logo_url", "primary_color", "conference"])
        self.results = storage.read_table(config.TABLES / "results" / league / f"{season}.csv")
        self.results = self.results.set_index("game_id") if not self.results.empty else self.results
        self.preds = storage.read_table(MODEL / "predictions" / league / f"{season}.csv")
        self.latest_pred = self.preds.sort_values("predicted_at").drop_duplicates("game_id", keep="last").set_index("game_id") if not self.preds.empty else pd.DataFrame()
        self.rankings = storage.read_table(config.TABLES / "context" / "rankings" / f"{season}.parquet") if league == "CFB" else pd.DataFrame()
        self.reg = storage.read_table(config.TABLES / "ref" / "metric_definitions.csv")
        self.reg = self.reg.set_index("metric_key") if not self.reg.empty else self.reg
        self.venues = storage.read_table(config.TABLES / "ref" / "venues.parquet")
        self.venues = self.venues.set_index("venue_id") if not self.venues.empty else self.venues
        self.eval = storage.read_table(MODEL / "model_evaluation" / league / f"{season}.csv")
        self.snap_index = storage.read_table(MODEL / "pregame_snapshots_index.csv")
        self.snap_index = self.snap_index.set_index("game_id") if not self.snap_index.empty else self.snap_index
        self.ai_index = storage.read_table(MODEL / "ai_analyses_index.csv")
        pl = storage.read_table(config.TABLES / "ref" / "players" / f"{league}.parquet")
        self.player_names = dict(zip(pl.player_id, pl.full_name)) if not pl.empty else {}
        self._cache: dict = {}

    def week_table(self, name: str, week: int) -> pd.DataFrame:
        key = (name, week)
        if key not in self._cache:
            p = {"metrics": AN / "team_metrics_asof", "edges": AN / "matchup_edges", "qb": ROSTER / "qb_status", "market": AN / "market_analysis"}[name] / self.league / str(self.season) / f"W{week:02d}.parquet"
            self._cache[key] = pd.read_parquet(p) if p.exists() else pd.DataFrame()
        return self._cache[key]

    def record(self, team_id: str, before: pd.Timestamp) -> str:
        if self.results.empty:
            return "0-0"
        g = self.games[((self.games.home_team_id == team_id) | (self.games.away_team_id == team_id)) & (pd.to_datetime(self.games.kickoff_utc, utc=True) < before)]
        w = l = 0
        for _, r in g.iterrows():
            if r.game_id not in self.results.index:
                continue
            res = self.results.loc[r.game_id]
            margin = res.margin_home if r.home_team_id == team_id else -res.margin_home
            w += margin > 0; l += margin < 0
        return f"{w}-{l}"

    def rank(self, team_id: str, week: int):
        if self.rankings.empty:
            return None
        r = self.rankings[(self.rankings.week == week) & (self.rankings.poll == "AP") & (self.rankings.team_id == team_id)]
        return int(r["rank"].iloc[0]) if not r.empty else None

    def team_head(self, team_id: str, week: int, before: pd.Timestamp) -> dict:
        t = self.teams.loc[team_id] if team_id in self.teams.index else None
        short = (t.mascot if self.league == "NFL" else t.school_or_city) if t is not None else team_id.split("_", 1)[1]
        return {"team_id": team_id, "name": t.display_name if t is not None else team_id.split("_", 1)[1], "short": short,
                "abbr": t.abbr if t is not None else team_id.split("_", 1)[1], "logo": t.logo_url if t is not None else None, "color": t.primary_color if t is not None else None,
                "conf": t.conference if t is not None else None, "record": self.record(team_id, before), "rank": self.rank(team_id, week), "is_fcs": team_id.startswith("CFB_FCS")}


# ---- slate --------------------------------------------------------------------------------
def build_slate(S: Season, week: int) -> dict:
    wk = S.games[(S.games.week == week) & (S.games.season_type == "REG")].copy()
    wk["k"] = pd.to_datetime(wk.kickoff_utc, utc=True)
    wk = wk.sort_values("k")
    mkt = S.week_table("market", week)
    mkt = mkt.set_index("game_id") if not mkt.empty else mkt
    edges = S.week_table("edges", week)
    cards = []
    for _, g in wk.iterrows():
        p = S.latest_pred.loc[g.game_id] if not S.latest_pred.empty and g.game_id in S.latest_pred.index else None
        m = mkt.loc[g.game_id] if not mkt.empty and g.game_id in mkt.index else None
        e = edges[(edges.game_id == g.game_id) & (~edges.is_unavailable) & (edges.category != "HOME_FIELD")] if not edges.empty else pd.DataFrame()
        key_edge = None
        if not e.empty:
            top = e.reindex(e.margin_contribution.abs().sort_values(ascending=False).index).iloc[0]
            key_edge = {"category": top.category, "label": CATEGORY_LABELS.get(top.category, top.category), "side": "home" if top.margin_contribution > 0 else "away", "points": round(float(top.margin_contribution), 1), "score": int(top.edge_score)}
        res = S.results.loc[g.game_id] if not S.results.empty and g.game_id in S.results.index else None
        ev = S.eval[S.eval.game_id == g.game_id] if not S.eval.empty else pd.DataFrame()
        venue = S.venues.loc[g.venue_id] if not S.venues.empty and pd.notna(g.venue_id) and g.venue_id in S.venues.index else None
        cards.append({
            "game_id": g.game_id, "status": g.status, "kickoff_utc": g.k.isoformat() if pd.notna(g.k) else None, "kickoff_is_tba": bool(g.kickoff_is_tba), "tv": _clean(g.get("tv_network")),
            "venue": {"name": _clean(g.get("venue_name")) or (venue.name if venue is not None else None), "city": venue.city if venue is not None else None, "roof": _clean(g.get("venue_roof")) or (venue.roof if venue is not None else None)},
            "neutral_site": bool(g.neutral_site), "conference_game": None if pd.isna(g.conference_game) else bool(g.conference_game), "is_fcs_game": bool(g.is_fcs_game),
            "away": S.team_head(g.away_team_id, week, g.k), "home": S.team_head(g.home_team_id, week, g.k),
            "market": None if m is None else {"spread_home": _clean(m.current_spread_home), "spread_open_home": _clean(m.open_spread_home), "total": _clean(m.current_total), "total_open": _clean(m.open_total),
                                              "spread_move": _clean(m.spread_move), "key_numbers": _clean(m.key_numbers), "book": _clean(m.primary_book), "wp_home_no_vig": _clean(m.market_wp_home),
                                              "ml_home": None, "ml_away": None, "ticket_pct_home": None, "money_pct_home": None},
            "model": None if p is None else {"proj_away": float(p.proj_away_pts), "proj_home": float(p.proj_home_pts), "proj_margin_home": float(p.proj_margin_home), "proj_total": float(p.proj_total),
                                             "win_prob_home": float(p.win_prob_home), "spread_diff": _clean(p.spread_diff), "total_diff": _clean(p.total_diff), "model_version": p.model_version,
                                             "data_quality": float(p.data_quality), "predicted_at": p.predicted_at},
            "key_edge": key_edge,
            "result": None if res is None else {"away": int(res.away_score), "home": int(res.home_score),
                                                "model_ats": None if ev.empty else _clean(ev.iloc[0].model_ats_result), "winner_correct": None if ev.empty else _clean(ev.iloc[0].winner_correct)},
            "filters": {"ranked": bool(S.rank(g.home_team_id, week) or S.rank(g.away_team_id, week)), "favorite": None if m is None or pd.isna(m.current_spread_home) else ("home" if m.current_spread_home < 0 else "away"),
                        "conf_home": S.teams.loc[g.home_team_id].conference if g.home_team_id in S.teams.index else None, "conf_away": S.teams.loc[g.away_team_id].conference if g.away_team_id in S.teams.index else None,
                        "date": g.k.tz_convert("America/Chicago").strftime("%Y-%m-%d") if pd.notna(g.k) else None},
        })
    return {"league": S.league, "season": S.season, "week": week, "generated_at": _now().isoformat(), "games": cards}


# ---- matchup ------------------------------------------------------------------------------
def _metric_cell(d: dict, key: str):
    v = d.get(key) if d else None
    return None if v is None else {"v": v["v"], "rank": v["rank"], "pct": v["pct"], "n": v["n"], "low_n": v.get("low_n", False)}


def build_matchup(S: Season, week: int, game_id: str, card: dict | None = None) -> dict:
    g = S.games[S.games.game_id == game_id].iloc[0]
    kick = pd.Timestamp(g.kickoff_utc) if pd.notna(g.kickoff_utc) else None
    snap_path = config.SNAPSHOTS / f"pregame_{game_id}.json"
    frozen = g.status in ("LOCKED", "FINAL") and snap_path.exists()
    if frozen:
        snap = json.loads(snap_path.read_text())
        metrics = pd.DataFrame(snap.get("team_metrics_asof", [])); edges = pd.DataFrame(snap.get("matchup_edges", []))
        qb = pd.DataFrame(snap.get("qb_status", [])); inj = pd.DataFrame(snap.get("injuries", []))
        pred = snap.get("prediction"); wx = (snap.get("weather") or [None])[-1] if snap.get("weather") else None
        market_hist = snap.get("market_history", []); closing = snap.get("closing_lines", [])
    else:
        metrics = S.week_table("metrics", week); metrics = metrics[metrics.as_of_game_id == game_id] if not metrics.empty else metrics
        edges = S.week_table("edges", week); edges = edges[edges.game_id == game_id] if not edges.empty else edges
        qb = S.week_table("qb", week); qb = qb[qb.team_id.isin([g.home_team_id, g.away_team_id])] if not qb.empty else qb
        inj_all = storage.read_table(ROSTER / "injuries" / S.league / f"{S.season}.csv")
        inj = inj_all[(inj_all.week == week) & inj_all.team_id.isin([g.home_team_id, g.away_team_id]) & inj_all.status.isin(["OUT", "DOUBTFUL", "QUESTIONABLE", "IR"])] if not inj_all.empty else pd.DataFrame()
        pred = S.latest_pred.loc[game_id].to_dict() if not S.latest_pred.empty and game_id in S.latest_pred.index else None
        wpath = config.TABLES / "context" / "weather_snapshots" / S.league / str(S.season) / f"W{week:02d}.csv"
        wxdf = pd.read_csv(wpath) if wpath.exists() else pd.DataFrame()
        wxr = wxdf[wxdf.game_id == game_id].sort_values("retrieved_at") if not wxdf.empty else pd.DataFrame()
        wx = wxr.iloc[-1].to_dict() if not wxr.empty else None
        market_hist, closing = [], []
    # market analysis json (built by the market engine; for frozen games it is capped at kickoff there too)
    mpath = OUT / "market" / f"{game_id}.json"
    market = json.loads(mpath.read_text()) if mpath.exists() else None
    # comparison grid
    def side_metrics(tid):
        out = {}
        for w in WINDOWS_SHOWN:
            for adj in ("RAW", "OPP_ADJ"):
                row = metrics[(metrics.team_id == tid) & (metrics.window == w) & (metrics.adjustment == adj)] if not metrics.empty else pd.DataFrame()
                out[(w, adj)] = json.loads(row.iloc[0].metrics) if not row.empty else {}
        return out
    am, hm = side_metrics(g.away_team_id), side_metrics(g.home_team_id)
    rows = []
    for group, keys in GRID_ROWS:
        for k in keys:
            if S.reg.empty or k not in S.reg.index:
                continue
            r = S.reg.loc[k]
            if S.league not in str(r.leagues):
                continue
            rows.append({"metric_key": k, "label": r.label, "description": r.description, "group": group, "unit": r.unit, "higher_is_better": bool(r.higher_is_better),
                         "away": {f"{w}:{adj}": _metric_cell(am[(w, adj)], k) for w in WINDOWS_SHOWN for adj in ("RAW", "OPP_ADJ")},
                         "home": {f"{w}:{adj}": _metric_cell(hm[(w, adj)], k) for w in WINDOWS_SHOWN for adj in ("RAW", "OPP_ADJ")}})
    games_n = {"away": int(metrics[(metrics.team_id == g.away_team_id) & (metrics.window == "SEASON")].games_n.max()) if not metrics.empty else 0,
               "home": int(metrics[(metrics.team_id == g.home_team_id) & (metrics.window == "SEASON")].games_n.max()) if not metrics.empty else 0}
    quality = {"away": float(metrics[(metrics.team_id == g.away_team_id) & (metrics.window == "BLEND") & (metrics.adjustment == "OPP_ADJ")].data_quality.max()) if not metrics.empty else None,
               "home": float(metrics[(metrics.team_id == g.home_team_id) & (metrics.window == "BLEND") & (metrics.adjustment == "OPP_ADJ")].data_quality.max()) if not metrics.empty else None,
               "flags": sorted(set(",".join(metrics.quality_flags.astype(str)).split(",")) - {""}) if not metrics.empty else []}
    # edges
    edge_list = []
    for c in CATEGORIES:
        e = edges[edges.category == c] if not edges.empty else pd.DataFrame()
        if e.empty:
            edge_list.append({"category": c, "label": CATEGORY_LABELS[c], "unavailable": True}); continue
        e = e.iloc[0]
        inputs = json.loads(e.inputs) if isinstance(e.inputs, str) else (e.inputs or {})
        edge_list.append({"category": c, "label": CATEGORY_LABELS[c], "score": None if pd.isna(e.edge_score) else int(e.edge_score), "raw": _clean(e.edge_raw),
                          "points_home": _clean(e.margin_contribution), "weight": _clean(e.weight), "unavailable": bool(e.is_unavailable), "inputs": inputs})
    # QB / injuries / AI
    def qb_block(tid):
        r = qb[qb.team_id == tid] if not qb.empty else pd.DataFrame()
        if r.empty:
            return None
        r = r.iloc[0]
        return {"name": _clean(r.player_name), "basis": r.projection_basis, "confidence": _clean(r.confidence), "flags": str(r["flags"]).split(","),
                "career_games": _clean(r.career_games_10att), "career_att": _clean(r.career_att), "career_cmp_pct": _clean(r.career_cmp_pct), "career_ypa": _clean(r.career_ypa),
                "career_td": _clean(r.career_td), "career_int": _clean(r.career_int), "career_epa_dropback": _clean(r.career_ppa_dropback), "season_att": _clean(r.season_att)}
    def inj_block(tid):
        r = inj[inj.team_id == tid] if not inj.empty else pd.DataFrame()
        return [{"player": _clean(x.get("player_name")) or S.player_names.get(_clean(x.get("player_id")), _clean(x.get("player_id"))), "position": _clean(x.position), "status": x.status, "desc": _clean(x.get("injury_desc")), "source": x.source} for _, x in r.iterrows()][:12]
    ai = None
    if not S.ai_index.empty:
        a = S.ai_index[(S.ai_index.game_id == game_id) & (~S.ai_index.validation_failed.astype(bool))].sort_values("generated_at")
        if not a.empty:
            ap = MODEL / "ai_analyses" / S.league / str(S.season) / f"{a.iloc[-1].analysis_id}.json"
            if ap.exists():
                d = json.loads(ap.read_text()); ai = {"generated_at": d["generated_at"], "model": d["llm_model"], "inputs_hash": d["inputs_hash"], "sections": d["sections"]}
        pending = S.ai_index[(S.ai_index.game_id == game_id) & (S.ai_index.validation_failed.astype(bool))]
        if ai is None and not pending.empty:
            ai = {"withheld": True, "reason": "The generated analysis referenced numbers not present in its data package and is withheld pending review."}
    res = S.results.loc[game_id] if not S.results.empty and game_id in S.results.index else None
    ev = S.eval[S.eval.game_id == game_id] if not S.eval.empty else pd.DataFrame()
    why = json.loads(pred["contributions"]) if pred and isinstance(pred.get("contributions"), str) else (pred.get("contributions") if pred else [])
    return {
        "game": card if card is not None else build_slate_card(S, week, g), "status": g.status, "frozen_from_snapshot": frozen, "locked_at": _clean(g.locked_at),
        "teams": {"away": {"identity": S.team_head(g.away_team_id, week, kick), "qb": qb_block(g.away_team_id), "injuries": inj_block(g.away_team_id), "games_n": games_n["away"], "data_quality": quality["away"]},
                  "home": {"identity": S.team_head(g.home_team_id, week, kick), "qb": qb_block(g.home_team_id), "injuries": inj_block(g.home_team_id), "games_n": games_n["home"], "data_quality": quality["home"]}},
        "metrics": {"windows": WINDOWS_SHOWN, "default_window": "SEASON", "rows": rows, "quality_flags": quality["flags"]},
        "edges": edge_list,
        "model": None if pred is None else {"proj_away": float(pred["proj_away_pts"]), "proj_home": float(pred["proj_home_pts"]), "proj_margin_home": float(pred["proj_margin_home"]), "proj_total": float(pred["proj_total"]),
                                            "win_prob_home": float(pred["win_prob_home"]), "margin_sd": _clean(pred.get("margin_sd")), "model_version": pred["model_version"], "predicted_at": pred["predicted_at"],
                                            "market_spread_home": _clean(pred.get("market_spread_home")), "market_total": _clean(pred.get("market_total")), "spread_diff": _clean(pred.get("spread_diff")), "total_diff": _clean(pred.get("total_diff")),
                                            "data_quality": _clean(pred.get("data_quality")), "quality_flags": _clean(pred.get("quality_flags")), "why": why},
        "market": None if market is None else {k: market.get(k) for k in ("available", "primary_book", "open", "current", "movement", "steam", "book_disagreement_spread", "implied", "model_vs_market", "notes", "n_snapshots", "last_snapshot", "public_note")},
        "market_history_url": f"json/market/{game_id}.json",
        "closing_lines": closing,
        "weather": None if wx is None else {k: _clean(wx.get(k)) for k in ("temp_f", "feels_like_f", "wind_mph", "wind_gust_mph", "wind_dir_deg", "precip_prob", "precip_in", "humidity_pct", "is_indoor", "hours_to_kickoff", "retrieved_at", "source")},
        "ai": ai,
        "result": None if res is None else {"away": int(res.away_score), "home": int(res.home_score), "evaluation": None if ev.empty else {k: _clean(ev.iloc[0][k]) for k in ("margin_error", "abs_margin_error", "total_error", "winner_correct", "model_ats_result", "model_ou_result")}},
        "sources": {"metrics": "CFBD PPA/advanced stats" if S.league == "CFB" else "nflverse play-by-play (nflfastR EPA), FTN charting, PFR pressures",
                    "lines": "CollegeFootballData lines" if S.league == "CFB" else "The Odds API", "weather": "Open-Meteo", "injuries": "manual entries" if S.league == "CFB" else "official league reports via nflverse",
                    "note": "PPA (CFB) and EPA (NFL) are related but not identical metrics and are never mixed."},
        "generated_at": _now().isoformat(),
    }


def build_slate_card(S: Season, week: int, g) -> dict:
    """A single card in slate format (reused as the matchup page header)."""
    return next((c for c in build_slate(S, week)["games"] if c["game_id"] == g.game_id), None)


# ---- status (admin page) -------------------------------------------------------------------
def build_status() -> dict:
    jl = storage.read_table(config.TABLES / "ops" / "job_log.csv")
    vl = storage.read_table(config.TABLES / "ops" / "validation_log.csv")
    bud = storage.read_table(config.TABLES / "ops" / "api_budget.csv")
    now = _now()
    jobs = []
    if not jl.empty:
        for name, g in jl.sort_values("started_at").groupby("job_name"):
            last = g.iloc[-1]
            last_ok = g[g.status == "SUCCESS"]
            jobs.append({"job": name, "last_run": last.started_at, "last_status": last.status, "last_message": _clean(last.message), "last_success": last_ok.iloc[-1].started_at if not last_ok.empty else None,
                         "rows": _clean(last.rows_written), "api_calls": _clean(last.api_calls), "hours_since_success": None if last_ok.empty else round((now - pd.Timestamp(last_ok.iloc[-1].started_at)).total_seconds() / 3600, 1)})
    month = now.strftime("%Y-%m")
    budget = []
    if not bud.empty:
        m = bud[bud.day.astype(str).str.startswith(month)].groupby("provider").agg(calls=("requests", "sum"), credits=("credits", "sum"), failures=("failures", "sum"), remaining_reported=("remaining_reported", "last")).reset_index()
        for _, r in m.iterrows():
            budget.append({"provider": r.provider, "calls": int(r.calls), "credits": int(r.credits), "monthly_limit": config.API_BUDGET.get(r.provider, {}).get("monthly"), "failures": int(r.failures), "remaining_reported": _clean(r.remaining_reported)})
    recent_val = vl.tail(50).to_dict("records") if not vl.empty else []
    unmatched = sorted(set(vl[vl.rule == "ALIAS_UNMATCHED"].observed.astype(str))) if not vl.empty else []
    freshness = {}
    for label, pat in (("odds NFL", "market/snapshots/NFL/*/W*.csv"), ("odds CFB", "market/snapshots/CFB/*/W*.csv"), ("stats NFL", "stats/team_game_advanced/NFL/*.parquet"),
                       ("stats CFB", "stats/team_game_advanced/CFB/*.parquet"), ("weather", "context/weather_snapshots/*/*/W*.csv"), ("predictions", "model/predictions/*/*.csv"),
                       ("ai analyses", "model/ai_analyses_index.csv"), ("rosters NFL", "roster/roster_snapshots/NFL/*/W*.parquet"), ("rosters CFB", "roster/roster_snapshots/CFB/*/W*.parquet")):
        files = glob.glob(str(config.TABLES / pat))
        freshness[label] = max((datetime.fromtimestamp(__import__("os").path.getmtime(f), tz=timezone.utc) for f in files), default=None)
    freshness = {k: (v.isoformat() if v else None) for k, v in freshness.items()}
    mvp = MODEL / "model_versions.json"
    models = []
    if mvp.exists():
        for k, v in json.loads(mvp.read_text()).items():
            o = (v.get("backtest_summary") or {}).get("overall", {})
            models.append({"model_version": k, "league": v["league"], "active": v.get("is_active"), "trained_on": v["trained_on_seasons"], "n_train": v["n_train"], "oos_mae": o.get("mae_margin"), "oos_winner_acc": o.get("winner_acc"), "market_mae": o.get("market_mae_margin"), "created_at": v["created_at"]})
    live_eval = []
    for lg in config.LEAGUES:
        ev = storage.read_table(MODEL / "model_evaluation" / lg / f"{config.SEASON}.csv")
        if not ev.empty:
            ats = ev[ev.model_ats_result.isin(["WIN", "LOSS"])]
            live_eval.append({"league": lg, "n": int(len(ev)), "mae": round(float(ev.abs_margin_error.mean()), 2), "winner_acc": round(float(ev.winner_correct.astype(float).mean()), 3),
                              "ats": None if ats.empty else round(float((ats.model_ats_result == "WIN").mean()), 3), "ats_n": int(len(ats))})
    return {"generated_at": now.isoformat(), "jobs": jobs, "api_budget": budget, "validation_recent": recent_val, "unmatched_aliases": unmatched[:100], "freshness": freshness, "models": models, "live_evaluation": live_eval,
            "workflows": {"Refresh team stats": "stats.yml", "Refresh odds": "odds.yml", "Refresh rosters": "roster.yml", "Rebuild matchups": "matchups.yml", "Regenerate AI analysis": "ai.yml", "Rebuild site": "site.yml", "Backtest": "backtest.yml"}}


# ---- orchestration -------------------------------------------------------------------------
def build_all(leagues: list[str], season: int, weeks_back: int = 1, weeks_ahead: int = 1, weeks_override: list[int] | None = None) -> dict:
    manifest = {"generated_at": _now().isoformat(), "season": season, "current_week": {}, "slates": {}, "weeks": {}, "leagues": leagues}
    counts = {}
    for lg in leagues:
        S = Season(lg, season)
        if S.games.empty:
            continue
        reg = S.games[S.games.season_type == "REG"]
        sched = reg[reg.status == "SCHEDULED"]
        cur = int(sched.week.min()) if not sched.empty else int(reg.week.max())
        weeks = weeks_override or [w for w in range(cur - weeks_back, cur + weeks_ahead + 1) if w >= 1 and w in set(reg.week)]
        manifest["current_week"][lg] = cur if cur in weeks else weeks[0]
        manifest["weeks"][lg] = weeks
        manifest["slates"][lg] = {str(w): f"json/slate/{lg}/{season}/W{w:02d}.json" for w in weeks}
        n = 0
        for w in weeks:
            slate = build_slate(S, w)
            _write(OUT / "slate" / lg / str(season) / f"W{w:02d}.json", slate)
            for c in slate["games"]:
                _write(OUT / "matchup" / f"{c['game_id']}.json", build_matchup(S, w, c["game_id"], card=c))
                n += 1
        counts[lg] = n
    _write(OUT / "status.json", build_status())
    manifest["version"] = hashlib.sha256(json.dumps(manifest["slates"], sort_keys=True).encode() + manifest["generated_at"].encode()).hexdigest()[:8]
    _write(OUT / "manifest.json", manifest)
    return counts
