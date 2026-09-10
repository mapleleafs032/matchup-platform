"""
python -m pipeline.jobs.build_picks --league BOTH
Writes data/tables/model/picks/{league}/{season}/W##.parquet (rebuilt each run for upcoming games)
and grades finished picks into data/tables/model/picks_evaluation/{league}/{season}.csv (APPEND-ONLY).
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone

import pandas as pd

import config
from pipeline import picks_engine, storage
from pipeline.log import JobRun

MODEL = config.TABLES / "model"


def grade(league: str, season: int, job: JobRun) -> int:
    """Grade any stored pick whose game has finished. Append-only; a pick is graded once."""
    res = storage.read_table(config.TABLES / "results" / league / f"{season}.csv")
    if res.empty:
        return 0
    res = res.set_index("game_id")
    done = storage.read_table(MODEL / "picks_evaluation" / league / f"{season}.csv")
    graded = set(done.pick_id) if not done.empty else set()
    rows = []
    for p in sorted((MODEL / "picks" / league / str(season)).glob("W*.parquet")) if (MODEL / "picks" / league / str(season)).exists() else []:
        picks = pd.read_parquet(p)
        for _, k in picks.iterrows():
            if k.pick_id in graded or k.game_id not in res.index:
                continue
            r = res.loc[k.game_id]
            outcome = None
            if k.market == "SPREAD":
                margin_for_side = r.margin_home if k.side_is_home else -r.margin_home
                cover = margin_for_side + k.line
                outcome = "PUSH" if cover == 0 else ("WIN" if cover > 0 else "LOSS")
            elif k.market == "TOTAL":
                d = r.total - k.line
                outcome = "PUSH" if d == 0 else ("WIN" if (d > 0) == (k.side == "Over") else "LOSS")
            elif k.market == "MONEYLINE":
                won = (r.margin_home > 0) if k.side_is_home else (r.margin_home < 0)
                outcome = "PUSH" if r.margin_home == 0 else ("WIN" if won else "LOSS")
            profit = 0.0
            if outcome == "WIN":
                profit = picks_engine.american_profit(k.price)
            elif outcome == "LOSS":
                profit = -1.0
            rows.append({"pick_id": k.pick_id, "game_id": k.game_id, "league": league, "season": season, "week": int(k.week),
                         "market": k.market, "side": k.side, "line": k.line, "price": k.price, "tier": k.tier, "score": k.score,
                         "edge_points": k.edge_points, "signals": k.signals, "result": outcome, "profit_units": round(profit, 3),
                         "actual_margin_home": int(r.margin_home), "actual_total": int(r.total),
                         "graded_at": datetime.now(timezone.utc).isoformat()})
            graded.add(k.pick_id)
    if rows:
        n = storage.append_csv(MODEL / "picks_evaluation" / league / f"{season}.csv", pd.DataFrame(rows), ["pick_id"], on_duplicate="skip")
        print(f"{league}: graded {n} picks")
        return n
    return 0


def run(league: str, season: int, weeks: list[int] | None, job: JobRun) -> None:
    games = storage.read_table(storage.games_path(league, season))
    if games.empty:
        job.status = "SKIPPED"; job.message = f"no games for {league}"; return
    if not weeks:
        sched = games[games.status == "SCHEDULED"]
        cur = int(sched.week.min()) if not sched.empty else int(games.week.max())
        weeks = [cur, cur + 1]
    total = 0
    for wk in weeks:
        picks, rejected, calib = picks_engine.build_week(league, season, wk)
        if picks.empty and rejected.empty:
            print(f"{league} {season} W{wk}: no candidates clear the minimum edge"); continue
        if not rejected.empty:
            storage.write_parquet(MODEL / "picks_rejected" / league / str(season) / f"W{wk:02d}.parquet", rejected)
            counts: dict = {}
            for r in rejected.veto_reasons:
                for reason in str(r).split(" | "):
                    key = reason.split(":")[0].split(",")[0][:52]
                    counts[key] = counts.get(key, 0) + 1
            print(f"{league} {season} W{wk}: {len(rejected)} candidates filtered out by the gates:")
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"      {v:3d}  {k}")
        if picks.empty:
            print(f"{league} {season} W{wk}: nothing survived the gates"); continue
        picks = picks.head(config.PICK_MAX_PER_WEEK)
        storage.write_parquet(MODEL / "picks" / league / str(season) / f"W{wk:02d}.parquet", picks)
        total += len(picks)
        by_tier = picks.tier.value_counts().to_dict()
        print(f"{league} {season} W{wk}: {len(picks)} plays {by_tier}")
        if calib.get("bands"):
            print(f"    measured relationship between score and winning ({league}):")
            for b in calib["bands"]:
                hi = f"-{b['hi']}" if b["hi"] else "+"
                verdict = "clears break-even" if b["significant"] else ("above, within noise" if b["beats_break_even"] else "below break-even")
                print(f"      score {b['lo']}{hi}: n={b['n']:4d} hit={b['hit_rate']*100:5.1f}%  95% [{b['ci_low']*100:.0f}-{b['ci_high']*100:.0f}]  {verdict}")
        for t in ("A+", "A", "B"):
            c = (calib.get("tiers") or {}).get(t)
            if c and c.get("hit_rate") is not None:
                print(f"    tier {t} = score {c['range'][0]}{'-' + str(c['range'][1]) if c['range'][1] else '+'}: {c['hit_rate']*100:.1f}% on {c['n']} graded")
    job.rows_written = total + grade(league, season, job)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="BOTH", choices=["NFL", "CFB", "BOTH"])
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--weeks", nargs="*", type=int)
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    with JobRun("PICKS", a.league, a.trigger) as job:
        for lg in (["NFL", "CFB"] if a.league == "BOTH" else [a.league]):
            run(lg, a.season, a.weeks, job)


if __name__ == "__main__":
    main()
