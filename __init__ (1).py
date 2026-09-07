"""
python -m pipeline.jobs.lock --league BOTH          # lock every SCHEDULED game whose kickoff has passed
python -m pipeline.jobs.lock --league BOTH --evaluate   # also evaluate locked games that now have results

Lock (§27, §76): for each game with kickoff_utc <= now and status SCHEDULED:
  * the latest prediction row with predicted_at < kickoff becomes the pregame-final prediction
    (recorded as an APPEND-ONLY flag row in pregame_final_flags.csv, never an update)
  * the last market snapshot per book before kickoff becomes the closing line
  * data/snapshots/pregame_{game_id}.json is written once with everything that existed before kickoff:
    prediction, matchup edges, both teams' as-of metrics, QB status, injuries, weather, market history, model version
  * games.status -> LOCKED, locked_at set. Nothing else on the game row changes, ever.
Evaluate (§28): for LOCKED games with a results row, append one model_evaluation row and set status FINAL.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timezone

import pandas as pd

import config
from pipeline import storage
from pipeline.log import JobRun

MODEL = config.TABLES / "model"
AN = config.TABLES / "analytics"
ROSTER = config.TABLES / "roster"


def _rows_json(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso")) if df is not None and not df.empty else []


def lock_league(league: str, season: int, job: JobRun, now: pd.Timestamp) -> int:
    games = storage.read_table(storage.games_path(league, season))
    if games.empty:
        return 0
    due = games[(games.status == "SCHEDULED") & games.kickoff_utc.notna() & (pd.to_datetime(games.kickoff_utc, utc=True) <= now)]
    if due.empty:
        return 0
    preds = storage.read_table(MODEL / "predictions" / league / f"{season}.csv")
    flags_path = MODEL / "pregame_final_flags.csv"
    index_path = MODEL / "pregame_snapshots_index.csv"
    n = 0
    for _, g in due.iterrows():
        kick = pd.Timestamp(g.kickoff_utc)
        gid = g.game_id
        wk = int(g.week)
        # 1) final prediction = latest row predicted before kickoff
        pfinal = pd.DataFrame()
        if not preds.empty:
            pg = preds[(preds.game_id == gid) & (pd.to_datetime(preds.predicted_at, utc=True) < kick)]
            if not pg.empty:
                pfinal = pg.sort_values("predicted_at").tail(1)
        # 2) closing lines = last snapshot per book before kickoff
        snap_path = config.TABLES / "market" / "snapshots" / league / str(season) / f"W{wk:02d}.csv"
        close = pd.DataFrame()
        hist = pd.DataFrame()
        if snap_path.exists():
            s = pd.read_csv(snap_path); s = s[s.game_id == gid]
            s = s[pd.to_datetime(s.retrieved_at, utc=True) < kick]
            hist = s.sort_values("retrieved_at")
            close = hist.sort_values("retrieved_at").drop_duplicates("book", keep="last")
        pref = [b for b in config.ODDS_BOOK_PRIORITY if b in set(close.book)] if not close.empty else []
        cl = close[close.book == pref[0]].iloc[0] if pref else None
        # 3) gather everything else that existed pre-kickoff
        edges = storage.read_table(AN / "matchup_edges" / league / str(season) / f"W{wk:02d}.parquet")
        edges = edges[edges.game_id == gid] if not edges.empty else edges
        metrics = storage.read_table(AN / "team_metrics_asof" / league / str(season) / f"W{wk:02d}.parquet")
        metrics = metrics[metrics.as_of_game_id == gid] if not metrics.empty else metrics
        qb = storage.read_table(ROSTER / "qb_status" / league / str(season) / f"W{wk:02d}.parquet")
        qb = qb[qb.team_id.isin([g.home_team_id, g.away_team_id])] if not qb.empty else qb
        inj = storage.read_table(ROSTER / "injuries" / league / f"{season}.csv")
        inj = inj[(inj.week == wk) & inj.team_id.isin([g.home_team_id, g.away_team_id])] if not inj.empty else inj
        wpath = config.TABLES / "context" / "weather_snapshots" / league / str(season) / f"W{wk:02d}.csv"
        wx = pd.read_csv(wpath) if wpath.exists() else pd.DataFrame()
        wx = wx[(wx.game_id == gid) & (pd.to_datetime(wx.retrieved_at, utc=True) < kick)] if not wx.empty else wx
        snapshot = {
            "game_id": gid, "league": league, "season": season, "week": wk, "locked_at": now.isoformat(), "kickoff_utc": kick.isoformat(),
            "game": json.loads(g.to_json(date_format="iso")),
            "prediction": _rows_json(pfinal)[0] if not pfinal.empty else None,
            "closing_lines": _rows_json(close), "market_history": _rows_json(hist),
            "matchup_edges": _rows_json(edges), "team_metrics_asof": _rows_json(metrics),
            "qb_status": _rows_json(qb), "injuries": _rows_json(inj), "weather": _rows_json(wx.tail(1)),
            "model_version": pfinal.model_version.iloc[0] if not pfinal.empty else None, "pipeline_version": config.PIPELINE_VERSION,
        }
        config.SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        spath = config.SNAPSHOTS / f"pregame_{gid}.json"
        if spath.exists():
            continue    # a snapshot is written once, ever
        body = json.dumps(snapshot, default=str, indent=0)
        spath.write_text(body)
        sha = hashlib.sha256(body.encode()).hexdigest()
        storage.append_csv(index_path, pd.DataFrame([{
            "game_id": gid, "locked_at": now.isoformat(), "prediction_id": pfinal.prediction_id.iloc[0] if not pfinal.empty else None,
            "model_version": snapshot["model_version"], "snapshot_path": str(spath.relative_to(config.ROOT)) if spath.is_relative_to(config.ROOT) else str(spath), "snapshot_sha256": sha,
            "closing_spread_home": None if cl is None else cl.spread_home, "closing_total": None if cl is None else cl.total,
            "closing_ml_home": None if cl is None else cl.ml_home, "closing_ml_away": None if cl is None else cl.ml_away, "closing_book": None if cl is None else cl.book,
            "weather_snapshot_ts": wx.retrieved_at.iloc[-1] if not wx.empty else None, "injuries_as_of": inj.retrieved_at.max() if not inj.empty else None,
            "had_prediction": not pfinal.empty}]), ["game_id"], on_duplicate="skip")
        if not pfinal.empty:
            storage.append_csv(flags_path, pd.DataFrame([{"prediction_id": pfinal.prediction_id.iloc[0], "game_id": gid, "model_version": snapshot["model_version"], "flagged_at": now.isoformat()}]),
                               ["game_id", "model_version"], on_duplicate="skip")
        games.loc[games.game_id == gid, "status"] = "LOCKED"; games.loc[games.game_id == gid, "locked_at"] = now.isoformat()
        n += 1
    if n:
        storage.write_parquet(storage.games_path(league, season), games)
    print(f"{league} {season}: locked {n} games")
    return n


def evaluate_league(league: str, season: int, job: JobRun) -> int:
    games = storage.read_table(storage.games_path(league, season))
    res = storage.read_table(config.TABLES / "results" / league / f"{season}.csv")
    idx = storage.read_table(MODEL / "pregame_snapshots_index.csv")
    preds = storage.read_table(MODEL / "predictions" / league / f"{season}.csv")
    if games.empty or res.empty or idx.empty or preds.empty:
        return 0
    locked = games[(games.status == "LOCKED") & games.game_id.isin(res.game_id)]
    if locked.empty:
        return 0
    res = res.set_index("game_id"); idx = idx.set_index("game_id"); preds = preds.set_index("prediction_id")
    rows = []
    for _, g in locked.iterrows():
        gid = g.game_id
        if gid not in idx.index or pd.isna(idx.loc[gid].prediction_id) or idx.loc[gid].prediction_id not in preds.index:
            games.loc[games.game_id == gid, "status"] = "FINAL"; continue
        p = preds.loc[idx.loc[gid].prediction_id]; r = res.loc[gid]
        cs = idx.loc[gid].closing_spread_home; ct = idx.loc[gid].closing_total
        mm = -cs if pd.notna(cs) else None
        cover_home = r.margin_home + cs if pd.notna(cs) else None
        side_home = (p.proj_margin_home > mm) if mm is not None else None
        rows.append({"prediction_id": p.name, "game_id": gid, "model_version": p.model_version, "league": league, "season": season, "week": int(g.week),
                     "is_backtest": False, "actual_margin_home": int(r.margin_home), "actual_total": int(r.total),
                     "margin_error": round(p.proj_margin_home - r.margin_home, 2), "abs_margin_error": round(abs(p.proj_margin_home - r.margin_home), 2),
                     "total_error": round(p.proj_total - r.total, 2), "abs_total_error": round(abs(p.proj_total - r.total), 2),
                     "away_pts_abs_err": round(abs(p.proj_away_pts - r.away_score), 2), "home_pts_abs_err": round(abs(p.proj_home_pts - r.home_score), 2),
                     "winner_correct": bool((p.proj_margin_home > 0) == (r.margin_home > 0)) if r.margin_home != 0 else None,
                     "favorite_side": None if pd.isna(cs) else ("HOME" if cs < 0 else "AWAY" if cs > 0 else "PICKEM"),
                     "favorite_correct": None if pd.isna(cs) or cs == 0 else bool((cs < 0) == (r.margin_home > 0)),
                     "model_ats_result": None if cover_home is None else ("PUSH" if cover_home == 0 else ("WIN" if (cover_home > 0) == side_home else "LOSS")),
                     "model_ou_result": None if pd.isna(ct) else ("PUSH" if r.total == ct else ("WIN" if (r.total > ct) == (p.proj_total > ct) else "LOSS")),
                     "clv_spread": None, "win_prob_bin": int(min(p.win_prob_home * 10, 9)), "evaluated_at": datetime.now(timezone.utc).isoformat()})
        games.loc[games.game_id == gid, "status"] = "FINAL"
    if rows:
        storage.append_csv(MODEL / "model_evaluation" / league / f"{season}.csv", pd.DataFrame(rows), ["prediction_id"], on_duplicate="skip")
    storage.write_parquet(storage.games_path(league, season), games)
    print(f"{league} {season}: evaluated {len(rows)} locked games -> FINAL")
    return len(rows)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="BOTH", choices=["NFL", "CFB", "BOTH"])
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--evaluate", action="store_true")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    leagues = ["NFL", "CFB"] if a.league == "BOTH" else [a.league]
    with JobRun("LOCK", a.league, a.trigger) as job:
        now = pd.Timestamp.now(tz="UTC")
        for lg in leagues:
            job.rows_written += lock_league(lg, a.season, job, now)
            if a.evaluate:
                job.rows_written += evaluate_league(lg, a.season, job)


if __name__ == "__main__":
    main()
