"""
python -m pipeline.jobs.build_matchups --league NFL --season 2025 --weeks 10
python -m pipeline.jobs.build_matchups --league CFB                      # current + next week
Writes data/tables/analytics/matchup_edges/{league}/{season}/W{ww}.parquet
"""
from __future__ import annotations
import argparse
import sys

import config
from pipeline import matchup_engine, storage
from pipeline.log import JobRun

AN = config.TABLES / "analytics"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--weeks", nargs="*", default=None)
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    if a.season < config.MIN_ALLOWED_SEASON:
        sys.exit("season refused")
    games = storage.read_table(storage.games_path(a.league, a.season))
    if a.weeks and a.weeks[0] == "all":
        weeks = sorted(games[games.season_type == "REG"].week.unique().tolist())
    elif a.weeks:
        weeks = [int(w) for w in a.weeks]
    else:
        sched = games[games.status == "SCHEDULED"]
        cur = int(sched.week.min()) if not sched.empty else int(games.week.max())
        weeks = [cur, cur + 1]
    with JobRun(f"{a.league}_MATCHUPS", a.league, a.trigger) as job:
        for wk in weeks:
            df = matchup_engine.build_week(a.league, a.season, wk)
            if df.empty:
                print(f"{a.league} {a.season} W{wk}: no metrics for this week (run build_metrics first)"); continue
            storage.write_parquet(AN / "matchup_edges" / a.league / str(a.season) / f"W{wk:02d}.parquet", df)
            job.rows_written += len(df)
            un = df[df.is_unavailable].category.value_counts().to_dict()
            print(f"{a.league} {a.season} W{wk}: {df.game_id.nunique()} games x {df.category.nunique()} categories; unavailable: {un}")


if __name__ == "__main__":
    main()
