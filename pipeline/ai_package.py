"""
Structured matchup package for the AI layer (master prompt §36). The AI receives THIS object and nothing else.
Every number it may cite is in here; the validator (ai_agent.validate) checks its output against the flat
value set of this package. Fields that are unavailable are present with null and listed in `unavailable`,
so the model can say "Insufficient reliable data" for exactly the right things.
"""
from __future__ import annotations
import hashlib
import json
import re

import pandas as pd

import config
from pipeline import storage
from pipeline.matchup_engine import CATEGORIES

AN = config.TABLES / "analytics"
ROSTER = config.TABLES / "roster"

# metrics shown to the AI (label from registry). Kept short on purpose: ~30 per team, not 83.
PACKAGE_METRICS = ["points_per_game", "points_allowed_per_game", "off_ppa_play", "def_ppa_play", "off_ppa_pass", "def_ppa_pass", "off_ppa_rush", "def_ppa_rush",
                   "off_success_rate", "def_success_rate", "off_explosive_play_rate", "def_explosive_play_rate_allowed", "yards_per_play_off", "yards_per_play_def",
                   "sack_rate_allowed", "sack_rate", "off_pressure_rate_allowed", "def_pressure_rate", "off_havoc_allowed", "def_havoc", "off_line_yards", "def_line_yards_allowed",
                   "third_down_pct_off", "third_down_pct_def", "off_rz_td_rate", "def_rz_td_rate_allowed", "off_pts_per_scoring_opp", "turnover_margin",
                   "off_pass_rate", "off_neutral_pass_rate", "off_sec_per_play", "off_play_action_rate", "def_blitz_rate", "def_pressure_no_blitz_rate", "plays_per_game"]


def _metrics_for(m: pd.DataFrame, team_id: str, game_id: str, window: str, adj: str, labels: dict) -> tuple[dict, list[str]]:
    sub = m[(m.team_id == team_id) & (m.as_of_game_id == game_id) & (m.window == window) & (m.adjustment == adj)]
    if sub.empty:
        return {}, PACKAGE_METRICS
    d = json.loads(sub.iloc[0].metrics)
    out, unavailable = {}, []
    for k in PACKAGE_METRICS:
        v = d.get(k)
        if v is None:
            unavailable.append(k); continue
        out[k] = {"label": labels.get(k, k), "value": v["v"], "rank": v["rank"], "pct": v["pct"], "games": v["n"], "low_sample": v["low_n"]}
    return out, unavailable


def build_package(league: str, season: int, week: int, game_id: str) -> dict | None:
    games = storage.read_table(storage.games_path(league, season))
    g = games[games.game_id == game_id]
    if g.empty:
        return None
    g = g.iloc[0]
    teams = storage.read_table(config.TABLES / "ref" / "teams.parquet").set_index("team_id")
    reg = storage.read_table(config.TABLES / "ref" / "metric_definitions.csv")
    labels = dict(zip(reg.metric_key, reg.label)) if not reg.empty else {}
    descs = dict(zip(reg.metric_key, reg.description)) if not reg.empty else {}
    m = storage.read_table(AN / "team_metrics_asof" / league / str(season) / f"W{week:02d}.parquet")
    edges = storage.read_table(AN / "matchup_edges" / league / str(season) / f"W{week:02d}.parquet")
    edges = edges[edges.game_id == game_id] if not edges.empty else edges
    preds = storage.read_table(config.TABLES / "model" / "predictions" / league / f"{season}.csv")
    pred = preds[preds.game_id == game_id].sort_values("predicted_at").tail(1) if not preds.empty else pd.DataFrame()
    rat = storage.read_table(AN / "team_ratings" / league / f"{season}.parquet")
    rat = rat[rat.as_of_week == week].set_index("team_id") if not rat.empty else rat
    qbp = ROSTER / "qb_status" / league / str(season) / f"W{week:02d}.parquet"
    qb = pd.read_parquet(qbp).set_index("team_id") if qbp.exists() else pd.DataFrame()
    cont = storage.read_table(ROSTER / "continuity" / league / f"{season}.parquet")
    cont = cont.set_index("team_id") if not cont.empty else cont
    rp = storage.read_table(ROSTER / "returning_production" / league / f"{season}.parquet")
    rp = rp[rp.method == "derived_position_weighted"].set_index("team_id") if not rp.empty else rp
    tal = storage.read_table(ROSTER / "talent_scores.parquet") if league == "CFB" else pd.DataFrame()
    tal = tal[tal.season == season].set_index("team_id") if not tal.empty else tal
    inj = storage.read_table(ROSTER / "injuries" / league / f"{season}.csv")
    mkt_path = config.SITE_JSON / "market" / f"{game_id}.json"
    market = json.loads(mkt_path.read_text()) if mkt_path.exists() else None
    wpath = config.TABLES / "context" / "weather_snapshots" / league / str(season) / f"W{week:02d}.csv"
    wx = pd.read_csv(wpath) if wpath.exists() else pd.DataFrame()

    def team_block(tid: str, side: str) -> dict:
        t = teams.loc[tid] if tid in teams.index else None
        blend, unav = _metrics_for(m, tid, game_id, "BLEND", "OPP_ADJ", labels)
        season_raw, _ = _metrics_for(m, tid, game_id, "SEASON", "RAW", labels)
        last3, _ = _metrics_for(m, tid, game_id, "LAST3", "RAW", labels)
        n_games = int(m[(m.team_id == tid) & (m.as_of_game_id == game_id) & (m.window == "SEASON")].games_n.max()) if not m.empty else 0
        q = qb.loc[tid] if not qb.empty and tid in qb.index else None
        c = cont.loc[tid] if not cont.empty and tid in cont.index else None
        r = rp.loc[tid] if not rp.empty and tid in rp.index else None
        ti = tal.loc[tid] if not tal.empty and tid in tal.index else None
        team_inj = inj[(inj.team_id == tid) & (inj.week == week) & inj.status.isin(["OUT", "DOUBTFUL", "QUESTIONABLE", "IR"])] if not inj.empty else pd.DataFrame()
        return {
            "team_id": tid, "name": t.display_name if t is not None else tid, "abbr": t.abbr if t is not None else tid.split("_")[1],
            "conference": t.conference if t is not None else None, "side": side, "games_played": n_games,
            "rating": None if rat.empty or tid not in rat.index else {"overall": round(float(rat.loc[tid].rating_overall), 1), "offense": round(float(rat.loc[tid].rating_off), 1),
                                                                     "defense": round(float(rat.loc[tid].rating_def), 1), "sos_rank": int(rat.loc[tid].sos_rank)},
            "metrics_blend_opp_adjusted": blend, "metrics_season_raw": season_raw, "metrics_last3_raw": last3, "unavailable_metrics": unav,
            "quarterback": None if q is None else {"name": q.player_name, "basis": q.projection_basis, "confidence": float(q.confidence), "flags": str(q["flags"]).split(","),
                                                   "career_games_10plus_att": int(q.career_games_10att), "career_attempts": float(q.career_att),
                                                   "career_completion_pct": None if pd.isna(q.career_cmp_pct) else round(float(q.career_cmp_pct), 3),
                                                   "career_yards_per_attempt": None if pd.isna(q.career_ypa) else round(float(q.career_ypa), 2),
                                                   "career_td": None if pd.isna(q.career_td) else int(q.career_td), "career_int": None if pd.isna(q.career_int) else int(q.career_int),
                                                   "career_epa_per_dropback": None if pd.isna(q.career_ppa_dropback) else round(float(q.career_ppa_dropback), 3),
                                                   "season_attempts": float(q.season_att)},
            "roster": {"continuity_index": None if c is None else round(float(c.continuity_index), 2), "head_coach_changed": None if c is None or pd.isna(c.hc_changed) else bool(c.hc_changed),
                       "returning_production": None if r is None else {"total": round(float(r.rp_total), 2) if pd.notna(r.rp_total) else None, "passing": round(float(r.rp_passing), 2) if pd.notna(r.rp_passing) else None,
                                                                        "rushing": round(float(r.rp_rushing), 2) if pd.notna(r.rp_rushing) else None, "receiving": round(float(r.rp_receiving), 2) if pd.notna(r.rp_receiving) else None,
                                                                        "defense": round(float(r.rp_defense), 2) if pd.notna(r.rp_defense) else None, "ol_starts": None if pd.isna(r.ol_starts_returning) else int(r.ol_starts_returning),
                                                                        "ol_starts_is_proxy": bool(r.ol_starts_returning_is_proxy)},
                       "talent": None if ti is None else {"percentile": round(float(ti.talent_score), 2) if pd.notna(ti.talent_score) else None, "blue_chip_ratio_4yr": round(float(ti.blue_chip_ratio_4yr), 2) if pd.notna(ti.blue_chip_ratio_4yr) else None}},
            "injuries": [{"player": x.get("player_name") if pd.notna(x.get("player_name")) else x.get("player_id"), "position": x.position, "status": x.status, "source": x.source} for _, x in team_inj.iterrows()][:10],
            "injuries_source_note": "official league report" if league == "NFL" else "manual entries only; no official CFB injury report exists",
        }

    home, away = team_block(g.home_team_id, "home"), team_block(g.away_team_id, "away")
    edge_rows = []
    for _, e in edges.iterrows():
        edge_rows.append({"category": e.category, "edge_score": int(e.edge_score) if pd.notna(e.edge_score) else None, "favors": None if e.is_unavailable else ("home" if e.edge_raw > 0 else "away" if e.edge_raw < 0 else "neutral"),
                          "points_toward_home": None if pd.isna(e.margin_contribution) else round(float(e.margin_contribution), 2), "unavailable": bool(e.is_unavailable),
                          "inputs": json.loads(e.inputs) if isinstance(e.inputs, str) else {}})
    p = pred.iloc[0] if not pred.empty else None
    weather = None
    if not wx.empty:
        w = wx[wx.game_id == game_id].sort_values("retrieved_at")
        if not w.empty:
            r = w.iloc[-1]
            weather = {"indoor": bool(r.is_indoor), "temp_f": None if pd.isna(r.temp_f) else float(r.temp_f), "wind_mph": None if pd.isna(r.wind_mph) else float(r.wind_mph),
                       "wind_gust_mph": None if pd.isna(r.wind_gust_mph) else float(r.wind_gust_mph), "precip_prob": None if pd.isna(r.precip_prob) else float(r.precip_prob),
                       "hours_to_kickoff_at_forecast": None if pd.isna(r.hours_to_kickoff) else float(r.hours_to_kickoff)}
    pkg = {
        "game": {"game_id": game_id, "league": league, "season": season, "week": week, "kickoff_utc": str(g.kickoff_utc), "neutral_site": bool(g.neutral_site),
                 "venue": g.get("venue_name"), "conference_game": None if pd.isna(g.conference_game) else bool(g.conference_game)},
        "teamA": away, "teamB": home, "note": "teamA is the away team, teamB is the home team",
        "matchup_edges": edge_rows, "edge_categories": CATEGORIES,
        "model": None if p is None else {"model_version": p.model_version, "projected_score": {"away": float(p.proj_away_pts), "home": float(p.proj_home_pts)},
                                         "projected_margin_home": float(p.proj_margin_home), "projected_total": float(p.proj_total), "win_prob_home": float(p.win_prob_home),
                                         "market_spread_home": None if pd.isna(p.market_spread_home) else float(p.market_spread_home), "market_total": None if pd.isna(p.market_total) else float(p.market_total),
                                         "spread_diff_vs_market": None if pd.isna(p.spread_diff) else float(p.spread_diff), "total_diff_vs_market": None if pd.isna(p.total_diff) else float(p.total_diff),
                                         "why": json.loads(p.contributions) if isinstance(p.contributions, str) else [], "data_quality": float(p.data_quality), "quality_flags": p.quality_flags},
        "market": None if market is None else {"available": market.get("available"), "open": market.get("open"), "current": market.get("current"), "movement": market.get("movement"),
                                               "steam": market.get("steam"), "book_disagreement_spread": market.get("book_disagreement_spread"), "implied": market.get("implied"),
                                               "public_betting": "unavailable", "notes": market.get("notes", [])},
        "weather": weather,
        "metric_descriptions": {k: descs.get(k) for k in PACKAGE_METRICS if k in descs},
        "unavailable": sorted(set(away["unavailable_metrics"]) | set(home["unavailable_metrics"]) | {e["category"] for e in edge_rows if e["unavailable"]}
                              | ({"weather"} if weather is None else set()) | ({"market"} if market is None or not market.get("available") else set())
                              | ({"quarterback_home"} if home["quarterback"] is None else set()) | ({"quarterback_away"} if away["quarterback"] is None else set())),
    }
    pkg = clean(pkg)
    pkg["inputs_hash"] = package_hash(pkg)
    return pkg


def clean(obj):
    """NaN/NA -> None recursively; numpy scalars -> Python scalars. The API rejects NaN tokens (invalid JSON)."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (str, bytes)):
        return obj
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            obj = obj.item()
        except (ValueError, TypeError):
            pass
    if isinstance(obj, float) and obj != obj:
        return None
    try:
        if obj is not None and not isinstance(obj, (str, bytes, bool, int, float, dict, list)) and pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj


def package_hash(pkg: dict) -> str:
    """Hash of everything that should trigger regeneration (excludes timestamps and the hash itself)."""
    core = {k: v for k, v in pkg.items() if k not in ("inputs_hash",)}
    if core.get("market"):
        core["market"] = {k: v for k, v in core["market"].items() if k not in ("notes",)}
        cur = core["market"].get("current") or {}
        core["market"]["current"] = {k: v for k, v in cur.items() if k != "retrieved_at"}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:16]


_NUM_IN_TEXT = re.compile(r"[-+]?\d+(?:\.\d+)?")


def flat_numbers(obj, out: set | None = None) -> set:
    """Every numeric value in the package (rounded to 3 dp) PLUS numbers written inside the package's own text
    (metric descriptions, market notes, flags), for citation validation."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for v in obj.values():
            flat_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            flat_numbers(v, out)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)) and obj == obj:
        out.add(round(float(obj), 3))
    elif isinstance(obj, str):
        for m in _NUM_IN_TEXT.findall(obj):
            try:
                out.add(round(float(m), 3))
            except ValueError:
                pass
    return out
