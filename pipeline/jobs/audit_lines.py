"""
python -m pipeline.jobs.audit_lines --league BOTH
python -m pipeline.jobs.audit_lines --league CFB --fix-sign 2023      # only acts if the audit confirms a flip

Checks every season's closing lines against what actually happened.

Why: the CFB backtest went 34.5% against the closing line on 730 games in 2023. A model that wrong that
consistently is almost never a football result -- it is a data error, and because that season sits in
the training set it distorts every college number. The usual culprit is a spread stored with its sign
reversed, which turns every favourite into an underdog.

The test: in any honest season the closing line predicts the result. The market margin (the negative of
the home spread) should correlate POSITIVELY with the actual margin, typically 0.35-0.60. A season where
that correlation is negative has its signs backwards. A season near zero has teams mismatched.

Correction is deliberately hard to trigger: --fix-sign only acts when the audit itself shows a clear
reversal (correlation below -0.2), the original file is kept, and the change is logged.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from pipeline import storage
from pipeline.log import JobRun

FLIP_THRESHOLD = -0.20      # below this the signs are reversed, not merely noisy
HEALTHY_FLOOR = 0.25        # a normal season sits comfortably above this


def season_audit(league: str, season: int) -> dict | None:
    cl = storage.read_table(config.TABLES / "market" / "closing_lines" / league / f"{season}.parquet")
    res = storage.read_table(config.TABLES / "results" / league / f"{season}.csv")
    if cl.empty or res.empty:
        return None
    cl = cl.dropna(subset=["spread_home"]).drop_duplicates("game_id")
    df = cl.merge(res[["game_id", "margin_home"]], on="game_id", how="inner")
    if len(df) < 20:
        return {"season": season, "n": len(df), "verdict": "too few games to judge"}
    market = -df.spread_home.astype(float)
    actual = df.margin_home.astype(float)
    corr = float(np.corrcoef(market, actual)[0, 1])
    fav_home = float((df.spread_home < 0).mean())
    # did the favourite win outright? in a normal season, roughly two games in three
    decided = df[(df.spread_home != 0) & (df.margin_home != 0)]
    fav_won = float(((decided.spread_home < 0) == (decided.margin_home > 0)).mean()) if len(decided) else float("nan")
    if corr < FLIP_THRESHOLD:
        verdict = "SIGNS REVERSED"
    elif corr < HEALTHY_FLOOR:
        verdict = "SUSPECT: teams may be mismatched"
    else:
        verdict = "healthy"
    return {"season": season, "n": int(len(df)), "corr_market_vs_result": round(corr, 3),
            "home_favoured_share": round(fav_home, 3), "favourite_won_share": round(fav_won, 3), "verdict": verdict}


def fix_sign(league: str, season: int) -> bool:
    a = season_audit(league, season)
    if not a or a.get("verdict") != "SIGNS REVERSED":
        print(f"{league} {season}: not correcting — audit verdict is '{a and a.get('verdict')}', "
              f"which is not a confirmed reversal. Nothing changed.")
        return False
    path = config.TABLES / "market" / "closing_lines" / league / f"{season}.parquet"
    cl = storage.read_table(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{season}.before_sign_fix_{stamp}.parquet")
    cl.to_parquet(backup, index=False)
    cl["spread_home"] = -cl.spread_home
    cl["sign_corrected_at"] = datetime.now(timezone.utc).isoformat()
    storage.write_parquet(path, cl)
    after = season_audit(league, season)
    print(f"{league} {season}: spreads reversed. Correlation {a['corr_market_vs_result']} -> "
          f"{after['corr_market_vs_result']}. Original kept at {backup.relative_to(config.ROOT)}.")
    print("Re-run the backtest: the training set has changed.")
    return True


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="BOTH", choices=["NFL", "CFB", "BOTH"])
    p.add_argument("--fix-sign", type=int, help="reverse this season's spreads, only if the audit confirms a reversal")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    leagues = ["NFL", "CFB"] if a.league == "BOTH" else [a.league]
    with JobRun("AUDIT_LINES", a.league, a.trigger) as job:
        if a.fix_sign:
            for lg in leagues:
                fix_sign(lg, a.fix_sign)
            return
        print(f"{'league':6} {'season':>6} {'games':>6} {'mkt~result':>10} {'home fav':>8} {'fav won':>8}  verdict")
        for lg in leagues:
            for season in config.BACKTEST_SEASONS:
                r = season_audit(lg, season)
                if r is None:
                    continue
                if "corr_market_vs_result" not in r:
                    print(f"{lg:6} {season:>6} {r['n']:>6}  {r['verdict']}")
                    continue
                print(f"{lg:6} {season:>6} {r['n']:>6} {r['corr_market_vs_result']:>10} "
                      f"{r['home_favoured_share']:>8} {r['favourite_won_share']:>8}  {r['verdict']}")
                job.rows_written += 1
        print("\nA healthy season correlates 0.35-0.60 with results, with favourites winning about two games in three.")
        print("A negative correlation means the signs are stored backwards; use --fix-sign SEASON to correct it.")


if __name__ == "__main__":
    main()
