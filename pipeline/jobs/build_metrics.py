"""
python -m pipeline.jobs.build_metrics --league NFL --season 2025 --weeks 10
python -m pipeline.jobs.build_metrics --league CFB                      # current + next week
python -m pipeline.jobs.build_metrics --league NFL --season 2025 --weeks all   # every week (backtest inputs)

Writes:
  data/tables/ref/metric_definitions.csv
  data/tables/analytics/team_metrics_asof/{league}/{season}/W{ww}.parquet
  data/tables/analytics/team_ratings/{league}/{season}.parquet   (merged by as_of_week)
"""
from __future__ import annotations
import argparse
import sys

import pandas as pd

import config
from pipeline import asof, storage
from pipeline.log import JobRun
from pipeline.metric_registry import write_registry

AN = config.TABLES / "analytics"


def _merge_ratings(league: str, season: int, rat: pd.DataFrame):
    if rat.empty:
        return
    path = AN / "team_ratings" / league / f"{season}.parquet"
    cur = storage.read_table(path)
    if not cur.empty:
        cur = cur[cur.as_of_week != rat.as_of_week.iloc[0]]
        rat = pd.concat([cur, rat], ignore_index=True)
    storage.write_parquet(path, rat.sort_values(["as_of_week", "team_id"]))


def run(league: str, season: int, weeks: list[int], job: JobRun):
    n_defs = write_registry()
    games = storage.read_table(storage.games_path(league, season))
    if games.empty:
        job.status = "SKIPPED"; job.message = "no games"; return
    tg = asof.load_team_games(league, season)
    prior = asof.prior_season_values(league, season - 1) if season - 1 >= config.MIN_ALLOWED_SEASON else None
    if prior is None:
        print(f"{league} {season}: no prior-season data (first backfill season) -> early-season prior disabled")
    for wk in weeks:
        m, rat = asof.build_week(league, season, wk, tg=tg, prior_season_values=prior)
        if m.empty:
            print(f"{league} {season} W{wk}: no games"); continue
        storage.write_parquet(AN / "team_metrics_asof" / league / str(season) / f"W{wk:02d}.parquet", m)
        _merge_ratings(league, season, rat)
        job.rows_written += len(m) + len(rat)
        lo = int((m.quality_flags.str.contains("LOW_SAMPLE")).sum())
        print(f"{league} {season} W{wk}: {len(m)} metric rows for {m.team_id.nunique()} teams, {len(rat)} ratings, "
              f"prior_w={asof.prior_weight(league, wk) if prior else 0}, low-sample rows={lo}")
    print(f"metric registry: {n_defs} definitions")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--weeks", nargs="*", default=None, help="week numbers, or 'all'")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    if a.season < config.MIN_ALLOWED_SEASON:
        sys.exit("season refused")
    games = storage.read_table(storage.games_path(a.league, a.season))
    if a.weeks and a.weeks[0] == "all":
        weeks = sorted(games[games.season_type == "REG"].week.unique().tolist()) if not games.empty else []
    elif a.weeks:
        weeks = [int(w) for w in a.weeks]
    else:
        sched = games[(games.status == "SCHEDULED")] if not games.empty else games
        cur = int(sched.week.min()) if not sched.empty else int(games.week.max())
        weeks = [cur, cur + 1]
    with JobRun(f"{a.league}_METRICS", a.league, a.trigger) as job:
        run(a.league, a.season, weeks, job)


if __name__ == "__main__":
    main()
