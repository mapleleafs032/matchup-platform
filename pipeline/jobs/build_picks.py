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


def _implied(ml) -> float | None:
    if ml is None or pd.isna(ml):
        return None
    ml = float(ml)
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def closing_number(league: str, season: int, week: int, game_id: str, kickoff, cache: dict) -> dict | None:
    """The last number posted before kickoff: the market's final, sharpest estimate."""
    key = (league, season, week)
    if key not in cache:
        cache[key] = storage.read_table(config.TABLES / "market" / "snapshots" / league / str(season) / f"W{week:02d}.csv")
    snaps = cache[key]
    if snaps.empty:
        return None
    g = snaps[snaps.game_id == game_id].copy()
    if g.empty:
        return None
    g["retrieved_at"] = pd.to_datetime(g.retrieved_at, utc=True, errors="coerce")
    if kickoff is not None and not pd.isna(kickoff):
        g = g[g.retrieved_at < pd.Timestamp(kickoff)]
    if g.empty:
        return None
    last = g.sort_values("retrieved_at").iloc[-1]
    return {"spread_home": last.get("spread_home"), "total": last.get("total"),
            "ml_home": last.get("ml_home"), "ml_away": last.get("ml_away"), "at": last.retrieved_at.isoformat()}


def closing_line_value(k, close: dict | None) -> dict:
    """
    How the number we took compares with where the market finished, from our side's point of view.
    Positive means we beat the close. It is the fastest honest measure of skill: win-loss needs
    hundreds of plays to mean anything, while a model that keeps beating the closing line shows it
    within weeks, because the close is the sharpest price there is.
    """
    out = {"close_line": None, "clv_points": None, "clv_prob": None, "beat_close": None}
    if not close:
        return out
    if k.market == "SPREAD" and close.get("spread_home") is not None and not pd.isna(close["spread_home"]):
        close_side = float(close["spread_home"]) if k.side_is_home else -float(close["spread_home"])
        clv = float(k.line) - close_side               # more points taken, or fewer laid, than the close
        out.update({"close_line": close_side, "clv_points": round(clv, 2)})
    elif k.market == "TOTAL" and close.get("total") is not None and not pd.isna(close["total"]):
        over = str(k.side).lower().startswith("o")
        clv = (float(close["total"]) - float(k.line)) if over else (float(k.line) - float(close["total"]))
        out.update({"close_line": float(close["total"]), "clv_points": round(clv, 2)})
    elif k.market == "MONEYLINE":
        cml = close.get("ml_home") if k.side_is_home else close.get("ml_away")
        taken, closed = _implied(k.price), _implied(cml)
        if taken is not None and closed is not None:
            out.update({"close_line": float(cml), "clv_prob": round(closed - taken, 4)})
    v = out["clv_points"] if out["clv_points"] is not None else out["clv_prob"]
    out["beat_close"] = None if v is None else bool(v > 0)
    return out


def grade(league: str, season: int, job: JobRun) -> int:
    """
    Grade every stored pick whose game has finished, in any week, however long ago.

    This is deliberately a full re-scan rather than a look at the current week: a pick made days before
    kickoff must still be graded even if no run happened at the moment the game ended. Grading is
    append-only and keyed on pick_id, so re-scanning costs nothing and can never double-count.
    """
    res = storage.read_table(config.TABLES / "results" / league / f"{season}.csv")
    if res.empty:
        print(f"{league}: no results table yet, nothing to grade")
        return 0
    res = res.set_index("game_id")
    done = storage.read_table(MODEL / "picks_evaluation" / league / f"{season}.csv")
    graded = set(done.pick_id) if not done.empty else set()
    rows, scanned, waiting = [], [], 0
    close_cache: dict = {}
    pick_dir = MODEL / "picks" / league / str(season)
    for p in sorted(pick_dir.glob("W*.parquet")) if pick_dir.exists() else []:
        picks = pd.read_parquet(p)
        scanned.extend(picks.pick_id.tolist())
        for _, k in picks.iterrows():
            if k.pick_id in graded:
                continue
            if k.game_id not in res.index:
                waiting += 1
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
            kick = k.get("kickoff_utc") if hasattr(k, "get") else None
            clv = closing_line_value(k, closing_number(league, season, int(k.week), k.game_id, kick, close_cache))
            made = k.get("built_at") if hasattr(k, "get") else None
            try:
                hrs_before = round((pd.Timestamp(kick) - pd.Timestamp(made)).total_seconds() / 3600, 1) if kick and made else None
            except (ValueError, TypeError):
                hrs_before = None
            rows.append({**clv, "hours_before_kick": hrs_before,
                         "pick_id": k.pick_id, "game_id": k.game_id, "league": league, "season": season, "week": int(k.week),
                         "market": k.market, "side": k.side, "line": k.line, "price": k.price, "tier": k.tier, "score": k.score,
                         "edge_points": k.edge_points, "signals": k.signals, "result": outcome, "profit_units": round(profit, 3),
                         "actual_margin_home": int(r.margin_home), "actual_total": int(r.total),
                         "graded_at": datetime.now(timezone.utc).isoformat()})
            graded.add(k.pick_id)
    if rows:
        n = storage.append_csv(MODEL / "picks_evaluation" / league / f"{season}.csv", pd.DataFrame(rows), ["pick_id"], on_duplicate="skip")
        by_wk = {}
        for r in rows:
            by_wk[r["week"]] = by_wk.get(r["week"], 0) + 1
        print(f"{league}: graded {n} picks across weeks {sorted(by_wk)} {by_wk}")
        return n
    print(f"{league}: {len(scanned)} stored picks scanned, {len(graded)} already graded, {waiting} still awaiting a result")
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
        rejected, leans = picks_engine.split_leans(rejected)
        storage.write_parquet(MODEL / "picks_leans" / league / str(season) / f"W{wk:02d}.parquet",
                              leans if not leans.empty else picks_engine.empty_picks_frame())
        if not leans.empty:
            print(f"{league} {season} W{wk}: {len(leans)} lean(s) — model edge with no market confirmation, not ranked and not graded")
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
        if not picks.empty and "rlm_earlier_only" in picks.columns:
            reb = int(picks.rlm_earlier_only.fillna(False).astype(bool).sum())
            if reb:
                print(f"      ({reb} of these showed reverse movement earlier in the week but have since rebounded, so they were kept)")
        # Always write, even when nothing qualifies. Skipping the write leaves the previous run's file in
        # place, so a play that has since lost its market support keeps showing as a ranked pick while
        # simultaneously appearing in the filtered-out list. The current state must replace the old one.
        picks = picks.head(config.PICK_MAX_PER_WEEK) if not picks.empty else picks
        storage.write_parquet(MODEL / "picks" / league / str(season) / f"W{wk:02d}.parquet",
                              picks if not picks.empty else picks_engine.empty_picks_frame())
        if picks.empty:
            print(f"{league} {season} W{wk}: nothing survived the gates (previous picks cleared)"); continue
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
    p.add_argument("--grade-only", action="store_true",
                   help="skip generating new plays; only grade stored picks whose games have finished")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    with JobRun("PICKS", a.league, a.trigger) as job:
        for lg in (["NFL", "CFB"] if a.league == "BOTH" else [a.league]):
            if a.grade_only:
                job.rows_written += grade(lg, a.season, job)
            else:
                run(lg, a.season, a.weeks, job)


if __name__ == "__main__":
    main()
