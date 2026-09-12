"""
CFBD context adapter (CFB): roster, rankings, coaches, venues. One call each.

  GET /roster?year               all FBS rosters (if the API requires a team param it returns 400; the job then
                                 falls back to one call per FBS team -> 138 calls, once per season)
  GET /rankings?year&week        AP / Coaches / CFP polls for that week (true as-of history)
  GET /coaches?year              head coaches with season records
  GET /venues                    lat/long/timezone/dome for every venue CFBD knows
"""
from __future__ import annotations
from datetime import datetime

import pandas as pd

from pipeline import ids
from providers.base import RequestManager
from providers.cfbd import BASE, _headers, _g

_POS_NORM = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE", "OL": "OL", "OT": "OL", "OG": "OL", "C": "OL", "G": "OL",
             "T": "OL", "DL": "DL", "DT": "DL", "NT": "DL", "DE": "EDGE", "EDGE": "EDGE", "OLB": "LB", "LB": "LB", "ILB": "LB",
             "MLB": "LB", "CB": "CB", "DB": "CB", "S": "S", "SAF": "S", "FS": "S", "SS": "S", "PK": "K", "K": "K", "P": "P", "LS": "LS"}
_YEAR_CODE = {1: "FR", 2: "SO", 3: "JR", 4: "SR", 5: "GR"}   # CFBD roster.year integer code; labeled cfbd_year_code in output
_POLL = {"AP Top 25": "AP", "Coaches Poll": "COACHES", "Playoff Committee Rankings": "CFP", "AFCA Division I Coaches Poll": "COACHES"}


def fetch_roster(rm: RequestManager, season: int, team: str | None = None):
    params = {"year": season}
    if team:
        params["team"] = team
    return rm.get(f"{BASE}/roster", params=params, headers=_headers())


def fetch_rankings(rm: RequestManager, season: int, week: int, season_type="regular"):
    return rm.get(f"{BASE}/rankings", params={"year": season, "week": week, "seasonType": season_type}, headers=_headers())


def fetch_coaches(rm: RequestManager, season: int):
    return rm.get(f"{BASE}/coaches", params={"year": season}, headers=_headers())


def fetch_venues(rm: RequestManager):
    return rm.get(f"{BASE}/venues", headers=_headers())


def normalize_roster(payload: list[dict], season: int, week: int, resolver: ids.AliasResolver, retrieved_at: datetime,
                     prior: pd.DataFrame | None, unmatched: set[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (roster_snapshot rows, players rows). Non-FBS teams in the payload are skipped."""
    ts = retrieved_at.isoformat()
    prior_map: dict[str, str] = {}
    if prior is not None and not prior.empty:
        last = prior[prior.week == prior.week.max()]
        prior_map = dict(zip(last.player_id, last.team_id))
    ros, players = [], []
    for p in payload:
        team = _g(p, "team")
        try:
            tid = resolver.resolve("cfbd", alias=team)
        except ids.UnmatchedAlias:
            resolver.unmatched.pop(); unmatched.add(str(team)); continue
        pid = f"CFB_P_{_g(p, 'id')}"
        yr = _g(p, "year")
        prior_team = prior_map.get(pid)
        arrival = None
        if prior_map:
            arrival = "RETURNING" if prior_team == tid else ("TRANSFER" if prior_team else ("FRESHMAN" if yr in (1, None) else "TRANSFER"))
        ros.append({
            "team_id": tid, "season": season, "week": week, "player_id": pid,
            "position": _POS_NORM.get(str(_g(p, "position")).upper(), None), "jersey": _g(p, "jersey"),
            "class_year": _YEAR_CODE.get(yr) if isinstance(yr, int) else None, "years_exp": yr if isinstance(yr, int) else None,
            "status": "ACT", "is_new_to_team": (prior_team != tid) if prior_map else None, "arrival_type": arrival,
            "prior_team_id": prior_team, "source": "cfbd", "retrieved_at": ts,
        })
        players.append({
            "player_id": pid, "league": "CFB", "full_name": f"{_g(p, 'firstName') or ''} {_g(p, 'lastName') or ''}".strip(),
            "position": _POS_NORM.get(str(_g(p, "position")).upper(), None), "position_raw": _g(p, "position"),
            "birth_date": None, "height_in": _g(p, "height"), "weight_lb": _g(p, "weight"),
            "draft_year": None, "draft_round": None, "draft_pick": None,
            "recruit_stars": None, "recruit_rating": None, "recruit_rank_natl": None, "source": "cfbd", "retrieved_at": ts,
        })
    return pd.DataFrame(ros), pd.DataFrame(players)


def normalize_rankings(payload: list[dict], resolver: ids.AliasResolver, retrieved_at: datetime, unmatched: set[str]) -> pd.DataFrame:
    rows = []
    for wk in payload:
        season, week = _g(wk, "season"), _g(wk, "week")
        for poll in (_g(wk, "polls") or []):
            code = _POLL.get(_g(poll, "poll"))
            if code is None:
                continue
            for r in (_g(poll, "ranks") or []):
                try:
                    tid = resolver.resolve("cfbd", alias=_g(r, "school"))
                except ids.UnmatchedAlias:
                    resolver.unmatched.pop(); unmatched.add(str(_g(r, "school"))); continue
                rows.append({"season": season, "week": week, "poll": code, "team_id": tid, "rank": _g(r, "rank"),
                             "points": _g(r, "points"), "first_place_votes": _g(r, "firstPlaceVotes"),
                             "source": "cfbd", "retrieved_at": retrieved_at.isoformat(), "effective_at": retrieved_at.isoformat()})
    return pd.DataFrame(rows)


def normalize_coaches(payload: list[dict], season: int, resolver: ids.AliasResolver, retrieved_at: datetime, unmatched: set[str]) -> pd.DataFrame:
    rows = []
    for c in payload:
        name = f"{_g(c, 'firstName') or ''} {_g(c, 'lastName') or ''}".strip()
        for s in (_g(c, "seasons") or []):
            if _g(s, "year") != season:
                continue
            try:
                tid = resolver.resolve("cfbd", alias=_g(s, "school"))
            except ids.UnmatchedAlias:
                resolver.unmatched.pop(); unmatched.add(str(_g(s, "school"))); continue
            rows.append({"team_id": tid, "season": season, "role": "HC", "coach_name": name,
                         "coach_id": name.lower().replace(" ", "_").replace(".", ""),
                         "effective_from": f"{season}-01-01", "effective_to": None, "is_first_season_in_role": None,
                         "source": "cfbd", "entered_by": None, "retrieved_at": retrieved_at.isoformat(),
                         "games": _g(s, "games"), "wins": _g(s, "wins"), "losses": _g(s, "losses")})
    df = pd.DataFrame(rows)
    if not df.empty:
        # a team with two HC rows in one season (mid-season change) keeps both; flag for manual effective dates
        df["needs_manual_dates"] = df.groupby("team_id").team_id.transform("count") > 1
    return df


def normalize_venues(payload: list[dict], retrieved_at: datetime) -> pd.DataFrame:
    rows = []
    for v in payload:
        rows.append({
            "venue_id": f"V_CFBD_{_g(v, 'id')}", "name": _g(v, "name"), "city": _g(v, "city"), "state": _g(v, "state"),
            "country": _g(v, "countryCode") or "US", "latitude": _g(v, "latitude"), "longitude": _g(v, "longitude"),
            "elevation_m": _g(v, "elevation"), "timezone": _g(v, "timezone"), "capacity": _g(v, "capacity"),
            "roof": "dome" if _g(v, "dome") else "outdoors", "surface": "grass" if _g(v, "grass") else ("turf" if _g(v, "grass") is False else None),
            "source": "cfbd", "retrieved_at": retrieved_at.isoformat(),
        })
    return pd.DataFrame(rows)
