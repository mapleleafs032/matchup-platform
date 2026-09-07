"""
python -m pipeline.jobs.predict --league NFL            # current + next week
python -m pipeline.jobs.predict --league CFB --weeks 3

Appends one predictions row per upcoming game (is_pregame_final=False). Games already kicked off or LOCKED are
never re-predicted. The row carries the latest market snapshot (consensus-first) for spread_diff / total_diff and
the per-category contributions for the "Why?" panel. The prediction file is APPEND-ONLY.
"""
from __future__ import annotations
import argparse
import hashlib
import sys

import pandas as pd

import config
from pipeline import model as M, storage
from pipeline.log import JobRun

PRED_DIR = config.TABLES / "model" / "predictions"


def latest_market(league: str, season: int, week: int) -> pd.DataFrame:
    p = config.TABLES / "market" / "snapshots" / league / str(season) / f"W{week:02d}.csv"
    if not p.exists():
        return pd.DataFrame(columns=["game_id", "spread_home", "total", "book", "retrieved_at"])
    s = pd.read_csv(p)
    s = s[s.spread_home.notna() | s.total.notna()].copy()
    s["_pri"] = s.book.map({b: i for i, b in enumerate(config.ODDS_BOOK_PRIORITY)}).fillna(99)
    s = s.sort_values(["game_id", "retrieved_at", "_pri"], ascending=[True, False, True])
    return s.drop_duplicates("game_id")[["game_id", "spread_home", "total", "book", "retrieved_at"]]


def run(league: str, season: int, weeks: list[int], job: JobRun) -> None:
    mv, models = M.load_active_model(league)
    if models is None:
        job.status = "SKIPPED"; job.message = f"no active model for {league}; run backtest --activate first"; return
    games = storage.read_table(storage.games_path(league, season))
    now = pd.Timestamp.now(tz="UTC")
    total = 0
    for wk in weeks:
        feats = M.week_features(league, season, wk)
        if feats.empty:
            print(f"{league} {season} W{wk}: no matchup edges (run build_matchups first)"); continue
        g = games.set_index("game_id")
        feats = feats[feats.game_id.map(lambda x: g.status.get(x) == "SCHEDULED")]
        feats = feats[pd.to_datetime(feats.kickoff_utc, utc=True) > now]
        if feats.empty:
            print(f"{league} {season} W{wk}: no upcoming games"); continue
        pred = M.predict_rows(models, feats, mv, is_backtest=False)
        mkt = latest_market(league, season, wk).set_index("game_id")
        pred["market_spread_home"] = pred.game_id.map(mkt.spread_home); pred["market_total"] = pred.game_id.map(mkt.total)
        pred["market_book"] = pred.game_id.map(mkt.book); pred["market_retrieved_at"] = pred.game_id.map(mkt.retrieved_at)
        pred["spread_diff"] = (pred.proj_margin_home + pred.market_spread_home).round(2)     # + = model likes home more than market
        pred["total_diff"] = (pred.proj_total - pred.market_total).round(2)
        pred["inputs_hash"] = [hashlib.sha256(feats[feats.game_id == gid].drop(columns=["margin_home", "total"]).round(4).to_json().encode()).hexdigest()[:16] for gid in pred.game_id]
        # skip games whose inputs hash is unchanged since the last prediction (no new information -> no new row)
        path = PRED_DIR / league / f"{season}.csv"
        cur = storage.read_table(path)
        if not cur.empty:
            last = cur.sort_values("predicted_at").drop_duplicates("game_id", keep="last").set_index("game_id")
            unchanged = pred.game_id.map(lambda x: last.inputs_hash.get(x) == pred.set_index("game_id").inputs_hash.get(x)) & pred.game_id.map(lambda x: last.market_spread_home.get(x)).eq(pred.market_spread_home)
            pred = pred[~unchanged.fillna(False)]
        n = storage.append_csv(path, pred, ["prediction_id"], on_duplicate="skip") if not pred.empty else 0
        total += n
        if n:
            show = pred.sort_values("proj_margin_home").head(3)
            print(f"{league} {season} W{wk}: {n} predictions written; e.g. " + " | ".join(f"{r.game_id.split('_',3)[3]} {r.proj_away_pts:.0f}-{r.proj_home_pts:.0f} (mkt {r.market_spread_home})" for _, r in show.iterrows()))
        else:
            print(f"{league} {season} W{wk}: nothing new (inputs and market unchanged)")
    job.rows_written = total


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--weeks", nargs="*", type=int)
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    if a.season < config.MIN_ALLOWED_SEASON:
        sys.exit("season refused")
    games = storage.read_table(storage.games_path(a.league, a.season))
    if a.weeks:
        weeks = a.weeks
    else:
        sched = games[games.status == "SCHEDULED"]
        cur = int(sched.week.min()) if not sched.empty else int(games.week.max())
        weeks = [cur, cur + 1]
    with JobRun(f"{a.league}_PREDICT", a.league, a.trigger) as job:
        run(a.league, a.season, weeks, job)


if __name__ == "__main__":
    main()
