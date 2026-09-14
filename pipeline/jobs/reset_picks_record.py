"""
python -m pipeline.jobs.reset_picks_record --league BOTH --confirm

Clears the graded picks record and starts it again from zero.

Use when the record was built on plays that were never properly locked, so the numbers in it do not
describe decisions anyone could have acted on. The cleared rows are archived rather than deleted, so
the old record can still be inspected; it simply stops counting.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone

import config
from pipeline import storage
from pipeline.log import JobRun

MODEL = config.TABLES / "model"


def reset(league: str, season: int, confirm: bool, job: JobRun) -> int:
    path = MODEL / "picks_evaluation" / league / f"{season}.csv"
    cur = storage.read_table(path)
    if cur.empty:
        print(f"{league} {season}: record already empty")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = MODEL / "picks_evaluation_archive" / league / f"{season}_{stamp}.csv"
    if not confirm:
        print(f"{league} {season}: would archive {len(cur)} graded pick(s) and reset to 0-0 (pass --confirm to do it)")
        return 0
    archive.parent.mkdir(parents=True, exist_ok=True)
    cur.to_csv(archive, index=False)
    path.unlink()
    print(f"{league} {season}: archived {len(cur)} graded pick(s) to {archive.relative_to(config.ROOT)}; record reset to 0-0")
    return len(cur)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="BOTH", choices=["NFL", "CFB", "BOTH"])
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--confirm", action="store_true", help="actually reset; without it the job only reports")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    with JobRun("PICKS_RESET", a.league, a.trigger) as job:
        for lg in (["NFL", "CFB"] if a.league == "BOTH" else [a.league]):
            job.rows_written += reset(lg, a.season, a.confirm, job)


if __name__ == "__main__":
    main()
