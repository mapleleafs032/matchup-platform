"""
python -m pipeline.jobs.build_market --league NFL            # current + next week
Writes data/tables/analytics/market_analysis/{league}/{season}/W{ww}.parquet and site/json/market/{game_id}.json
"""
from __future__ import annotations
import argparse
import json

import config
from pipeline import market_engine, storage
from pipeline.log import JobRun

AN = config.TABLES / "analytics"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--weeks", nargs="*", type=int)
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    games = storage.read_table(storage.games_path(a.league, a.season))
    if a.weeks:
        weeks = a.weeks
    else:
        sched = games[games.status == "SCHEDULED"]
        cur = int(sched.week.min()) if not sched.empty else int(games.week.max())
        weeks = [cur, cur + 1]
    out_dir = config.SITE_JSON / "market"; out_dir.mkdir(parents=True, exist_ok=True)
    with JobRun(f"{a.league}_MARKET", a.league, a.trigger) as job:
        for wk in weeks:
            df, payloads = market_engine.build_week(a.league, a.season, wk)
            if df.empty:
                print(f"{a.league} {a.season} W{wk}: no games"); continue
            storage.write_parquet(AN / "market_analysis" / a.league / str(a.season) / f"W{wk:02d}.parquet", df)
            for gid, pl in payloads.items():
                (out_dir / f"{gid}.json").write_text(json.dumps(pl, default=str))
            job.rows_written += len(df)
            av = df[df.available]
            print(f"{a.league} {a.season} W{wk}: {len(df)} games, {len(av)} with market history; steam flags={int(av.steam.notna().sum())}; "
                  f"key-number moves={int((av.key_numbers != '').sum())}; median snapshots/game={int(av.n_snapshots.median()) if len(av) else 0}")


if __name__ == "__main__":
    main()
