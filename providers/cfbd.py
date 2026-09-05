"""
CollegeFootballData adapter (CFB). REST v2, Bearer key. Free tier: 1,000 calls/month.

Endpoints used in this phase (1 call each, whole-week payloads):
  GET /teams/fbs?year=              seed teams + aliases (once per season)
  GET /games?year&week&seasonType   schedule for one week, all games incl. FCS opponents
  GET /lines?year&week&seasonType   lines for one week, multiple books, includes spreadOpen/overUnderOpen

Field shapes below follow the v2 documentation. Every field access is defensive: a missing field
is NULL, never a default value. The first live pull runs verify_first_pull() and the checklist in
the deployment notes must be completed before the job is scheduled.

Spread sign: CFBD's `spread` is home-relative (negative = home favored), the same as our convention.
Because that convention is undocumented in the payload itself, every line row is cross-checked
against `formattedSpread` (e.g. "Nebraska -7.5"); any mismatch is REJECTED, not corrected.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone

import pandas as pd

import config
from pipeline import ids
from providers.base import RequestManager, ProviderError

BASE = "https://api.collegefootballdata.com"


def _headers() -> dict:
    if not config.CFBD_API_KEY:
        raise ProviderError("CFBD_API_KEY not set")
    return {"Authorization": f"Bearer {config.CFBD_API_KEY}", "Accept": "application/json"}


def _g(d: dict, *keys, default=None):
    """Nested getter that treats missing as None, never as a value."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# ---- teams ---------------------------------------------------------------------
def fetch_teams_fbs(rm: RequestManager, season: int):
    return rm.get(f"{BASE}/teams/fbs", params={"year": season}, headers=_headers())


def normalize_teams(payload: list[dict], retrieved_at: datetime) -> tuple[pd.DataFrame, list[dict], list[str]]:
    """Returns (teams, alias rows, collision warnings)."""
    teams, aliases, seen_abbr, warnings = [], [], {}, []
    for t in payload:
        abbr = _g(t, "abbreviation") or _g(t, "school")
        tid = ids.make_team_id("CFB", abbr)
        if tid in seen_abbr and seen_abbr[tid] != _g(t, "id"):
            warnings.append(f"abbreviation collision {tid}: cfbd ids {seen_abbr[tid]} and {_g(t, 'id')} — resolve manually in team_aliases.csv")
            continue
        seen_abbr[tid] = _g(t, "id")
        logos = _g(t, "logos") or []
        loc = _g(t, "location") or {}
        teams.append({
            "team_id": tid, "league": "CFB", "abbr": tid.split("_", 1)[1], "school_or_city": _g(t, "school"),
            "mascot": _g(t, "mascot"), "display_name": f"{_g(t, 'school')} {_g(t, 'mascot') or ''}".strip(),
            "conference": _g(t, "conference"), "division": _g(t, "division"), "classification": _g(t, "classification"),
            "home_venue_id": f"V_CFBD_{loc.get('id')}" if loc.get("id") else None,
            "primary_color": _g(t, "color"), "secondary_color": _g(t, "alternateColor"),
            "logo_url": logos[0] if logos else None,
            "source": "cfbd", "retrieved_at": retrieved_at.isoformat(),
        })
        for alias in {_g(t, "school"), f"{_g(t, 'school')} {_g(t, 'mascot')}", _g(t, "abbreviation")}:
            if alias:
                aliases.append({"provider": "cfbd", "alias": alias, "provider_id": str(_g(t, "id")), "team_id": tid,
                                "season_from": None, "season_to": None})
        for alt in (_g(t, "alternateNames") or []):
            aliases.append({"provider": "cfbd", "alias": alt, "provider_id": str(_g(t, "id")), "team_id": tid,
                            "season_from": None, "season_to": None})
    return pd.DataFrame(teams), aliases, warnings


# ---- games -----------------------------------------------------------------------
def _team_or_fcs(resolver: ids.AliasResolver, name: str, cfbd_id, classification: str | None) -> tuple[str, bool]:
    """FBS teams must resolve. Any team not labeled 'fbs' (FCS, D2, D3, NAIA, or unlabeled) is a
    non-FBS opponent: it gets a synthetic tagged id and the game is flagged is_fcs_game."""
    try:
        return resolver.resolve("cfbd", alias=name, provider_id=cfbd_id), False
    except ids.UnmatchedAlias:
        if (classification or "").lower() != "fbs":
            resolver.unmatched.pop()  # not an error: non-FBS opponent by design
            return ids.make_team_id("CFB", f"FCS{cfbd_id}"), True
        raise


def normalize_games(payload: list[dict], season: int, resolver: ids.AliasResolver, retrieved_at: datetime) -> pd.DataFrame:
    rows = []
    for g in payload:
        st = config.CFBD_SEASON_TYPE.get(_g(g, "seasonType"), None)
        if st is None:
            continue
        home, home_fcs = _team_or_fcs(resolver, _g(g, "homeTeam"), _g(g, "homeId"), _g(g, "homeClassification"))
        away, away_fcs = _team_or_fcs(resolver, _g(g, "awayTeam"), _g(g, "awayId"), _g(g, "awayClassification"))
        if home_fcs and away_fcs:
            continue  # FCS vs FCS never enters the database
        gid = ids.make_game_id(season, "CFB", st, int(_g(g, "week")), away, home)
        start = _g(g, "startDate")
        tba = bool(_g(g, "startTimeTBD", default=False))
        kick = pd.Timestamp(start).tz_convert("UTC") if start and not tba else (pd.Timestamp(start).tz_convert("UTC") if start else None)
        completed = bool(_g(g, "completed", default=False))
        rows.append({
            "game_id": gid, "league": "CFB", "season": season, "season_type": st, "week": int(_g(g, "week")),
            "away_team_id": away, "home_team_id": home,
            "kickoff_utc": kick, "kickoff_is_tba": tba, "kickoff_source": "cfbd",
            "venue_id": f"V_CFBD_{_g(g, 'venueId')}" if _g(g, "venueId") else None,
            "venue_name": _g(g, "venue"), "venue_roof": None,
            "neutral_site": bool(_g(g, "neutralSite", default=False)),
            "conference_game": _g(g, "conferenceGame"),
            "is_fcs_game": bool(home_fcs or away_fcs), "tv_network": None, "tv_source": None,
            "status": "FINAL" if completed else "SCHEDULED", "locked_at": None,
            "provider_game_ids": '{"cfbd":%s}' % _g(g, "id"),
            "source": "cfbd", "retrieved_at": retrieved_at.isoformat(), "effective_at": retrieved_at.isoformat(),
        })
    return pd.DataFrame(rows)


# ---- lines -----------------------------------------------------------------------
def fetch_lines(rm: RequestManager, season: int, week: int, season_type: str = "regular"):
    return rm.get(f"{BASE}/lines", params={"year": season, "week": week, "seasonType": season_type}, headers=_headers())


_FMT_RE = re.compile(r"^(.*?)\s([+-]\d+(?:\.\d)?)$")


def _sign_check(formatted: str | None, spread: float | None, home_name: str, away_name: str) -> bool | None:
    """True if formattedSpread agrees with home-relative sign; None if cannot evaluate."""
    if not formatted or spread is None:
        return None
    m = _FMT_RE.match(formatted.strip())
    if not m:
        return None
    fav, num = m.group(1).strip(), float(m.group(2))
    if fav == home_name:
        return abs(num - spread) < 1e-9 and spread <= 0
    if fav == away_name:
        return abs(-num - spread) < 1e-9 and spread >= 0
    return None


def normalize_lines(payload: list[dict], season: int, game_ids_by_cfbd: dict[int, str], retrieved_at: datetime,
                    plan: str, rejects: list[dict], first_snapshot_ids: set[str]) -> pd.DataFrame:
    rows = []
    for g in payload:
        gid = game_ids_by_cfbd.get(_g(g, "id"))
        if gid is None:
            continue  # FCS-vs-FCS or unmapped: skipped, games job already logged it
        for ln in (_g(g, "lines") or []):
            book = (_g(ln, "provider") or "unknown").lower().replace(" ", "")
            spread = _g(ln, "spread")
            chk = _sign_check(_g(ln, "formattedSpread"), spread, _g(g, "homeTeam"), _g(g, "awayTeam"))
            if chk is False:
                rejects.append({"key": f"{gid}_{book}", "rule": "SPREAD_SIGN_MISMATCH",
                                "observed": f"spread={spread} formatted={_g(ln, 'formattedSpread')}"})
                continue
            sid = f"{gid}_{book}_{retrieved_at.strftime('%Y%m%dT%H%M%SZ')}"
            rows.append({
                "snapshot_id": sid, "game_id": gid, "retrieved_at": retrieved_at.isoformat(),
                "provider_updated_at": None, "book": book,
                "spread_home": float(spread) if spread is not None else None,
                "spread_home_price": None, "spread_away_price": None,
                "ml_home": int(_g(ln, "homeMoneyline")) if _g(ln, "homeMoneyline") is not None else None,
                "ml_away": int(_g(ln, "awayMoneyline")) if _g(ln, "awayMoneyline") is not None else None,
                "total": float(_g(ln, "overUnder")) if _g(ln, "overUnder") is not None else None,
                "over_price": None, "under_price": None,
                "spread_ticket_pct_home": None, "spread_money_pct_home": None, "ml_ticket_pct_home": None,
                "ml_money_pct_home": None, "total_ticket_pct_over": None, "total_money_pct_over": None,
                "is_first_snapshot": f"{gid}|{book}" not in first_snapshot_ids,
                "provider_open_spread_home": float(_g(ln, "spreadOpen")) if _g(ln, "spreadOpen") is not None else None,
                "provider_open_total": float(_g(ln, "overUnderOpen")) if _g(ln, "overUnderOpen") is not None else None,
                "source": "cfbd", "plan": plan,
            })
    return pd.DataFrame(rows)


def verify_first_pull(games_payload: list[dict], lines_payload: list[dict]) -> list[str]:
    """Run once with a live key. Returns human-readable findings for the deployment checklist."""
    out = []
    if games_payload:
        g = games_payload[0]
        for f in ("id", "season", "week", "seasonType", "startDate", "startTimeTBD", "completed", "neutralSite",
                  "conferenceGame", "venueId", "homeId", "homeTeam", "homeClassification", "awayId", "awayTeam",
                  "awayClassification", "homePoints", "awayPoints"):
            out.append(f"games.{f}: {'present' if f in g else 'MISSING'}")
    if lines_payload:
        with_lines = [x for x in lines_payload if x.get("lines")]
        out.append(f"lines: {len(lines_payload)} games, {len(with_lines)} with >=1 book")
        if with_lines:
            ln = with_lines[0]["lines"][0]
            for f in ("provider", "spread", "formattedSpread", "spreadOpen", "overUnder", "overUnderOpen",
                      "homeMoneyline", "awayMoneyline"):
                out.append(f"lines.{f}: {'present' if f in ln else 'MISSING'}")
            books = sorted({l.get("provider") for x in with_lines for l in x["lines"]})
            out.append(f"books seen: {books}")
    return out
