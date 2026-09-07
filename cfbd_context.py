"""
The Odds API adapter. One call per league per snapshot returns every upcoming game with all US books.
Cost = markets x regions credits = 3 per call with config defaults. Free plan: 500/month.

Payload shape (v4 /sports/{sport}/odds):
  [{id, sport_key, commence_time, home_team, away_team,
    bookmakers:[{key, title, last_update, markets:[{key:'h2h'|'spreads'|'totals', last_update,
                 outcomes:[{name, price, point?}]}]}]}]

Normalization:
  * spreads outcome with name == home_team gives point -> spread_home directly (already home-relative).
  * totals: 'Over' outcome point -> total.
  * Team names are full names ("Kansas City Chiefs") and resolve through team_aliases(provider='odds_api').
  * Game matching to our game_id: by (away_team_id, home_team_id, commence_time within 36h of our kickoff)
    against the games table. An unmatched event is logged, never guessed.
"""
from __future__ import annotations
from datetime import datetime

import pandas as pd

import config
from pipeline import ids
from providers.base import RequestManager, ProviderError

BASE = "https://api.the-odds-api.com/v4"


def call_cost() -> int:
    return len(config.ODDS_API_MARKETS.split(",")) * len(config.ODDS_API_REGIONS.split(","))


def fetch_odds(rm: RequestManager, league: str):
    if not config.ODDS_API_KEY:
        raise ProviderError("ODDS_API_KEY not set")
    sport = config.ODDS_API_SPORT_KEYS[league]
    return rm.get(f"{BASE}/sports/{sport}/odds",
                  params={"apiKey": config.ODDS_API_KEY, "regions": config.ODDS_API_REGIONS,
                          "markets": config.ODDS_API_MARKETS, "oddsFormat": "american", "dateFormat": "iso"},
                  cost=call_cost())


def _match_game(games: pd.DataFrame, away_id: str, home_id: str, commence: pd.Timestamp) -> str | None:
    cand = games[(games.away_team_id == away_id) & (games.home_team_id == home_id) & (games.status != "FINAL")]
    if cand.empty:
        return None
    if len(cand) == 1:
        return str(cand.game_id.iloc[0])
    cand = cand.assign(_dt=(pd.to_datetime(cand.kickoff_utc, utc=True) - commence).abs())
    cand = cand[cand._dt <= pd.Timedelta(hours=36)]
    return str(cand.sort_values("_dt").game_id.iloc[0]) if not cand.empty else None


def normalize_odds(payload: list[dict], league: str, games: pd.DataFrame, resolver: ids.AliasResolver,
                   retrieved_at: datetime, plan: str, first_snapshot_ids: set[str],
                   unmatched_events: list[dict]) -> pd.DataFrame:
    rows = []
    for ev in payload:
        try:
            home_id = resolver.resolve("odds_api", alias=ev["home_team"])
            away_id = resolver.resolve("odds_api", alias=ev["away_team"])
        except ids.UnmatchedAlias as e:
            unmatched_events.append({"event_id": ev.get("id"), "reason": str(e)})
            continue
        commence = pd.Timestamp(ev["commence_time"]).tz_convert("UTC")
        gid = _match_game(games, away_id, home_id, commence)
        if gid is None:
            unmatched_events.append({"event_id": ev.get("id"), "reason": f"no game for {away_id}@{home_id} near {commence}"})
            continue
        for bk in ev.get("bookmakers", []):
            book = bk["key"]
            rec = {"spread_home": None, "spread_home_price": None, "spread_away_price": None,
                   "ml_home": None, "ml_away": None, "total": None, "over_price": None, "under_price": None}
            for mk in bk.get("markets", []):
                outs = mk.get("outcomes", [])
                if mk["key"] == "spreads":
                    for o in outs:
                        if o["name"] == ev["home_team"]:
                            rec["spread_home"], rec["spread_home_price"] = o.get("point"), o.get("price")
                        elif o["name"] == ev["away_team"]:
                            rec["spread_away_price"] = o.get("price")
                elif mk["key"] == "h2h":
                    for o in outs:
                        if o["name"] == ev["home_team"]:
                            rec["ml_home"] = o.get("price")
                        elif o["name"] == ev["away_team"]:
                            rec["ml_away"] = o.get("price")
                elif mk["key"] == "totals":
                    for o in outs:
                        if o["name"] == "Over":
                            rec["total"], rec["over_price"] = o.get("point"), o.get("price")
                        elif o["name"] == "Under":
                            rec["under_price"] = o.get("price")
            sid = f"{gid}_{book}_{retrieved_at.strftime('%Y%m%dT%H%M%SZ')}"
            rows.append({
                "snapshot_id": sid, "game_id": gid, "retrieved_at": retrieved_at.isoformat(),
                "provider_updated_at": bk.get("last_update"), "book": book,
                **rec,
                "spread_ticket_pct_home": None, "spread_money_pct_home": None, "ml_ticket_pct_home": None,
                "ml_money_pct_home": None, "total_ticket_pct_over": None, "total_money_pct_over": None,
                "is_first_snapshot": f"{gid}|{book}" not in first_snapshot_ids,
                "provider_open_spread_home": None, "provider_open_total": None,
                "source": "odds_api", "plan": plan,
            })
    return pd.DataFrame(rows)
