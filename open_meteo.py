"""
CFBD stats adapter (CFB). Extends providers/cfbd.py. One call per endpoint per week (5 calls/week).

  GET /games/teams?year&week&seasonType                 team box scores  (category/stat string pairs)
  GET /stats/game/advanced?year&week&excludeGarbageTime  per team-game PPA / success / explosiveness / line yards / havoc
  GET /plays?year&week&seasonType                        all plays (ppa per play, clock, down/distance, playType)
  GET /drives?year&week&seasonType                       all drives
  GET /games/players?year&week&seasonType                player box (passing/rushing/receiving/defense)

Every field access is defensive. schema_report() lists which expected fields were present in the first
payload of each endpoint so the deployment log can confirm the assumptions below.

Known ambiguity, handled explicitly: in /games/teams, "interceptions" is a DEFENSIVE count (passes
intercepted BY this team) and "passesIntercepted" duplicates it in some seasons. We do not use either
for pass_int. Interceptions thrown are taken from the PLAYS table (playType contains "Interception"),
which is unambiguous.

Garbage time for plays: CFBD's published rule (config.CFB_GARBAGE_LEAD_BY_PERIOD).
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta

import pandas as pd

import config
from pipeline import ids
from providers.base import RequestManager
from providers.cfbd import BASE, _headers, _g


# ---- fetchers ------------------------------------------------------------------
def fetch_team_box(rm: RequestManager, season: int, week: int, season_type="regular"):
    return rm.get(f"{BASE}/games/teams", params={"year": season, "week": week, "seasonType": season_type}, headers=_headers())


def fetch_advanced(rm: RequestManager, season: int, week: int, exclude_garbage: bool = True, season_type="regular"):
    return rm.get(f"{BASE}/stats/game/advanced", params={"year": season, "week": week, "seasonType": season_type,
                                                          "excludeGarbageTime": str(exclude_garbage).lower()}, headers=_headers())


def fetch_plays(rm: RequestManager, season: int, week: int, season_type="regular"):
    return rm.get(f"{BASE}/plays", params={"year": season, "week": week, "seasonType": season_type}, headers=_headers(), timeout=90)


def fetch_drives(rm: RequestManager, season: int, week: int, season_type="regular"):
    return rm.get(f"{BASE}/drives", params={"year": season, "week": week, "seasonType": season_type}, headers=_headers())


def fetch_player_box(rm: RequestManager, season: int, week: int, season_type="regular"):
    return rm.get(f"{BASE}/games/players", params={"year": season, "week": week, "seasonType": season_type}, headers=_headers())


# ---- helpers -------------------------------------------------------------------
def _num(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _pair(x):
    """'5-12' -> (5, 12); '22/35' -> (22, 35)."""
    if not isinstance(x, str):
        return None, None
    m = re.match(r"^\s*(\d+)\s*[-/]\s*(\d+)\s*$", x)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _mmss(x):
    if not isinstance(x, str) or ":" not in x:
        return None
    m, s = x.split(":")[:2]
    return int(m) * 60 + int(s)


def _effective_at(game_row) -> str:
    return (pd.Timestamp(game_row.kickoff_utc) + timedelta(hours=4)).isoformat() if pd.notna(game_row.kickoff_utc) else game_row.retrieved_at


def _cfbd_game_map(games: pd.DataFrame) -> dict[int, pd.Series]:
    out = {}
    for _, g in games.iterrows():
        try:
            out[int(str(g.provider_game_ids).split(":")[1].strip("}"))] = g
        except (IndexError, ValueError):
            pass
    return out


def _resolve_team(resolver: ids.AliasResolver, name: str, cfbd_id, game_row) -> str | None:
    """Team in a stats payload -> team_id. FCS opponents map to the synthetic id already on the game row."""
    try:
        return resolver.resolve("cfbd", alias=name, provider_id=cfbd_id)
    except ids.UnmatchedAlias:
        resolver.unmatched.pop()
        for tid in (game_row.home_team_id, game_row.away_team_id):
            if tid.startswith("CFB_FCS") and (cfbd_id is None or tid == f"CFB_FCS{cfbd_id}"):
                return tid
        return None


# ---- team box ------------------------------------------------------------------
def normalize_team_box(payload: list[dict], games: pd.DataFrame, resolver: ids.AliasResolver, retrieved_at: datetime,
                       missing: set[str]) -> pd.DataFrame:
    gm = _cfbd_game_map(games)
    rows = []
    for g in payload:
        game = gm.get(_g(g, "id"))
        if game is None:
            continue
        teams = _g(g, "teams") or []
        if len(teams) != 2:
            continue
        for t in teams:
            st = {s.get("category"): s.get("stat") for s in (_g(t, "stats") or [])}
            tid = _resolve_team(resolver, _g(t, "team"), _g(t, "teamId"), game)
            if tid is None:
                missing.add(f"box team {_g(t, 'team')}")
                continue
            opp = game.away_team_id if tid == game.home_team_id else game.home_team_id
            other = teams[1] if t is teams[0] else teams[0]
            c, a = _pair(st.get("completionAttempts"))
            t3c, t3a = _pair(st.get("thirdDownEff"))
            t4c, t4a = _pair(st.get("fourthDownEff"))
            pen, peny = _pair(st.get("totalPenaltiesYards"))
            for want in ("rushingYards", "netPassingYards", "totalYards", "rushingAttempts", "completionAttempts",
                         "thirdDownEff", "fourthDownEff", "turnovers", "fumblesLost", "sacks", "tacklesForLoss",
                         "possessionTime", "firstDowns", "passingTDs", "rushingTDs", "passesDeflected"):
                if want not in st:
                    missing.add(f"box.{want}")
            rows.append({
                "game_id": game.game_id, "team_id": tid, "opponent_id": opp,
                "is_home": (_g(t, "homeAway") == "home"), "is_garbage_filtered": False,
                "points": int(_g(t, "points")) if _g(t, "points") is not None else None,
                "points_allowed": int(_g(other, "points")) if _g(other, "points") is not None else None,
                "plays": None,
                "total_yards": _num(st.get("totalYards")), "rush_att": _num(st.get("rushingAttempts")),
                "rush_yds": _num(st.get("rushingYards")), "rush_td": _num(st.get("rushingTDs")),
                "pass_att": a, "pass_cmp": c, "pass_yds": _num(st.get("netPassingYards")), "pass_td": _num(st.get("passingTDs")),
                "pass_int": None,                               # from plays (see module docstring)
                "sacks_taken": None,                            # box 'sacks' is defensive; taken from plays
                "sack_yds_lost": None, "dropbacks": None,
                "first_downs": _num(st.get("firstDowns")),
                "third_down_att": t3a, "third_down_conv": t3c, "fourth_down_att": t4a, "fourth_down_conv": t4c,
                "fumbles_lost": _num(st.get("fumblesLost")), "turnovers": _num(st.get("turnovers")), "takeaways": None,
                "penalties": pen, "penalty_yds": peny, "possession_sec": _mmss(st.get("possessionTime")),
                "tfl": _num(st.get("tacklesForLoss")), "sacks_made": _num(st.get("sacks")),
                "int_made": _num(st.get("interceptions")), "pass_deflections": _num(st.get("passesDeflected")),
                "fumbles_forced": None, "punts": None, "punt_yds": None, "fg_att": None, "fg_made": None,
                "source": "cfbd", "retrieved_at": retrieved_at.isoformat(), "effective_at": _effective_at(game),
            })
    return pd.DataFrame(rows)


# ---- advanced ------------------------------------------------------------------
_ADV_MAP = {  # CFBD offense/defense object -> our column suffix
    "plays": "plays", "ppa": "ppa_play", "successRate": "success_rate", "explosiveness": "explosiveness",
    "powerSuccess": "power_success", "stuffRate": "stuff_rate", "lineYards": "line_yards",
    "secondLevelYards": "second_level_yds", "openFieldYards": "open_field_yds",
}


def normalize_advanced(payload: list[dict], games: pd.DataFrame, resolver: ids.AliasResolver, retrieved_at: datetime,
                       garbage_filtered: bool, missing: set[str]) -> pd.DataFrame:
    """CFBD-native advanced values. Column names match team_game_advanced; the job overlays them on computed rows."""
    gm = _cfbd_game_map(games)
    rows = []
    for r in payload:
        game = gm.get(_g(r, "gameId"))
        if game is None:
            continue
        tid = _resolve_team(resolver, _g(r, "team"), None, game)
        if tid is None:
            missing.add(f"adv team {_g(r, 'team')}")
            continue
        opp = game.away_team_id if tid == game.home_team_id else game.home_team_id
        row = {"game_id": game.game_id, "team_id": tid, "opponent_id": opp, "is_garbage_filtered": garbage_filtered}
        for side, prefix in (("offense", "off_"), ("defense", "def_")):
            o = _g(r, side) or {}
            if not o:
                missing.add(f"adv.{side}")
            for k, suf in _ADV_MAP.items():
                if k in o:
                    row[f"{prefix}{suf}"] = _num(o[k])
                elif side == "offense":
                    missing.add(f"adv.{side}.{k}")
            row[f"{prefix}ppa_pass"] = _num(_g(o, "passingPlays", "ppa"))
            row[f"{prefix}ppa_rush"] = _num(_g(o, "rushingPlays", "ppa"))
            row[f"{prefix}success_pass"] = _num(_g(o, "passingPlays", "successRate"))
            row[f"{prefix}success_rush"] = _num(_g(o, "rushingPlays", "successRate"))
            row[f"{prefix}success_std_downs"] = _num(_g(o, "standardDowns", "successRate"))
            row[f"{prefix}success_pass_downs"] = _num(_g(o, "passingDowns", "successRate"))
            hv = _g(o, "havoc") or {}
            if side == "defense":
                row["def_havoc"] = _num(hv.get("total")); row["def_havoc_front"] = _num(hv.get("frontSeven")); row["def_havoc_db"] = _num(hv.get("db"))
                row["def_stuff_rate"] = row.pop("def_stuff_rate", None)
                row["def_line_yards_allowed"] = row.pop("def_line_yards", None)
            else:
                row["off_havoc_allowed"] = _num(hv.get("total"))
                row["off_stuff_rate_allowed"] = row.pop("off_stuff_rate", None)
        rows.append(row)
    return pd.DataFrame(rows)


# ---- plays ---------------------------------------------------------------------
def _play_type(pt: str | None) -> tuple[str, bool, bool, str | None]:
    """returns (normalized type, is_dropback, is_sack, turnover_type)"""
    s = (pt or "").lower()
    if "kickoff" in s:
        return "KICKOFF", False, False, None
    if "punt" in s:
        return "PUNT", False, False, ("FUM" if "opponent" in s or "muff" in s else None)
    if "field goal" in s:
        return "FG", False, False, None
    if "extra point" in s or "two point" in s or "2pt" in s:
        return "XP", False, False, None
    if "timeout" in s:
        return "TIMEOUT", False, False, None
    if "penalty" in s:
        return "PENALTY", False, False, None
    if "kneel" in s:
        return "KNEEL", False, False, None
    if "spike" in s:
        return "SPIKE", False, False, None
    if "sack" in s:
        return "SACK", True, True, None
    if "interception" in s:
        return "PASS", True, False, "INT"
    if "pass" in s:
        return "PASS", True, False, ("FUM" if "opponent" in s else None)
    if "fumble" in s:
        return "RUSH", False, False, ("FUM" if "opponent" in s else None)
    if "rush" in s:
        return "RUSH", False, False, None
    if "end of" in s or "period" in s:
        return "OTHER", False, False, None
    return "OTHER", False, False, None


def _garbage(period, off_score, def_score) -> bool:
    thr = config.CFB_GARBAGE_LEAD_BY_PERIOD.get(int(period) if period else 0)
    return bool(thr and abs((off_score or 0) - (def_score or 0)) >= thr)


def normalize_plays(payload: list[dict], games: pd.DataFrame, resolver: ids.AliasResolver, retrieved_at: datetime,
                    missing: set[str]) -> pd.DataFrame:
    gm = _cfbd_game_map(games)
    rows = []
    first = True
    for p in payload:
        game = gm.get(_g(p, "gameId"))
        if game is None:
            continue
        if first:
            for f in ("id", "driveId", "gameId", "offense", "defense", "period", "clock", "yardsToGoal", "down", "distance",
                      "yardsGained", "playType", "ppa", "offenseScore", "defenseScore", "scoring"):
                if f not in p:
                    missing.add(f"plays.{f}")
            first = False
        off = _resolve_team(resolver, _g(p, "offense"), None, game)
        deff = _resolve_team(resolver, _g(p, "defense"), None, game)
        if off is None or deff is None:
            continue
        ptype, is_db, is_sack, tot = _play_type(_g(p, "playType"))
        clk = _g(p, "clock") or {}
        period = _g(p, "period")
        csec = (int(clk.get("minutes") or 0) * 60 + int(clk.get("seconds") or 0)) if clk else None
        gsec = ((4 - int(period)) * 900 + csec) if (period and csec is not None and int(period) <= 4) else None
        os_, ds_ = _g(p, "offenseScore"), _g(p, "defenseScore")
        rows.append({
            "play_id": f"{game.game_id}_{_g(p, 'id')}", "game_id": game.game_id,
            "offense_team_id": off, "defense_team_id": deff,
            "period": int(period) if period is not None else None, "clock_sec_remaining": csec, "game_sec_remaining": gsec,
            "drive_id": f"{game.game_id}_{_g(p, 'driveId')}" if _g(p, "driveId") is not None else None,
            "down": int(_g(p, "down")) if _g(p, "down") is not None else None,
            "distance": int(_g(p, "distance")) if _g(p, "distance") is not None else None,
            "yardline_100": int(_g(p, "yardsToGoal")) if _g(p, "yardsToGoal") is not None else None,
            "play_type": ptype, "yards_gained": int(_g(p, "yardsGained") or 0),
            "is_success": None,                         # metric engine applies the yardage rule for CFB
            "ppa": _num(_g(p, "ppa")),
            "is_dropback": is_db, "is_scramble": False, "is_sack": is_sack,
            "is_turnover": tot is not None, "turnover_type": tot,
            "is_td": bool(_g(p, "scoring")) and "touchdown" in (_g(p, "playType") or "").lower(),
            "is_complete": (ptype == "PASS" and "incompletion" not in (_g(p, "playType") or "").lower() and tot != "INT"),
            "is_garbage_time": _garbage(period, os_, ds_),
            "score_diff_pre": (int(os_) - int(ds_)) if os_ is not None and ds_ is not None else None,
            "wp_pre": None, "passer_id": None, "rusher_id": None, "receiver_id": None,
            "air_yards": None, "run_gap": None, "run_location": None, "shotgun": None, "play_action": None, "rpo": None,
            "n_blitzers": None, "n_pass_rushers": None, "qb_hit": None, "is_tfl": None, "is_pbu": None, "is_ff": None,
            "int_worthy": None, "cpoe": None,
            "posteam_score": int(os_) if os_ is not None else None, "posteam_score_post": None,
            "source": "cfbd", "retrieved_at": retrieved_at.isoformat(), "effective_at": _effective_at(game),
        })
    return pd.DataFrame(rows)


# ---- drives --------------------------------------------------------------------
_DRIVE_RESULT = {"TD": "TD", "FG": "FG", "MISSED FG": "MISSED_FG", "PUNT": "PUNT", "INT": "TURNOVER", "FUMBLE": "TURNOVER",
                 "DOWNS": "DOWNS", "END OF HALF": "END_HALF", "END OF GAME": "END_GAME", "SF": "SAFETY", "END OF 4TH QUARTER": "END_GAME"}


def normalize_drives(payload: list[dict], games: pd.DataFrame, resolver: ids.AliasResolver, retrieved_at: datetime,
                     missing: set[str]) -> pd.DataFrame:
    gm = _cfbd_game_map(games)
    rows = []
    first = True
    for d in payload:
        game = gm.get(_g(d, "gameId"))
        if game is None:
            continue
        if first:
            for f in ("id", "driveNumber", "offense", "defense", "startPeriod", "startYardsToGoal", "endYardsToGoal",
                      "plays", "yards", "driveResult", "startOffenseScore", "endOffenseScore", "elapsed"):
                if f not in d:
                    missing.add(f"drives.{f}")
            first = False
        off = _resolve_team(resolver, _g(d, "offense"), None, game)
        deff = _resolve_team(resolver, _g(d, "defense"), None, game)
        if off is None or deff is None:
            continue
        sy, ey = _g(d, "startYardsToGoal"), _g(d, "endYardsToGoal")
        min_y = min([v for v in (sy, ey) if v is not None], default=None)
        res_raw = str(_g(d, "driveResult") or "").upper()
        result = _DRIVE_RESULT.get(res_raw, "TURNOVER" if "INT" in res_raw or "FUMBLE" in res_raw else ("TD" if "TD" in res_raw else "OTHER"))
        so, eo = _g(d, "startOffenseScore"), _g(d, "endOffenseScore")
        el = _g(d, "elapsed") or {}
        rows.append({
            "drive_id": f"{game.game_id}_{_g(d, 'id')}", "game_id": game.game_id, "offense_team_id": off, "defense_team_id": deff,
            "drive_number": int(_g(d, "driveNumber") or 0),
            "start_period": int(_g(d, "startPeriod")) if _g(d, "startPeriod") is not None else None,
            "start_yardline_100": int(sy) if sy is not None else None, "end_yardline_100": int(ey) if ey is not None else None,
            "plays": int(_g(d, "plays") or 0), "yards": int(_g(d, "yards") or 0),
            "elapsed_sec": (int(el.get("minutes") or 0) * 60 + int(el.get("seconds") or 0)) if isinstance(el, dict) else None,
            "result": result,
            "points": max(0, int(eo) - int(so)) if so is not None and eo is not None else None,
            "reached_opp_40": bool(min_y is not None and min_y <= 40) or result in ("TD", "FG", "MISSED_FG"),
            "reached_rz": bool(min_y is not None and min_y <= 20) or result == "TD",
            "is_garbage_time": _garbage(_g(d, "startPeriod"), so, _g(d, "startDefenseScore")),
            "source": "cfbd", "retrieved_at": retrieved_at.isoformat(), "effective_at": _effective_at(game),
        })
    return pd.DataFrame(rows)


# ---- player box (QB rows) ------------------------------------------------------
def normalize_qb_box(payload: list[dict], games: pd.DataFrame, resolver: ids.AliasResolver, retrieved_at: datetime,
                     missing: set[str]) -> pd.DataFrame:
    gm = _cfbd_game_map(games)
    rows = []
    for g in payload:
        game = gm.get(_g(g, "id"))
        if game is None:
            continue
        for t in (_g(g, "teams") or []):
            tid = _resolve_team(resolver, _g(t, "team"), _g(t, "teamId"), game)
            if tid is None:
                continue
            cats = {c.get("name"): c for c in (_g(t, "categories") or [])}
            passing = cats.get("passing")
            if not passing:
                missing.add("players.passing"); continue
            by_athlete: dict = {}
            for typ in (_g(passing, "types") or []):
                for a in (_g(typ, "athletes") or []):
                    by_athlete.setdefault(str(a.get("id")), {"name": a.get("name")})[typ.get("name")] = a.get("stat")
            for aid, st in by_athlete.items():
                c, a = _pair(st.get("C/ATT"))
                rows.append({
                    "game_id": game.game_id, "team_id": tid, "player_id": f"CFB_P_{aid}", "position": "QB", "started": None,
                    "snaps_off": None, "snaps_def": None, "pass_att": a, "pass_cmp": c, "pass_yds": _num(st.get("YDS")),
                    "pass_td": _num(st.get("TD")), "pass_int": _num(st.get("INT")), "sacks_taken": None, "dropbacks": None,
                    "ppa_dropback": None, "qbr": _num(st.get("QBR")), "cpoe": None, "int_worthy": None,
                    "pressured_dropbacks": None, "pressured_ppa": None, "clean_ppa": None,
                    "rush_att": None, "rush_yds": None, "rush_td": None, "targets": None, "receptions": None, "rec_yds": None,
                    "rec_td": None, "tackles": None, "tfl": None, "sacks": None, "pressures": None, "ints": None, "pbu": None, "ff": None,
                    "player_name": st.get("name"),
                    "source": "cfbd", "retrieved_at": retrieved_at.isoformat(), "effective_at": _effective_at(game),
                })
    return pd.DataFrame(rows)


def schema_report(name: str, payload, missing: set[str]) -> list[str]:
    out = [f"{name}: {len(payload) if isinstance(payload, list) else 'n/a'} records"]
    if missing:
        out.append(f"{name}: MISSING/unmapped -> {sorted(missing)}")
    return out
