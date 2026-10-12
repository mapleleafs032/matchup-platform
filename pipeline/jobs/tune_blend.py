"""
python -m pipeline.jobs.tune_blend --league BOTH              # report only
python -m pipeline.jobs.tune_blend --league BOTH --apply      # adopt the winner if it clears the gates

Chooses how fast the model lets go of last season, using five seasons of held-out history.

What is swept:
  schedule             how quickly the prior's weight falls week by week (a stretch of the current curve:
                       1.0 is today's, 1.5 lets go more slowly, 0.75 more quickly)
  continuity_strength  how much a team's roster/staff continuity scales its own prior weight
  early_weeks          whether weeks 1..N get their own margin model

How a winner is chosen, deliberately conservatively:
  * walk-forward only: each season is predicted by a model trained on EARLIER seasons
  * the first season (no earlier data) is excluded from every score
  * selection is on margin error, not on ATS win rate. With two dozen candidates, picking the best
    ATS record mostly picks the luckiest one; margin error is far less noisy. ATS is reported beside it.
  * a candidate must beat the current policy by at least MIN_GAIN points AND do so in a majority of
    seasons. Anything less is treated as noise and the current policy is kept.

The blend is recomputed from each season's stored ingredients, so no metrics are rebuilt.
"""
from __future__ import annotations
import argparse
import itertools
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from pipeline import model as M, storage
from pipeline.jobs import backtest as BT
from pipeline.log import JobRun

MIN_GAIN = 0.05            # points of mean margin error a change must win by to count as real
STRETCHES = (0.75, 1.0, 1.5, 2.0)
CONTINUITY = (0.0, 0.5, 1.0)
EARLY = (0, 4)


def stretched_schedule(base: dict, k: float) -> dict:
    """Stretch the prior-weight curve in time. k > 1 keeps last season's weight for longer."""
    weeks = sorted(w for w in base if isinstance(w, int))
    last = weeks[-1] if weeks else 1
    default = float(base.get("default", 0.05))
    def at(week_float):
        if week_float <= 1:
            return float(base.get(1, default))
        lo = int(np.floor(week_float)); hi = lo + 1
        wl = float(base.get(lo, default)) if lo <= last else default
        wh = float(base.get(hi, default)) if hi <= last else default
        return wl + (wh - wl) * (week_float - lo)
    out = {w: round(at(1 + (w - 1) / k), 4) for w in range(1, int(np.ceil(last * max(k, 1))) + 3)}
    out["default"] = default
    return out


def walk_forward(raw: dict, league: str, policy: dict) -> pd.DataFrame:
    """Predict every season from earlier seasons only, under one blend policy."""
    feats = {s: M.apply_blend(f, league, policy) for s, f in raw.items()}
    seasons = sorted(feats)
    preds = []
    for s in seasons[1:]:                       # the first season has nothing earlier to learn from
        train = pd.concat([feats[t] for t in seasons if t < s], ignore_index=True)
        models = M.fit_models(train, league, early_weeks=policy.get("early_weeks", 0))
        p = M.predict_rows(models, feats[s], "sweep", is_backtest=True)
        # the same baselines the backtest reports, so the sweep's evaluation is identical to it
        tr = train[train.margin_home.notna()]
        p["baseline_ratings_margin"] = M.Ridge(["rating_diff_blend", "home_field"], 1.0).fit(tr, "margin_home").predict(feats[s])
        p["baseline_hfa_margin"] = M.Ridge(["home_field"], 1.0).fit(tr, "margin_home").predict(feats[s])
        preds.append(p)
    if not preds:
        return pd.DataFrame()
    pred = pd.concat(preds, ignore_index=True)
    return BT.evaluate(pred, pd.concat(feats.values(), ignore_index=True), league)


def score(ev: pd.DataFrame) -> dict:
    if ev.empty:
        return {}
    ats = ev[ev.model_ats_result.isin(["WIN", "LOSS"])]
    by_season = {int(k): round(float(g.abs_margin_error.mean()), 4) for k, g in ev.groupby("season")}
    early = ev[ev.week <= 4]
    return {"mae": round(float(ev.abs_margin_error.mean()), 4), "mae_early": round(float(early.abs_margin_error.mean()), 4) if len(early) else None,
            "ats": round(float((ats.model_ats_result == "WIN").mean()), 4) if len(ats) else None, "ats_n": int(len(ats)),
            "n": int(len(ev)), "by_season": by_season}


def run(league: str, apply: bool, job: JobRun, raw: dict | None = None,
        grid: list | None = None) -> dict:
    if raw is None:
        raw = {s: M.season_features(league, s) for s in config.BACKTEST_SEASONS}
    raw = {s: f for s, f in raw.items() if not f.empty}
    if len(raw) < 3:
        print(f"{league}: need at least three seasons of matchup edges to sweep; found {sorted(raw)}")
        return {}
    base_sched = M.blend_policy(league)["schedule"]
    current = M.blend_policy(league)
    results = []
    grid = grid or list(itertools.product(STRETCHES, CONTINUITY, EARLY))
    print(f"{league}: sweeping {len(grid)} policies over seasons {sorted(raw)} (first season excluded from scoring)")
    for k, cs, ew in grid:
        pol = {"schedule": stretched_schedule(base_sched, k), "continuity_strength": cs, "early_weeks": ew}
        sc = score(walk_forward(raw, league, pol))
        if not sc:
            continue
        results.append({"stretch": k, "continuity_strength": cs, "early_weeks": ew, "policy": pol, **sc})
    if not results:
        return {}
    cur = next((r for r in results if r["stretch"] == 1.0 and r["continuity_strength"] == float(current.get("continuity_strength", 0.0))
                and r["early_weeks"] == int(current.get("early_weeks", 0))), None)
    if cur is None:
        cur = next(r for r in results if r["stretch"] == 1.0 and r["continuity_strength"] == 0.0 and r["early_weeks"] == 0)
    results.sort(key=lambda r: r["mae"])
    print(f"\n{'stretch':>7} {'contin':>6} {'early':>5} {'MAE':>7} {'MAE wk1-4':>9} {'ATS':>6} {'n':>5}  seasons beating current")
    for r in results[:12]:
        wins = sum(1 for s, v in r["by_season"].items() if v < cur["by_season"].get(s, 1e9))
        mark = "  <- current" if r is cur else ""
        ats = f"{r['ats']*100:.1f}%" if r["ats"] is not None else "  n/a"
        print(f"{r['stretch']:>7} {r['continuity_strength']:>6} {r['early_weeks']:>5} {r['mae']:>7.3f} "
              f"{(r['mae_early'] or float('nan')):>9.3f} {ats:>6} {r['ats_n']:>5}  {wins}/{len(r['by_season'])}{mark}")
    best = results[0]
    n_seasons = len(best["by_season"])
    wins = sum(1 for s, v in best["by_season"].items() if v < cur["by_season"].get(s, 1e9))
    gain = cur["mae"] - best["mae"]
    adopt = best is not cur and gain >= MIN_GAIN and wins > n_seasons / 2
    verdict = {"league": league, "current": {k: cur[k] for k in ("stretch", "continuity_strength", "early_weeks", "mae", "ats")},
               "best": {k: best[k] for k in ("stretch", "continuity_strength", "early_weeks", "mae", "ats")},
               "mae_gain": round(gain, 4), "seasons_improved": f"{wins}/{n_seasons}", "adopt": adopt}
    print(f"\n{league}: best is stretch {best['stretch']}, continuity {best['continuity_strength']}, early weeks {best['early_weeks']}"
          f" — margin error {best['mae']:.3f} vs current {cur['mae']:.3f} ({gain:+.3f}), better in {wins}/{n_seasons} seasons.")
    if not adopt:
        why = ("it is the current policy" if best is cur else
               f"the gain is under {MIN_GAIN} points" if gain < MIN_GAIN else "it does not improve a majority of seasons")
        print(f"{league}: keeping the current policy — {why}. That is the honest result, not a failure.")
    elif apply:
        path = config.TABLES / "model" / "blend_policy.json"
        existing = json.loads(path.read_text()) if path.exists() else {}
        existing[league] = {**best["policy"], "adopted_at": datetime.now(timezone.utc).isoformat(),
                            "evidence": {"mae": best["mae"], "current_mae": cur["mae"], "seasons_improved": f"{wins}/{n_seasons}"}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=1, default=str))
        print(f"{league}: adopted. Written to {path.relative_to(config.ROOT)}. Re-run the backtest so the saved model uses it.")
    else:
        print(f"{league}: clears both gates. Re-run with --apply to adopt it.")
    report = config.TABLES / "model" / "backtest" / league / "blend_sweep.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"verdict": verdict, "results": [{k: v for k, v in r.items() if k != "policy"} for r in results]},
                                 indent=1, default=str))
    job.rows_written += len(results)
    return verdict


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="BOTH", choices=["NFL", "CFB", "BOTH"])
    p.add_argument("--apply", action="store_true", help="adopt the winner, but only if it clears both gates")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    with JobRun("TUNE_BLEND", a.league, a.trigger) as job:
        for lg in (["NFL", "CFB"] if a.league == "BOTH" else [a.league]):
            run(lg, a.apply, job)


if __name__ == "__main__":
    main()
