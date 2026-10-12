"""
CFBD roster-intelligence adapter (CFB). One call per endpoint per season (8 calls/season):

  /player/returning?year          provider's returning production (offense only): percentPPA, usage shares
  /player/portal?year             transfers with origin/destination/rating/stars (no player id -> matched by name)
  /recruiting/teams?year          class rank + points per team
  /recruiting/players?year        individual recruits with stars/rating (for blue-chip ratio, freshman ordering)
  /talent?year                    team talent composite
  /player/usage?year              per-player usage shares (overall/pass/rush/downs) with player ids
  /stats/player/season?year       long-format player season stats (category/statType/stat)
  /draft/picks?year               NFL draft picks with collegeAthleteId (lost players)

Every normalizer is defensive; unknown teams are collected into `unmatched` rather than guessed.
"""
from __future__ import annotations
import re
from datetime import datetime

import pandas as pd

from pipeline import ids
from providers.base import RequestManager
from providers.cfbd import BASE, _headers, _g
from providers.cfbd_context import _POS_NORM


def fetch(rm: RequestManager, endpoint: str, season: int, **extra):
    return rm.get(f"{BASE}{endpoint}", params={"year": season, **extra}, headers=_headers(), timeout=90)


def _tid(resolver: ids.AliasResolver, name, unmatched: set[str]):
    if not name:
        return None
    try:
        return resolver.resolve("cfbd", alias=name)
    except ids.UnmatchedAlias:
        resolver.unmatched.pop(); unmatched.add(str(name)); return None


def _num(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def normalize_returning(payload, season, resolver, ts: datetime, unmatched) -> pd.DataFrame:
    rows = []
    for r in payload:
        tid = _tid(resolver, _g(r, "team"), unmatched)
        if not tid:
            continue
        rows.append({"team_id": tid, "season": season, "as_of_week": 0,
                     "rp_total": _num(_g(r, "percentPPA")), "rp_offense": _num(_g(r, "percentPPA")), "rp_defense": None,
                     "rp_passing": _num(_g(r, "percentPassingPPA")), "rp_rushing": _num(_g(r, "percentRushingPPA")),
                     "rp_receiving": _num(_g(r, "percentReceivingPPA")),
                     "usage_returning": _num(_g(r, "usage")), "passing_usage_returning": _num(_g(r, "passingUsage")),
                     "rushing_usage_returning": _num(_g(r, "rushingUsage")), "receiving_usage_returning": _num(_g(r, "receivingUsage")),
                     "ol_starts_returning": None, "ol_starts_returning_is_proxy": None, "def_pressure_returning": None,
                     "secondary_snaps_returning": None, "method": "cfbd_returning", "source": "cfbd", "retrieved_at": ts.isoformat()})
    return pd.DataFrame(rows)


def normalize_portal(payload, season, resolver, ts: datetime, unmatched) -> pd.DataFrame:
    rows = []
    for r in payload:
        name = f"{_g(r, 'firstName') or ''} {_g(r, 'lastName') or ''}".strip()
        rows.append({"transfer_id": f"{season}_{re.sub(r'[^a-z]', '', name.lower())}_{_g(r, 'origin')}", "season": season,
                     "player_id": None, "player_name": name, "position": _POS_NORM.get(str(_g(r, "position")).upper(), _g(r, "position")),
                     "from_team_id": _tid(resolver, _g(r, "origin"), set()), "from_team_raw": _g(r, "origin"),
                     "to_team_id": _tid(resolver, _g(r, "destination"), set()), "to_team_raw": _g(r, "destination"),
                     "stars": _g(r, "stars"), "rating": _num(_g(r, "rating")), "transfer_rank": None, "eligibility": _g(r, "eligibility"),
                     "prior_season_usage": None, "prior_season_snaps": None, "prior_production": None, "projected_role": "UNKNOWN",
                     "announced_at": (_g(r, "transferDate") or "")[:10] or None, "source": "cfbd", "retrieved_at": ts.isoformat()})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["transfer_rank"] = df.rating.rank(ascending=False, method="min").astype("Int64")
    return df


def normalize_recruiting_teams(payload, season, resolver, ts: datetime, unmatched) -> pd.DataFrame:
    rows = []
    for r in payload:
        tid = _tid(resolver, _g(r, "team"), unmatched)
        if tid:
            rows.append({"team_id": tid, "season": season, "class_rank": _g(r, "rank"), "class_points": _num(_g(r, "points"))})
    return pd.DataFrame(rows)


def normalize_recruits(payload, season, resolver, ts: datetime, unmatched) -> pd.DataFrame:
    rows = []
    for r in payload:
        tid = _tid(resolver, _g(r, "committedTo"), unmatched) if _g(r, "committedTo") else None
        rows.append({"recruit_id": _g(r, "id"), "season": season, "athlete_id": _g(r, "athleteId"), "name": _g(r, "name"),
                     "position": _POS_NORM.get(str(_g(r, "position")).upper(), _g(r, "position")), "team_id": tid,
                     "stars": _g(r, "stars"), "rating": _num(_g(r, "rating")), "ranking": _g(r, "ranking"),
                     "recruit_type": _g(r, "recruitType"), "source": "cfbd", "retrieved_at": ts.isoformat()})
    return pd.DataFrame(rows)


def class_summary(recruits: pd.DataFrame, teams_class: pd.DataFrame, season: int) -> pd.DataFrame:
    """recruiting_classes rows: rank/points from /recruiting/teams, star counts and blue-chip ratio from recruits."""
    hs = recruits[(recruits.recruit_type.fillna("HighSchool") == "HighSchool") & recruits.team_id.notna()]
    g = hs.groupby("team_id").agg(commits=("recruit_id", "count"), avg_rating=("rating", "mean"),
                                  five_stars=("stars", lambda s: int((s == 5).sum())), four_stars=("stars", lambda s: int((s == 4).sum())),
                                  three_stars=("stars", lambda s: int((s == 3).sum())))
    g["blue_chip_ratio"] = (g.five_stars + g.four_stars) / g.commits.replace(0, pd.NA)
    g = g.reset_index()
    out = teams_class.merge(g, on="team_id", how="outer")
    out["season"] = season
    return out


def normalize_talent(payload, season, resolver, ts: datetime, unmatched) -> pd.DataFrame:
    rows = []
    for r in payload:
        tid = _tid(resolver, _g(r, "team") or _g(r, "school"), unmatched)
        if tid:
            rows.append({"team_id": tid, "season": season, "talent_composite": _num(_g(r, "talent")), "source": "cfbd", "retrieved_at": ts.isoformat()})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["talent_rank"] = df.talent_composite.rank(ascending=False, method="min").astype("Int64")
    return df


def normalize_usage(payload, season, resolver, ts: datetime, unmatched) -> pd.DataFrame:
    rows = []
    for r in payload:
        tid = _tid(resolver, _g(r, "team"), unmatched)
        if not tid:
            continue
        u = _g(r, "usage") or {}
        rows.append({"player_id": f"CFB_P_{_g(r, 'id')}", "team_id": tid, "season": season, "player_name": _g(r, "name"),
                     "position_raw": _g(r, "position"), "position": _POS_NORM.get(str(_g(r, "position")).upper(), None),
                     "usage_overall": _num(u.get("overall")), "usage_pass": _num(u.get("pass")), "usage_rush": _num(u.get("rush")),
                     "usage_first_down": _num(u.get("firstDown")), "usage_std_downs": _num(u.get("standardDowns")), "usage_pass_downs": _num(u.get("passingDowns"))})
    return pd.DataFrame(rows)


_STAT_MAP = {("passing", "ATT"): "pass_att", ("passing", "COMPLETIONS"): "pass_cmp", ("passing", "YDS"): "pass_yds", ("passing", "TD"): "pass_td",
             ("passing", "INT"): "pass_int", ("rushing", "CAR"): "rush_att", ("rushing", "YDS"): "rush_yds", ("rushing", "TD"): "rush_td",
             ("receiving", "REC"): "receptions", ("receiving", "YDS"): "rec_yds", ("receiving", "TD"): "rec_td",
             ("defensive", "TOT"): "tackles", ("defensive", "SOLO"): "tackles_solo", ("defensive", "TFL"): "tfl", ("defensive", "SACKS"): "sacks",
             ("defensive", "PD"): "pbu", ("defensive", "QB HUR"): "qb_hurries", ("interceptions", "INT"): "ints", ("fumbles", "FUM"): "fumbles"}


def normalize_player_season_stats(payload, season, resolver, ts: datetime, unmatched) -> pd.DataFrame:
    rows: dict = {}
    for r in payload:
        key = (str(_g(r, "category") or "").lower(), str(_g(r, "statType") or "").upper())
        col = _STAT_MAP.get(key)
        if col is None:
            continue
        tid = _tid(resolver, _g(r, "team"), unmatched)
        if not tid:
            continue
        pid = f"CFB_P_{_g(r, 'playerId')}"
        row = rows.setdefault((pid, tid), {"player_id": pid, "team_id": tid, "season": season, "player_name": _g(r, "player"),
                                            "position_raw": _g(r, "position"), "position": _POS_NORM.get(str(_g(r, "position")).upper(), None)})
        row[col] = _num(_g(r, "stat"))
    out = pd.DataFrame(list(rows.values()))
    if not out.empty:
        out["source"] = "cfbd"; out["retrieved_at"] = ts.isoformat()
    return out


def normalize_draft_picks(payload, season, resolver, ts: datetime, unmatched) -> pd.DataFrame:
    rows = []
    for r in payload:
        rows.append({"season": season, "round": _g(r, "round"), "pick": _g(r, "pick"), "overall": _g(r, "overall"),
                     "player_id": f"CFB_P_{_g(r, 'collegeAthleteId')}" if _g(r, "collegeAthleteId") else None,
                     "team_id": _tid(resolver, _g(r, "collegeTeam"), set()), "college_raw": _g(r, "collegeTeam"),
                     "nfl_team_raw": _g(r, "nflTeam"), "name": _g(r, "name"), "position": _POS_NORM.get(str(_g(r, "position")).upper(), _g(r, "position")),
                     "source": "cfbd", "retrieved_at": ts.isoformat()})
    return pd.DataFrame(rows)
