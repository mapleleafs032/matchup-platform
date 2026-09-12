"""
python -m pipeline.jobs.build_site                 # both leagues, previous + current + next week
python -m pipeline.jobs.build_site --weeks 2 3     # explicit weeks
Writes site/json/** (manifest, slates, matchups, market history, status). The static pages in site/ read
only these files (§2, §49). LOCKED/FINAL games are rendered from data/snapshots/pregame_{game_id}.json (§27).
"""
from __future__ import annotations
import argparse
import glob
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import config
from pipeline import storage, market_engine, splits_engine, picks_engine
from pipeline.log import JobRun

AN = config.TABLES / "analytics"
ROSTER = config.TABLES / "roster"
OUT = config.SITE_JSON
WINDOWS = ["SEASON", "LAST5", "LAST3", "HOME", "AWAY", "CONF"]
CAT_LABEL = {"OVERALL_OFF": "Overall offense", "OVERALL_DEF": "Overall defense", "PASS_OFF": "Passing offense", "PASS_DEF": "Passing defense", "RUSH_OFF": "Rushing offense",
             "RUSH_DEF": "Rushing defense", "QB": "Quarterback", "OFFENSIVE_LINE": "Offensive line", "DEFENSIVE_FRONT": "Defensive front", "EXPLOSIVE": "Explosiveness",
             "SUCCESS": "Success rate", "THIRD_DOWN": "Third down", "RED_ZONE": "Red zone", "TURNOVER": "Turnovers", "SPECIAL_TEAMS": "Special teams", "COACHING": "Coaching",
             "TALENT": "Roster talent", "RETURNING_PROD": "Returning production", "RECENT_FORM": "Recent form", "SOS": "Strength of schedule", "HOME_FIELD": "Home field",
             "STYLE_FIT": "Style of play fit", "INJURY": "Injuries", "WEATHER": "Weather", "REST": "Rest and travel"}


def _j(x):
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return x.item() if hasattr(x, "item") else x


def _local_date(kick, tz: str) -> str | None:
    if kick is None or pd.isna(kick):
        return None
    try:
        return pd.Timestamp(kick).tz_convert(ZoneInfo(tz)).strftime("%Y-%m-%d")
    except Exception:
        return pd.Timestamp(kick).strftime("%Y-%m-%d")


class Season:
    """Everything loaded once per league-season."""

    def __init__(self, league: str, season: int):
        self.league, self.season = league, season
        self.games = storage.read_table(storage.games_path(league, season))
        t = storage.read_table(config.TABLES / "ref" / "teams.parquet"); self.teams = t.set_index("team_id") if not t.empty else t
        self.venues = storage.read_table(config.TABLES / "ref" / "venues.parquet")
        self.venues = self.venues.set_index("venue_id") if not self.venues.empty else self.venues
        self.res = storage.read_table(config.TABLES / "results" / league / f"{season}.csv")
        self.res = self.res.set_index("game_id") if not self.res.empty else self.res
        p = storage.read_table(config.TABLES / "model" / "predictions" / league / f"{season}.csv")
        self.preds = p.sort_values("predicted_at").drop_duplicates("game_id", keep="last").set_index("game_id") if not p.empty else pd.DataFrame()
        si = storage.read_table(config.TABLES / "model" / "pregame_snapshots_index.csv")
        self.snap_idx = si.set_index("game_id") if not si.empty else si
        ev = storage.read_table(config.TABLES / "model" / "model_evaluation" / league / f"{season}.csv")
        self.eval = ev.set_index("game_id") if not ev.empty else ev
        rk = storage.read_table(config.TABLES / "context" / "rankings" / f"{season}.parquet") if league == "CFB" else pd.DataFrame()
        self.ranks = {}
        if not rk.empty:
            ap = rk[rk.poll == "AP"]; ap = ap[ap.week == ap.week.max()]; self.ranks = dict(zip(ap.team_id, ap["rank"].astype(int)))
        self.records = self._records()
        self.reg = storage.read_table(config.TABLES / "ref" / "metric_definitions.csv")
        ai = storage.read_table(config.TABLES / "model" / "ai_analyses_index.csv")
        self.ai_idx = ai[ai.league == league] if not ai.empty else ai
        cont = storage.read_table(ROSTER / "continuity" / league / f"{season}.parquet"); self.cont = cont.drop_duplicates("team_id", keep="last").set_index("team_id") if not cont.empty else cont
        rp = storage.read_table(ROSTER / "returning_production" / league / f"{season}.parquet")
        self.rp = rp[rp.method == "derived_position_weighted"].sort_values("as_of_week").drop_duplicates("team_id", keep="last").set_index("team_id") if not rp.empty else rp
        self.mv = self._model_versions()

    def _records(self) -> dict:
        rec: dict = {}
        if self.res.empty or self.games.empty:
            return rec
        g = self.games.set_index("game_id")
        for gid, r in self.res.iterrows():
            if gid not in g.index:
                continue
            h, a = g.loc[gid].home_team_id, g.loc[gid].away_team_id
            for t, won in ((h, r.margin_home > 0), (a, r.margin_home < 0)):
                w, l = rec.get(t, (0, 0)); rec[t] = (w + int(won), l + int(not won))
        return {t: f"{w}-{l}" for t, (w, l) in rec.items()}

    def _model_versions(self) -> dict:
        p = config.TABLES / "model" / "model_versions.json"
        return json.loads(p.read_text()) if p.exists() else {}

    # ---- helpers ----
    def team(self, tid: str) -> dict:
        t = self.teams.loc[tid] if not self.teams.empty and tid in self.teams.index else None
        is_fcs = tid.startswith("CFB_FCS")
        name = t.display_name if t is not None else ("FCS opponent" if is_fcs else tid)
        return {"team_id": tid, "name": name, "short": (t.school_or_city if t is not None else name), "abbr": (t.abbr if t is not None else tid.split("_")[1]),
                "logo": _j(t.logo_url) if t is not None else None, "record": self.records.get(tid, "0-0"), "rank": self.ranks.get(tid), "conf": _j(t.conference) if t is not None else None, "is_fcs": is_fcs}

    def venue(self, g) -> dict:
        v = self.venues.loc[g.venue_id] if not self.venues.empty and pd.notna(g.venue_id) and g.venue_id in self.venues.index else None
        return {"name": _j(g.get("venue_name")) or (_j(v.name) if v is not None else None), "city": _j(v.city) if v is not None else None,
                "roof": _j(g.get("venue_roof")) or (_j(v.roof) if v is not None else None), "tz": _j(v.timezone) if v is not None else None}

    def model_block(self, gid: str) -> dict | None:
        if self.preds.empty or gid not in self.preds.index:
            return None
        p = self.preds.loc[gid]
        return {"proj_away": float(p.proj_away_pts), "proj_home": float(p.proj_home_pts), "proj_margin_home": float(p.proj_margin_home), "proj_total": float(p.proj_total),
                "win_prob_home": float(p.win_prob_home), "spread_diff": _j(p.spread_diff), "total_diff": _j(p.total_diff), "market_spread_home": _j(p.market_spread_home),
                "model_version": p.model_version, "predicted_at": p.predicted_at, "data_quality": float(p.data_quality), "quality_flags": _j(p.quality_flags) or "",
                "why": json.loads(p.contributions) if isinstance(p.contributions, str) else []}

    def ai_block(self, gid: str) -> dict | None:
        if self.ai_idx.empty:
            return None
        rows = self.ai_idx[self.ai_idx.game_id == gid].sort_values("generated_at")
        if rows.empty:
            return None
        r = rows.iloc[-1]
        if bool(r.validation_failed):
            return {"withheld": True, "reason": "Analysis withheld: the latest generation cited numbers not in the data package and is awaiting review.", "generated_at": r.generated_at}
        p = config.TABLES / "model" / "ai_analyses" / self.league / str(self.season) / f"{r.analysis_id}.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        return {"withheld": False, "sections": d["sections"], "model": d.get("llm_model"), "generated_at": d["generated_at"], "inputs_hash": d["inputs_hash"]}


def slate_entry(S: Season, g, mkt_row, edges: pd.DataFrame) -> dict:
    gid = g.game_id
    v = S.venue(g)
    tz = v.get("tz") or ("America/New_York" if S.league == "NFL" else "America/Chicago")
    kick = pd.Timestamp(g.kickoff_utc) if pd.notna(g.kickoff_utc) else None
    m = mkt_row
    market = None if m is None else {"spread_home": _j(m.current_spread_home), "spread_open_home": _j(m.open_spread_home), "total": _j(m.current_total), "total_open": _j(m.open_total), "book": _j(m.primary_book)}
    if market is None and not S.snap_idx.empty and gid in S.snap_idx.index:
        s = S.snap_idx.loc[gid]; market = {"spread_home": _j(s.closing_spread_home), "spread_open_home": None, "total": _j(s.closing_total), "total_open": None, "book": _j(s.closing_book)}
    key = None
    e = edges[(edges.game_id == gid) & (~edges.is_unavailable) & (edges.category != "HOME_FIELD") & edges.margin_contribution.notna()] if not edges.empty else pd.DataFrame()
    if not e.empty:
        top = e.loc[e.margin_contribution.abs().idxmax()]
        key = {"category": top.category, "label": CAT_LABEL.get(top.category, top.category), "side": "home" if top.margin_contribution > 0 else "away", "points": float(top.margin_contribution)}
    result = None
    if not S.res.empty and gid in S.res.index:
        r = S.res.loc[gid]; ev = S.eval.loc[gid] if not S.eval.empty and gid in S.eval.index else None
        result = {"away": int(r.away_score), "home": int(r.home_score), "model_ats": _j(ev.model_ats_result) if ev is not None else None}
    fav = None if market is None or market["spread_home"] is None else ("home" if market["spread_home"] < 0 else "away" if market["spread_home"] > 0 else None)
    away, home = S.team(g.away_team_id), S.team(g.home_team_id)
    return {"game_id": gid, "status": g.status, "kickoff_utc": kick.isoformat() if kick is not None else None, "kickoff_is_tba": bool(g.kickoff_is_tba), "tv": _j(g.tv_network),
            "venue": v, "neutral_site": bool(g.neutral_site), "away": away, "home": home, "market": market, "model": S.model_block(gid), "result": result, "key_edge": key,
            "filters": {"date": _local_date(kick, tz), "conf_home": home["conf"], "conf_away": away["conf"], "ranked": bool(home["rank"] or away["rank"]), "favorite": fav}}



# Quick-look scorecard (§57): a fixed set of the stats worth seeing first, with each team's value and
# national rank side by side and an edge tally. Rows are grouped so related pairs read together.
QUICK_ROWS = [
    ("SCORING",  "Points/Gm",        "points_per_game"),
    ("SCORING",  "Points All/Gm",    "points_allowed_per_game"),
    ("EFF",      "YPP (Offense)",    "yards_per_play_off"),
    ("EFF",      "YPP (Defense)",    "yards_per_play_def"),
    ("RUSH",     "Rush Yds/Gm",      "rush_yds_per_game"),
    ("RUSH",     "Opp RY/Gm",        "opp_rush_yds_per_game"),
    ("RUSH",     "Yds/Rush",         "yards_per_rush"),
    ("RUSH",     "Opp Yds/Rush",     "opp_yards_per_rush"),
    ("PASS",     "Pass Yds/Gm",      "pass_yds_per_game"),
    ("PASS",     "Opp PY/Gm",        "opp_pass_yds_per_game"),
    ("PASS",     "Yds/Pass",         "yards_per_pass"),
    ("PASS",     "Opp Yds/Pass",     "opp_yards_per_pass"),
    ("QB",       "QBR",              "__qbr"),
    ("TRENCH",   "Sack Allowed%",    "sack_rate_allowed"),
    ("TRENCH",   "Sack%",            "sack_rate"),
    ("SIT",      "3D% (Offense)",    "third_down_pct_off"),
    ("SIT",      "3D% (Defense)",    "third_down_pct_def"),
    ("RZ",       "RZ% (Offense)",    "off_rz_td_rate"),
    ("RZ",       "RZ% (Defense)",    "def_rz_td_rate_allowed"),
    ("OTHER",    "TO Margin",        "turnover_margin"),
    ("OTHER",    "SOS",              "__sos"),
]
QUICK_EDGE_PCT_GAP = 0.12       # percentile gap at which one side is credited with the edge


def _passer_rating(cmp_, att, yds, td, intc) -> float | None:
    """NCAA passing efficiency, the number ncaa.com ranks QBs by. Published formula, so it is computed
    here from our own per-player box rather than taken from anyone's page."""
    if not att:
        return None
    return round((8.4 * float(yds) + 330.0 * float(td) + 100.0 * float(cmp_) - 200.0 * float(intc)) / float(att), 1)


def _team_qb_metric(S: "Season", team_id: str, week: int) -> tuple[float | None, str | None]:
    """
    The starting quarterback's season efficiency, with the metric named so two teams are never compared
    on different scales. ESPN QBR when both sides have it (NFL), otherwise NCAA passing efficiency,
    which we can always compute.
    """
    path = config.TABLES / "stats" / "player_game_stats" / S.league / f"{S.season}.parquet"
    pgs = storage.read_table(path)
    if pgs.empty:
        return None, None
    ids = set(S.games[S.games.week < week].game_id)
    sub = pgs[(pgs.team_id == team_id) & pgs.game_id.isin(ids) & pgs.pass_att.notna() & (pgs.pass_att > 0)]
    if sub.empty:
        return None, None
    starters = sub.sort_values("pass_att").groupby("game_id").tail(1)
    att = float(starters.pass_att.sum())
    if not att:
        return None, None
    if "qbr" in starters.columns and starters.qbr.notna().any():
        q = starters[starters.qbr.notna()]
        w = float(q.pass_att.sum())
        if w:
            return round(float((q.qbr * q.pass_att).sum() / w), 1), "QBR"
    need = ("pass_cmp", "pass_yds", "pass_td", "pass_int")
    if all(c in starters.columns for c in need) and starters[list(need)].notna().all().all():
        return _passer_rating(starters.pass_cmp.sum(), att, starters.pass_yds.sum(),
                              starters.pass_td.sum(), starters.pass_int.sum()), "PASSER_RTG"
    return None, None


def _team_qb_metric_passer(S: "Season", team_id: str, week: int) -> float | None:
    """Force the passing-efficiency form, used when the two teams do not both have QBR."""
    path = config.TABLES / "stats" / "player_game_stats" / S.league / f"{S.season}.parquet"
    pgs = storage.read_table(path)
    if pgs.empty:
        return None
    ids = set(S.games[S.games.week < week].game_id)
    sub = pgs[(pgs.team_id == team_id) & pgs.game_id.isin(ids) & pgs.pass_att.notna() & (pgs.pass_att > 0)]
    if sub.empty:
        return None
    st = sub.sort_values("pass_att").groupby("game_id").tail(1)
    need = ("pass_cmp", "pass_yds", "pass_td", "pass_int")
    if not all(c in st.columns for c in need) or st[list(need)].isna().any().any():
        return None
    return _passer_rating(st.pass_cmp.sum(), st.pass_att.sum(), st.pass_yds.sum(), st.pass_td.sum(), st.pass_int.sum())


def _fpi_sos(S: "Season", team_id: str) -> int | None:
    """ESPN FPI strength-of-schedule rank, taken from ESPN's own endpoint. CFB only."""
    f = storage.read_table(config.TABLES / "context" / "espn_fpi" / f"{S.season}.parquet")
    if f.empty or "sos_rank_espn" not in f.columns:
        return None
    r = f[f.team_id == team_id]
    if r.empty or pd.isna(r.sos_rank_espn.iloc[0]):
        return None
    return int(r.sos_rank_espn.iloc[0])


def build_quick_look(S: "Season", week: int, gid: str, home: str, away: str, metrics_rows: list, adj: str = "OPP_ADJ") -> dict:
    by_key = {r["metric_key"]: r for r in metrics_rows}
    reg = S.reg.set_index("metric_key") if not S.reg.empty else pd.DataFrame()
    rat = storage.read_table(AN / "team_ratings" / S.league / f"{S.season}.parquet")
    rat = rat[rat.as_of_week == week].set_index("team_id") if not rat.empty and (rat.as_of_week == week).any() else pd.DataFrame()
    qb = {t: _team_qb_metric(S, t, week) for t in (home, away)}
    qb_kind = next((k for _, k in qb.values() if k), None)
    if len({k for _, k in qb.values() if k}) > 1:
        qb_kind = "PASSER_RTG"          # never compare two teams on different scales
        qb = {t: (_team_qb_metric_passer(S, t, week), "PASSER_RTG") for t in (home, away)}
    rows, tally = [], {"home": 0, "away": 0}
    for group, label, key in QUICK_ROWS:
        hib, unit = True, None
        if key == "__qbr":
            a = {"v": qb[away][0], "rank": None, "pct": None}
            h = {"v": qb[home][0], "rank": None, "pct": None}
            unit = "qbr"
            label = "QBR" if qb_kind == "QBR" else "Pass Efficiency"
        elif key == "__sos":
            def sos(t):
                fpi = _fpi_sos(S, t) if S.league == "CFB" else None       # ESPN FPI rank when we have it
                if fpi is not None:
                    return {"v": fpi, "rank": None, "pct": None}
                if rat.empty or t not in rat.index:
                    return {"v": None, "rank": None, "pct": None}
                return {"v": int(rat.loc[t].sos_rank), "rank": None, "pct": None}
            a, h = sos(away), sos(home)
            hib = False            # a lower SOS rank means a tougher schedule faced
            unit = "rank"
        else:
            row = by_key.get(key)
            if not row:
                continue
            k = f"SEASON:{adj}"
            a = row["away"].get(k) or row["away"].get("SEASON:RAW") or {}
            h = row["home"].get(k) or row["home"].get("SEASON:RAW") or {}
            if not reg.empty and key in reg.index:
                hib = bool(reg.loc[key].higher_is_better); unit = reg.loc[key].unit
        av, hv = a.get("v") if a else None, h.get("v") if h else None
        edge = None
        ap, hp = (a or {}).get("pct"), (h or {}).get("pct")
        if ap is not None and hp is not None and abs(hp - ap) >= QUICK_EDGE_PCT_GAP:
            edge = "home" if hp > ap else "away"
        elif ap is None and hp is None and av is not None and hv is not None:
            better_home = (hv > av) if hib else (hv < av)
            edge = "home" if better_home else "away"
        if edge:
            tally[edge] += 1
        rows.append({"group": group, "label": label, "metric_key": key, "unit": unit, "higher_is_better": hib,
                     "away": {"v": av, "rank": (a or {}).get("rank")}, "home": {"v": hv, "rank": (h or {}).get("rank")},
                     "edge": edge})
    return {"rows": rows, "edge_count": tally,
            "winner": ("home" if tally["home"] > tally["away"] else "away" if tally["away"] > tally["home"] else None),
            "adjustment": adj, "edge_rule": f"An edge is credited when the two teams differ by at least "
                                            f"{int(QUICK_EDGE_PCT_GAP*100)} percentile points on that metric."}


def build_odds(S: Season, week: int, slate: dict) -> dict:
    """Odds tab payload: one entry per game with the splits history for both periods, plus the line history."""
    sp = splits_engine.build_week(S.league, S.season, week, S.games[(S.games.week == week)], S.teams)
    hist_all = splits_engine.load(S.league, S.season, week)
    games = []
    for entry in slate["games"]:
        gid = entry["game_id"]
        s_ = sp.get(gid) or {"periods": {}, "any_available": False}
        mkt_path = OUT / "market" / f"{gid}.json"
        mk = json.loads(mkt_path.read_text()) if mkt_path.exists() else None
        line_series = (mk or {}).get("series", [])
        gh = hist_all[hist_all.game_id == gid] if not hist_all.empty else hist_all
        ha, aa = entry["home"]["abbr"], entry["away"]["abbr"]
        kick = pd.Timestamp(entry["kickoff_utc"]) if entry["kickoff_utc"] else None
        state = splits_engine.current_state(gh, "FULL", ha, aa, kick) if not gh.empty else {"events": [], "rlm_active": {}, "rlm_ever": {}, "lopsided": {}, "recent_move": {}}
        games.append({"game_id": gid, "status": entry["status"], "kickoff_utc": entry["kickoff_utc"], "kickoff_is_tba": entry["kickoff_is_tba"],
                      "away": entry["away"], "home": entry["home"], "market": entry["market"], "model": entry["model"], "result": entry["result"],
                      "filters": entry["filters"], "splits": s_.get("periods", {}), "splits_available": s_.get("any_available", False),
                      "line_series": line_series, "market_state": state, "events": state.get("events", [])})
    covered = sum(1 for g in games if g["splits_available"])
    return {"league": S.league, "season": S.season, "week": week, "generated_at": datetime.now(timezone.utc).isoformat(),
            "games": games, "coverage": {"with_splits": covered, "total": len(games)},
            "source_note": (f"{config.VSIN['attribution']}. Percentages are the share of tickets and of money on the home side "
                            "(over side for totals), captured on a schedule and stored with a timestamp."
                            if config.VSIN.get("enabled") else
                            ("Ticket and money percentages are entered by hand and stamped 'manual'." if not config.SPLITS_FEED.get("enabled")
                             else f"Splits from the licensed {config.SPLITS_FEED.get('provider')} feed."))}


def build_picks(S: Season, week: int) -> dict:
    """Picks payload: tiered plays plus the measured hit rate for each tier and the season's graded record."""
    path = config.TABLES / "model" / "picks" / S.league / str(S.season) / f"W{week:02d}.parquet"
    picks = storage.read_table(path)
    cal = picks_engine.calibrate(S.league)
    ev = storage.read_table(config.TABLES / "model" / "picks_evaluation" / S.league / f"{S.season}.csv")
    rec = None
    if not ev.empty:
        dec = ev[ev.result.isin(["WIN", "LOSS"])]
        rec = {"n": int(len(dec)), "wins": int((dec.result == "WIN").sum()), "losses": int((dec.result == "LOSS").sum()),
               "pushes": int((ev.result == "PUSH").sum()),
               "hit_rate": round(float((dec.result == "WIN").mean()), 4) if len(dec) else None,
               "profit_units": round(float(ev.profit_units.sum()), 3)}
    cols = ("game_id", "market", "side", "line", "price", "tier", "score", "edge_points", "model_number", "market_number",
            "data_quality", "signals", "signal_notes", "tickets_pct_side", "money_pct_side", "expected_value",
            "model_version", "kickoff_utc", "home", "away", "week", "marquee_why", "rlm", "lopsided_side", "move_against",
                                                  "band_hit_rate", "band_n", "band_ci_low", "band_ci_high", "score_edge_only")
    out = [{c: _j(k.get(c)) for c in cols} for _, k in picks.iterrows()] if not picks.empty else []
    rej_path = config.TABLES / "model" / "picks_rejected" / S.league / str(S.season) / f"W{week:02d}.parquet"
    rejected = storage.read_table(rej_path)
    rej = []
    if not rejected.empty:
        rejected = rejected.sort_values("score", ascending=False).head(80)
        rej = [{**{c: _j(k.get(c)) for c in cols}, "veto_reasons": _j(k.get("veto_reasons"))} for _, k in rejected.iterrows()]
    return {"league": S.league, "season": S.season, "week": week, "generated_at": datetime.now(timezone.utc).isoformat(),
            "picks": out, "rejected": rej, "gates": config.PICK_GATES, "calibration": cal, "season_record": rec}


def _latest_splits(hist: pd.DataFrame, gid: str, kickoff) -> dict | None:
    """Most recent pre-kickoff ticket/money shares for the board, one number per market per metric."""
    if hist.empty:
        return None
    g = hist[(hist.game_id == gid) & (hist.period == "FULL")]
    if kickoff is not None and not g.empty:
        g = g[g.retrieved_at < pd.Timestamp(kickoff)]
    if g.empty:
        return None
    r = g.sort_values("retrieved_at").iloc[-1]
    out = {"book": r.book, "retrieved_at": r.retrieved_at.isoformat(), "n": int(len(g))}
    for m in ("spread", "total", "moneyline"):
        t, mo = r.get(f"{m}_ticket_pct_home"), r.get(f"{m}_money_pct_home")
        out[m] = {"ticket_pct_home": None if pd.isna(t) else round(float(t), 3),
                  "money_pct_home": None if pd.isna(mo) else round(float(mo), 3)}
    return out


def build_slate(S: Season, week: int) -> dict:
    wk = S.games[(S.games.week == week) & (S.games.season_type == "REG")].copy()
    wk = wk[~(wk.home_team_id.str.startswith("CFB_FCS") & wk.away_team_id.str.startswith("CFB_FCS"))]
    mkt = storage.read_table(AN / "market_analysis" / S.league / str(S.season) / f"W{week:02d}.parquet")
    mkt = mkt.set_index("game_id") if not mkt.empty else mkt
    edges = storage.read_table(AN / "matchup_edges" / S.league / str(S.season) / f"W{week:02d}.parquet")
    hist = splits_engine.load(S.league, S.season, week)
    games = []
    for _, g in wk.sort_values("kickoff_utc", na_position="last").iterrows():
        entry = slate_entry(S, g, (mkt.loc[g.game_id] if not mkt.empty and g.game_id in mkt.index else None), edges)
        entry["splits"] = _latest_splits(hist, g.game_id, g.kickoff_utc)
        gh = hist[hist.game_id == g.game_id] if not hist.empty else hist
        if not gh.empty:
            st = splits_engine.current_state(gh, "FULL", entry["home"]["abbr"], entry["away"]["abbr"],
                                             pd.Timestamp(g.kickoff_utc) if pd.notna(g.kickoff_utc) else None)
            entry["indicators"] = {"rlm": {k: v["toward"] for k, v in (st.get("rlm_active") or {}).items()},
                                   "lopsided": {k: v for k, v in (st.get("lopsided") or {}).items() if v},
                                   "steam": sorted({e["market"] for e in st.get("events", []) if e["kind"] == "steam"})}
        else:
            entry["indicators"] = None
        games.append(entry)
    return {"league": S.league, "season": S.season, "week": week, "generated_at": datetime.now(timezone.utc).isoformat(), "games": games}


def comparison_rows(S: Season, week: int, gid: str, home: str, away: str, snap_metrics: list | None) -> tuple[list[dict], list[str]]:
    m = pd.DataFrame(snap_metrics) if snap_metrics else storage.read_table(AN / "team_metrics_asof" / S.league / str(S.season) / f"W{week:02d}.parquet")
    if m.empty:
        return [], []
    m = m[m.as_of_game_id == gid]
    packs = {(r.team_id, r.window, r.adjustment): (json.loads(r.metrics) if isinstance(r.metrics, str) else r.metrics) for _, r in m.iterrows()}
    flags = sorted({f for r in m[m.window == "SEASON"].itertuples() for f in str(r.quality_flags).split(",") if f and f != "nan"})
    rows = []
    for _, d in S.reg.iterrows():
        if S.league not in str(d.leagues).split(","):
            continue
        k = d.metric_key
        row = {"metric_key": k, "label": d.label, "group": d.group, "description": d.description, "unit": d.unit, "higher_is_better": bool(d.higher_is_better), "away": {}, "home": {}}
        any_val = False
        for w in WINDOWS:
            for adj in ("RAW", "OPP_ADJ"):
                a = (packs.get((away, w, adj)) or {}).get(k); h = (packs.get((home, w, adj)) or {}).get(k)
                row["away"][f"{w}:{adj}"] = a; row["home"][f"{w}:{adj}"] = h
                any_val = any_val or bool(a or h)
        if any_val:
            rows.append(row)
    return rows, flags


def _market_state_for(S: Season, week: int, gid: str, entry: dict) -> dict:
    hist = splits_engine.load(S.league, S.season, week)
    gh = hist[hist.game_id == gid] if not hist.empty else hist
    if gh.empty:
        return {"events": [], "rlm_active": {}, "rlm_ever": {}, "lopsided": {}, "recent_move": {}}
    kick = pd.Timestamp(entry["kickoff_utc"]) if entry.get("kickoff_utc") else None
    return splits_engine.current_state(gh, "FULL", entry["home"]["abbr"], entry["away"]["abbr"], kick)


def build_matchup(S: Season, week: int, entry: dict) -> dict:
    gid = entry["game_id"]
    g = S.games.set_index("game_id").loc[gid]
    home, away = g.home_team_id, g.away_team_id
    snap = None
    if g.status in ("LOCKED", "FINAL"):
        sp = config.SNAPSHOTS / f"pregame_{gid}.json"
        snap = json.loads(sp.read_text()) if sp.exists() else None
    edges = pd.DataFrame(snap["matchup_edges"]) if snap and snap.get("matchup_edges") else storage.read_table(AN / "matchup_edges" / S.league / str(S.season) / f"W{week:02d}.parquet")
    edges = edges[edges.game_id == gid] if not edges.empty else edges
    edge_list = [{"category": e.category, "label": CAT_LABEL.get(e.category, e.category), "points_home": _j(e.margin_contribution), "score": _j(e.edge_score), "unavailable": bool(e.is_unavailable),
                  "inputs": (json.loads(e.inputs) if isinstance(e.inputs, str) else e.inputs)} for _, e in edges.iterrows()]
    if snap and snap.get("prediction"):
        p = snap["prediction"]
        model = {"proj_away": p["proj_away_pts"], "proj_home": p["proj_home_pts"], "proj_margin_home": p["proj_margin_home"], "proj_total": p["proj_total"], "win_prob_home": p["win_prob_home"],
                 "spread_diff": p.get("spread_diff"), "market_spread_home": p.get("market_spread_home"), "model_version": p["model_version"], "predicted_at": p["predicted_at"],
                 "data_quality": p["data_quality"], "quality_flags": p.get("quality_flags") or "", "why": json.loads(p["contributions"]) if isinstance(p.get("contributions"), str) else (p.get("contributions") or [])}
    else:
        model = S.model_block(gid)
    qb = pd.DataFrame(snap["qb_status"]) if snap and snap.get("qb_status") else None
    if qb is None:
        qbp = ROSTER / "qb_status" / S.league / str(S.season) / f"W{week:02d}.parquet"
        qb = pd.read_parquet(qbp) if qbp.exists() else pd.DataFrame()
    qb = qb.set_index("team_id") if not qb.empty else qb
    inj = pd.DataFrame(snap["injuries"]) if snap and snap.get("injuries") is not None else storage.read_table(ROSTER / "injuries" / S.league / f"{S.season}.csv")
    inj = inj[(inj.week == week) & inj.team_id.isin([home, away]) & inj.status.isin(["OUT", "DOUBTFUL", "QUESTIONABLE", "IR"])] if not inj.empty else inj
    metrics_rows, qflags = comparison_rows(S, week, gid, home, away, snap.get("team_metrics_asof") if snap else None)
    games_n = {}
    src_m = pd.DataFrame(snap["team_metrics_asof"]) if snap and snap.get("team_metrics_asof") else storage.read_table(AN / "team_metrics_asof" / S.league / str(S.season) / f"W{week:02d}.parquet")
    if not src_m.empty:
        for t in (home, away):
            sub = src_m[(src_m.team_id == t) & (src_m.as_of_game_id == gid) & (src_m.window == "SEASON")]
            games_n[t] = int(sub.games_n.max()) if not sub.empty else 0

    def team_block(tid, ident):
        q = qb.loc[tid] if not qb.empty and tid in qb.index else None
        qd = None if q is None else {"name": _j(q.player_name), "basis": q.projection_basis, "confidence": float(q.confidence), "flags": str(q["flags"]).split(","), "career_games": int(q.career_games_10att),
                                     "career_att": float(q.career_att), "career_cmp_pct": _j(q.career_cmp_pct), "career_ypa": _j(q.career_ypa), "career_td": _j(q.career_td), "career_int": _j(q.career_int),
                                     "career_epa_dropback": _j(q.career_ppa_dropback), "season_att": float(q.season_att)}
        ti = inj[inj.team_id == tid] if not inj.empty else inj
        return {"identity": ident, "games_n": games_n.get(tid, 0), "qb": qd,
                "injuries": [{"player": _j(x.get("player_name")) or _j(x.get("player_id")), "position": _j(x.position), "status": x.status, "desc": _j(x.get("injury_desc")), "source": x.source} for _, x in ti.iterrows()],
                "roster": {"continuity": _j(S.cont.loc[tid].continuity_index) if not S.cont.empty and tid in S.cont.index else None,
                           "rp_total": _j(S.rp.loc[tid].rp_total) if not S.rp.empty and tid in S.rp.index else None}}
    # market: from the engine payload (live) or re-analyzed from the frozen history (locked)
    mkt_path = OUT / "market" / f"{gid}.json"
    market = json.loads(mkt_path.read_text()) if mkt_path.exists() else None
    if snap and snap.get("market_history"):
        hist = pd.DataFrame(snap["market_history"]); hist["retrieved_at"] = pd.to_datetime(hist.retrieved_at, utc=True)
        market = market_engine.analyze_game(S.league, hist, None, pd.Timestamp(g.kickoff_utc), pd.Timestamp(snap["locked_at"]))
        (OUT / "market").mkdir(parents=True, exist_ok=True); (OUT / "market" / f"{gid}.json").write_text(json.dumps(market, default=str))
    wx = None
    if snap and snap.get("weather"):
        wx = snap["weather"][0]
    else:
        wpath = config.TABLES / "context" / "weather_snapshots" / S.league / str(S.season) / f"W{week:02d}.csv"
        if wpath.exists():
            w = pd.read_csv(wpath); w = w[w.game_id == gid].sort_values("retrieved_at")
            wx = w.iloc[-1].to_dict() if not w.empty else None
    if wx:
        wx = {k: _j(v) for k, v in wx.items() if k in ("is_indoor", "temp_f", "wind_mph", "wind_gust_mph", "precip_prob", "retrieved_at", "source", "hours_to_kickoff")}
    result = entry.get("result")
    if result and not S.eval.empty and gid in S.eval.index:
        e = S.eval.loc[gid]; result = {**result, "evaluation": {"margin_error": _j(e.margin_error), "winner_correct": _j(e.winner_correct), "model_ats_result": _j(e.model_ats_result), "model_ou_result": _j(e.model_ou_result)}}
    return {"game": entry, "status": g.status, "frozen_from_snapshot": bool(snap), "locked_at": snap["locked_at"] if snap else None,
            "teams": {"away": team_block(away, entry["away"]), "home": team_block(home, entry["home"])},
            "metrics": {"windows": WINDOWS, "default_window": "SEASON", "rows": metrics_rows, "quality_flags": qflags},
            "quick_look": {a: build_quick_look(S, week, gid, home, away, metrics_rows, a) for a in ("OPP_ADJ", "RAW")},
            "splits": splits_engine.build_week(S.league, S.season, week, S.games[S.games.game_id == gid], S.teams).get(gid, {}).get("periods", {}),
            "market_state": _market_state_for(S, week, gid, entry),
            "edges": edge_list, "model": model, "market": market, "market_history_url": f"json/market/{gid}.json", "weather": wx, "result": result, "ai": S.ai_block(gid),
            "sources": {"metrics": "CollegeFootballData (PPA, advanced stats, plays) and nflverse (nflfastR EPA, FTN charting, PFR pressures)" if S.league == "CFB" else "nflverse (nflfastR play-by-play EPA, FTN charting, PFR pressures)",
                        "lines": "CollegeFootballData lines" if S.league == "CFB" else "The Odds API (US books)", "weather": "Open-Meteo", "injuries": "official league report" if S.league == "NFL" else "manual entries",
                        "note": "Opponent-adjusted values are this platform's own ridge fits; early-season values blend the previous season's adjusted numbers."},
            "generated_at": datetime.now(timezone.utc).isoformat()}


def build_status(leagues: list[str], season: int) -> dict:
    jl = storage.read_table(config.TABLES / "ops" / "job_log.csv")
    vl = storage.read_table(config.TABLES / "ops" / "validation_log.csv")
    bud = storage.read_table(config.TABLES / "ops" / "api_budget.csv")
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    fresh = {}
    for label, pattern in (("Team game stats (NFL)", f"stats/team_game_advanced/NFL/{season}.parquet"), ("Team game stats (CFB)", f"stats/team_game_advanced/CFB/{season}.parquet"),
                           ("Odds snapshots (NFL)", f"market/snapshots/NFL/{season}/*.csv"), ("Odds snapshots (CFB)", f"market/snapshots/CFB/{season}/*.csv"),
                           ("Rosters", f"roster/roster_snapshots/*/{season}/*.parquet"), ("Injuries (NFL)", f"roster/injuries/NFL/{season}.csv"), ("Weather", f"context/weather_snapshots/*/{season}/*.csv"),
                           ("Team metrics", f"analytics/team_metrics_asof/*/{season}/*.parquet"), ("Matchup edges", f"analytics/matchup_edges/*/{season}/*.parquet"),
                           ("Predictions", f"model/predictions/*/{season}.csv"), ("AI analyses", "model/ai_analyses_index.csv"),
                           ("Betting splits", f"market/splits/*/{season}/*.csv")):
        files = glob.glob(str(config.TABLES / pattern))
        fresh[label] = max((datetime.fromtimestamp(__import__("os").path.getmtime(f), tz=timezone.utc) for f in files), default=None)
        fresh[label] = fresh[label].isoformat() if fresh[label] else None
    jobs = []
    if not jl.empty:
        for name, g in jl.sort_values("started_at").groupby("job_name"):
            last = g.iloc[-1]; ok = g[g.status == "SUCCESS"]
            jobs.append({"job": name, "last_run": last.started_at, "last_status": last.status, "last_success": ok.iloc[-1].finished_at if len(ok) else None, "last_message": _j(last.message)})
    budget = []
    if not bud.empty:
        b = bud[bud.day.astype(str).str.startswith(month)]
        for prov, g in b.groupby("provider"):
            budget.append({"provider": prov, "calls": int(g.requests.sum()), "credits": int(g.credits.fillna(0).sum()), "monthly_limit": config.API_BUDGET.get(prov, {}).get("monthly"),
                           "failures": int(g.failures.sum()), "remaining_reported": _j(g.sort_values("day").remaining_reported.dropna().iloc[-1]) if g.remaining_reported.notna().any() else None})
    models = []
    mvp = config.TABLES / "model" / "model_versions.json"
    if mvp.exists():
        for k, v in json.loads(mvp.read_text()).items():
            o = (v.get("backtest_summary") or {}).get("overall", {})
            models.append({"model_version": k, "active": v.get("is_active"), "trained_on": v.get("trained_on_seasons", []), "oos_mae": o.get("mae_margin"), "oos_winner_acc": o.get("winner_acc"), "market_mae": o.get("market_mae_margin")})
    live = []
    for lg in leagues:
        ev = storage.read_table(config.TABLES / "model" / "model_evaluation" / lg / f"{season}.csv")
        if not ev.empty:
            ats = ev[ev.model_ats_result.isin(["WIN", "LOSS"])]
            live.append({"league": lg, "n": int(len(ev)), "mae": round(float(ev.abs_margin_error.mean()), 2), "winner_acc": round(float(ev.winner_correct.astype(float).mean()), 3),
                         "ats": round(float((ats.model_ats_result == "WIN").mean()), 3) if len(ats) else None, "ats_n": int(len(ats))})
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "freshness": fresh, "jobs": jobs, "api_budget": budget, "models": models, "live_evaluation": live,
            "validation_recent": vl.tail(40).to_dict("records") if not vl.empty else [],
            "unmatched_aliases": sorted(vl[vl.rule == "ALIAS_UNMATCHED"].observed.dropna().astype(str).unique().tolist()) if not vl.empty else [],
            "workflows": {"Refresh team stats": "stats.yml", "Refresh odds": "odds.yml", "Refresh rosters": "roster.yml", "Refresh context (injuries, weather)": "context.yml",
                          "Rebuild metrics": "metrics.yml", "Rebuild matchups": "matchups.yml", "Predict and lock": "predict.yml", "Regenerate AI analysis": "ai.yml", "Rebuild site": "site.yml"}}


def run(leagues: list[str], season: int, weeks: list[int] | None, job: JobRun) -> None:
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "season": season, "leagues": [], "current_week": {}, "slates": {}, "weeks": {},
                "version": datetime.now(timezone.utc).strftime("%Y%m%d%H%M")}
    for league in leagues:
        S = Season(league, season)
        if S.games.empty:
            continue
        sched = S.games[S.games.status == "SCHEDULED"]
        cur = int(sched.week.min()) if not sched.empty else int(S.games.week.max())
        wks = weeks or [w for w in (cur - 1, cur, cur + 1) if w >= 1 and w in set(S.games.week)]
        manifest["leagues"].append(league); manifest["current_week"][league] = cur; manifest["weeks"][league] = wks; manifest["slates"][league] = {}
        for wk in wks:
            slate = build_slate(S, wk)
            (OUT / "slate" / league / str(season)).mkdir(parents=True, exist_ok=True)
            rel = f"json/slate/{league}/{season}/W{wk:02d}.json"
            (config.SITE_DIR / rel).write_text(json.dumps(slate, default=str))
            manifest["slates"][league][str(wk)] = rel
            (OUT / "picks" / league / str(season)).mkdir(parents=True, exist_ok=True)
            (OUT / "picks" / league / str(season) / f"W{wk:02d}.json").write_text(json.dumps(build_picks(S, wk), default=str))
            manifest.setdefault("picks", {}).setdefault(league, {})[str(wk)] = f"json/picks/{league}/{season}/W{wk:02d}.json"
            (OUT / "odds" / league / str(season)).mkdir(parents=True, exist_ok=True)
            (OUT / "odds" / league / str(season) / f"W{wk:02d}.json").write_text(json.dumps(build_odds(S, wk, slate), default=str))
            manifest.setdefault("odds", {}).setdefault(league, {})[str(wk)] = f"json/odds/{league}/{season}/W{wk:02d}.json"
            (OUT / "matchup").mkdir(parents=True, exist_ok=True)
            n = 0
            for entry in slate["games"]:
                try:
                    (OUT / "matchup" / f"{entry['game_id']}.json").write_text(json.dumps(build_matchup(S, wk, entry), default=str)); n += 1
                except Exception as e:  # one broken game never breaks the board
                    print(f"  matchup page failed for {entry['game_id']}: {e}")
            job.rows_written += n
            print(f"{league} {season} W{wk}: slate {len(slate['games'])} games, {n} matchup pages")
    (OUT / "status.json").write_text(json.dumps(build_status(leagues, season), default=str))
    (OUT / "manifest.json").write_text(json.dumps(manifest))
    print(f"manifest: {manifest['current_week']} version {manifest['version']}")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="BOTH", choices=["NFL", "CFB", "BOTH"])
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--weeks", nargs="*", type=int)
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    with JobRun("SITE", a.league, a.trigger) as job:
        run(["NFL", "CFB"] if a.league == "BOTH" else [a.league], a.season, a.weeks, job)


if __name__ == "__main__":
    main()
