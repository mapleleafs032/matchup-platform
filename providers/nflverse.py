"""
nflverse adapter (NFL). Free, no key. Files are GitHub release assets.

Normalization notes (verified against the live file 2026-09-02):
  * spread_line is POSITIVE when the HOME team is favored. Our spread_home is negative when home favored,
    so spread_home = -spread_line.
  * gametime is Eastern local time as "HH:MM"; gameday is a date. Converted to UTC via America/New_York.
  * roof can be missing for some stadiums (e.g., HOU in 2026). Missing -> NULL, never assumed outdoors.
  * game_type REG/WC/DIV/CON/SB -> season_type REG/POST; week numbering continues (19..22) in POST.
  * 'LA' is the Rams. We map LA and LAR to NFL_LAR.
"""
from __future__ import annotations
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import config
from pipeline import ids
from providers.base import RequestManager

SCHEDULES_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
TEAMS_URL = "https://raw.githubusercontent.com/nflverse/nflverse-pbp/master/teams_colors_logos.csv"

ABBR_OVERRIDES = {"LA": "LAR"}   # our canonical abbreviation


def fetch_teams(rm: RequestManager) -> pd.DataFrame:
    res = rm.get(TEAMS_URL, expect_json=False)
    df = pd.read_csv(io.StringIO(res.payload))
    df.attrs["raw_id"] = res.raw_id
    df.attrs["retrieved_at"] = res.retrieved_at
    return df


def normalize_teams(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Returns (teams frame, alias rows). Only current 32 franchises (skip historical abbrs)."""
    cur = raw[raw.team_abbr.isin(CURRENT_ABBRS)].copy()
    teams, aliases = [], []
    for _, r in cur.iterrows():
        canon = ABBR_OVERRIDES.get(r.team_abbr, r.team_abbr)
        tid = ids.make_team_id("NFL", canon)
        city = r.team_name.replace(r.team_nick, "").strip()
        teams.append({
            "team_id": tid, "league": "NFL", "abbr": canon, "school_or_city": city, "mascot": r.team_nick,
            "display_name": r.team_name, "conference": r.team_conf, "division": r.team_division,
            "classification": None, "home_venue_id": None, "primary_color": r.team_color,
            "secondary_color": r.team_color2, "logo_url": r.team_logo_espn,
            "source": "nflverse", "retrieved_at": raw.attrs["retrieved_at"].isoformat(),
        })
        aliases.append({"provider": "nflverse", "alias": r.team_abbr, "provider_id": str(r.team_id), "team_id": tid,
                        "season_from": None, "season_to": None})
        if canon != r.team_abbr:
            aliases.append({"provider": "nflverse", "alias": canon, "provider_id": None, "team_id": tid,
                            "season_from": None, "season_to": None})
        # The Odds API uses full names; ESPN uses display names. Seed both from the same trusted string.
        aliases.append({"provider": "odds_api", "alias": r.team_name, "provider_id": None, "team_id": tid,
                        "season_from": None, "season_to": None})
        aliases.append({"provider": "espn", "alias": r.team_name, "provider_id": None, "team_id": tid,
                        "season_from": None, "season_to": None})
    return pd.DataFrame(teams), aliases


CURRENT_ABBRS = {"ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND",
                 "JAX", "KC", "LA", "LAC", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF",
                 "TB", "TEN", "WAS"}


def fetch_schedules(rm: RequestManager) -> pd.DataFrame:
    res = rm.get(SCHEDULES_URL, expect_json=False)
    df = pd.read_csv(io.StringIO(res.payload))
    df.attrs["raw_id"] = res.raw_id
    df.attrs["retrieved_at"] = res.retrieved_at
    return df


def _kickoff_utc(gameday: str, gametime) -> tuple[pd.Timestamp | None, bool]:
    if pd.isna(gametime) or str(gametime).strip() == "":
        return None, True
    local = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(config.NFL_TIMEZONE))
    return pd.Timestamp(local.astimezone(ZoneInfo("UTC"))), False


def normalize_schedules(raw: pd.DataFrame, season: int, resolver: ids.AliasResolver) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (games frame, closing_lines frame for completed games).
    Raises ids.UnmatchedAlias via resolver on any unknown abbreviation; caller handles.
    """
    d = raw[raw.season == season].copy()
    retrieved_at = raw.attrs["retrieved_at"].isoformat()
    games, closes = [], []
    for _, r in d.iterrows():
        away = resolver.resolve("nflverse", alias=r.away_team)
        home = resolver.resolve("nflverse", alias=r.home_team)
        season_type = config.NFL_GAME_TYPE_TO_SEASON_TYPE[r.game_type]
        gid = ids.make_game_id(season, "NFL", season_type, int(r.week), away, home)
        kick, tba = _kickoff_utc(r.gameday, r.gametime)
        completed = pd.notna(r.home_score) and pd.notna(r.away_score)
        roof = r.roof if isinstance(r.roof, str) and r.roof in ("outdoors", "dome", "closed", "open") else None
        # nflverse records a retractable roof's actual state for the game; keep it (weather treats closed as indoor)
        roof_norm = {"outdoors": "outdoors", "dome": "dome", "closed": "retractable_closed", "open": "retractable_open"}.get(roof) if roof else None
        games.append({
            "game_id": gid, "league": "NFL", "season": season, "season_type": season_type, "week": int(r.week),
            "away_team_id": away, "home_team_id": home,
            "kickoff_utc": kick, "kickoff_is_tba": tba, "kickoff_source": "nflverse",
            "venue_id": f"V_NFL_{r.stadium_id}" if pd.notna(r.stadium_id) else None,
            "venue_name": r.stadium if pd.notna(r.stadium) else None, "venue_roof": roof_norm,
            "neutral_site": (str(r.location).lower() == "neutral"), "conference_game": bool(r.div_game) if pd.notna(r.div_game) else None,
            "is_fcs_game": False, "tv_network": None, "tv_source": None,
            "status": "FINAL" if completed else "SCHEDULED", "locked_at": None,
            "provider_game_ids": '{"nflverse":"%s","espn":"%s","pfr":"%s"}' % (r.game_id, r.espn if pd.notna(r.espn) else "", r.pfr if pd.notna(r.pfr) else ""),
            "source": "nflverse", "retrieved_at": retrieved_at, "effective_at": retrieved_at,
        })
        if completed and pd.notna(r.spread_line):
            closes.append({
                "game_id": gid, "book": "nflverse_close",
                "spread_home": -float(r.spread_line),            # sign flip, see module docstring
                "ml_home": int(r.home_moneyline) if pd.notna(r.home_moneyline) else None,
                "ml_away": int(r.away_moneyline) if pd.notna(r.away_moneyline) else None,
                "total": float(r.total_line) if pd.notna(r.total_line) else None,
                "from_snapshot_id": None, "source": "nflverse", "retrieved_at": retrieved_at,
            })
    return pd.DataFrame(games), pd.DataFrame(closes)


def normalize_results(raw: pd.DataFrame, season: int, resolver: ids.AliasResolver) -> pd.DataFrame:
    d = raw[(raw.season == season) & raw.home_score.notna()].copy()
    retrieved_at = raw.attrs["retrieved_at"].isoformat()
    rows = []
    for _, r in d.iterrows():
        away = resolver.resolve("nflverse", alias=r.away_team)
        home = resolver.resolve("nflverse", alias=r.home_team)
        gid = ids.make_game_id(season, "NFL", config.NFL_GAME_TYPE_TO_SEASON_TYPE[r.game_type], int(r.week), away, home)
        rows.append({"game_id": gid, "away_score": int(r.away_score), "home_score": int(r.home_score),
                     "margin_home": int(r.home_score - r.away_score), "total": int(r.home_score + r.away_score),
                     "went_overtime": bool(r.overtime) if pd.notna(r.overtime) else None, "q_scores": None,
                     "attendance": None, "source": "nflverse", "retrieved_at": retrieved_at,
                     "effective_at": pd.Timestamp(r.gameday).isoformat()})
    return pd.DataFrame(rows)
