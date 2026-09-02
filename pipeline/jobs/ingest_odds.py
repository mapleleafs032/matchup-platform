"""
python -m pipeline.jobs.ingest_odds --league NFL
python -m pipeline.jobs.ingest_odds --league CFB --week 1

Writes one append-only row per (game, book) to data/tables/market/snapshots/{league}/{season}/W{ww}.csv.
Routing: config.ODDS_ROUTING[league] -> 'odds_api' | 'cfbd'. Switching to paid is a config change only.
Automatic degradation: when the provider's remaining monthly credits drop below
config.BUDGET_DEGRADE_AT_PCT_REMAINING, the job skips unless --force.
"""
from __future__ import annotations
import argparse
import sys

import pandas as pd

import config
from pipeline import ids, storage, validate
from pipeline.log import JobRun, ValidationLog
from providers import cfbd, odds_api
from providers.base import RequestManager, BudgetExceeded

SNAP_DIR = config.TABLES / "market" / "snapshots"


def snapshot_path(league: str, season: int, week: int):
    return SNAP_DIR / league / str(season) / f"W{week:02d}.csv"


def _first_snapshot_ids(league: str, season: int, week: int) -> set[str]:
    cur = storage.read_table(snapshot_path(league, season, week))
    if cur.empty:
        return set()
    return set((cur.game_id + "|" + cur.book).tolist())


def _current_week(games: pd.DataFrame) -> int:
    upcoming = games[(games.status == "SCHEDULED") & games.kickoff_utc.notna()]
    if upcoming.empty:
        return int(games.week.max())
    now = pd.Timestamp.now(tz="UTC")
    soon = upcoming.assign(k=pd.to_datetime(upcoming.kickoff_utc, utc=True))
    soon = soon[soon.k >= now - pd.Timedelta(hours=6)]
    return int(soon.sort_values("k").week.iloc[0]) if not soon.empty else int(upcoming.week.min())


def run(league: str, season: int, week: int | None, force: bool, job: JobRun) -> None:
    provider = config.ODDS_ROUTING[league]
    games = storage.read_table(storage.games_path(league, season))
    if games.empty:
        raise RuntimeError(f"no games table for {league} {season}; run ingest_schedules first")
    week = week or _current_week(games)
    rm = RequestManager(provider, job.job_run_id)
    remaining = rm.remaining_monthly()
    limit = config.API_BUDGET[provider]["monthly"]
    if not force and remaining < limit * config.BUDGET_DEGRADE_AT_PCT_REMAINING:
        job.status = "SKIPPED"; job.message = f"{provider} remaining {remaining}/{limit} below degrade threshold"
        return
    resolver = ids.AliasResolver.load()
    first_ids = _first_snapshot_ids(league, season, week)
    vlog = ValidationLog(job.job_run_id, "market_snapshots")
    unmatched: list[dict] = []
    rejects: list[dict] = []

    if provider == "odds_api":
        res = odds_api.fetch_odds(rm, league)
        snaps = odds_api.normalize_odds(res.payload, league, games, resolver, res.retrieved_at, config.ODDS_PLAN, first_ids, unmatched)
    elif provider == "cfbd":
        wk_games = games[games.week == week]
        gid_by_cfbd = {}
        for _, g in wk_games.iterrows():
            try:
                gid_by_cfbd[int(str(g.provider_game_ids).split(":")[1].strip("}"))] = g.game_id
            except (IndexError, ValueError):
                pass
        res = cfbd.fetch_lines(rm, season, week)
        snaps = cfbd.normalize_lines(res.payload, season, gid_by_cfbd, res.retrieved_at, config.ODDS_PLAN, rejects, first_ids)
    else:
        raise ValueError(provider)

    for u in unmatched:
        vlog.warn("EVENT_UNMATCHED", u.get("event_id"), "event", u["reason"], "mapped game")
    for rj in rejects:
        vlog.reject(rj["rule"], rj["key"], "spread_home", rj["observed"], "sign agrees with formattedSpread")
    if not snaps.empty:
        snaps = validate.validate_market(snaps, set(games.game_id), vlog)
    vlog.flush()

    written = 0
    if not snaps.empty:
        # snapshots may span weeks (odds_api returns all upcoming); partition by each game's week
        wk_of = games.set_index("game_id").week
        snaps = snaps.assign(_wk=snaps.game_id.map(wk_of))
        for wk, part in snaps.groupby("_wk"):
            written += storage.append_csv(snapshot_path(league, season, int(wk)), part.drop(columns="_wk"),
                                          key_cols=["snapshot_id"], on_duplicate="skip")
    job.rows_written = written
    job.api_calls = rm.calls_this_run
    if vlog.rejects or unmatched:
        job.status = "PARTIAL"
        job.message += f" rejects={vlog.rejects} unmatched={len(unmatched)};"
    print(f"{league} odds via {provider}: {written} rows, week {week}, provider remaining={res.remaining_reported}, "
          f"our monthly remaining={rm.remaining_monthly()}")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--week", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    with JobRun(f"{a.league}_ODDS", a.league, a.trigger) as job:
        run(a.league, a.season, a.week, a.force, job)


if __name__ == "__main__":
    main()
