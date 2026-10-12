"""
Is the market data actually arriving?

A pull that returns nothing used to count as a success: the job went green, the gap was invisible, and
it surfaced days later as a hole in a chart. That matters beyond the charts -- a game with no snapshot
near kickoff has no closing number, so it drops out of closing-line value and out of the forward tests
without anything being said.

Three things live here:
  heartbeat  every ingest run records when it ran and how much it got, so silence is itself a record
  gaps       the longest stretch with no successful pull, per league
  coverage   per game: did we observe an opening number and a number close to kickoff

Nothing here can recover a price that was never fetched. The point is that a miss is reported the same
day instead of being noticed a week later.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pandas as pd

import config
from pipeline import storage

HEARTBEAT = config.TABLES / "ops" / "ingest_heartbeat.csv"
# a game is considered to have a usable closing observation if something was seen this close to kickoff
CLOSING_WINDOW_MIN = 180
OPENING_MIN_HOURS = 24          # and an opening observation at least this far out


def record(source: str, league: str, season: int, rows: int, games: int, note: str = "") -> None:
    """One row per ingest attempt, successful or empty."""
    storage.append_csv(HEARTBEAT, pd.DataFrame([{
        "beat_id": f"{source}_{league}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}",
        "source": source, "league": league, "season": season,
        "ran_at": datetime.now(timezone.utc).isoformat(), "rows": int(rows), "games": int(games),
        "ok": bool(rows > 0), "note": note}]), ["beat_id"], on_duplicate="skip")


def gaps(source: str, league: str, days: int = 7) -> dict:
    """Longest stretch with no successful pull, and how long since the last one."""
    hb = storage.read_table(HEARTBEAT)
    now = pd.Timestamp.now(tz="UTC")
    if hb.empty:
        return {"last_ok": None, "hours_since": None, "longest_gap_h": None, "attempts": 0, "empty_pulls": 0}
    hb = hb[(hb.source == source) & (hb.league == league)].copy()
    if hb.empty:
        return {"last_ok": None, "hours_since": None, "longest_gap_h": None, "attempts": 0, "empty_pulls": 0}
    hb["ran_at"] = pd.to_datetime(hb.ran_at, utc=True, errors="coerce")
    hb = hb[hb.ran_at >= now - timedelta(days=days)].sort_values("ran_at")
    ok = hb[hb.ok.astype(bool)]
    if ok.empty:
        return {"last_ok": None, "hours_since": None, "longest_gap_h": None,
                "attempts": int(len(hb)), "empty_pulls": int(len(hb))}
    times = [now - timedelta(days=days)] + list(ok.ran_at) + [now]
    longest = max((b - a).total_seconds() / 3600 for a, b in zip(times[:-1], times[1:]))
    return {"last_ok": ok.ran_at.iloc[-1].isoformat(), "hours_since": round((now - ok.ran_at.iloc[-1]).total_seconds() / 3600, 1),
            "longest_gap_h": round(longest, 1), "attempts": int(len(hb)), "empty_pulls": int((~hb.ok.astype(bool)).sum())}


def observation_coverage(league: str, season: int, week: int) -> dict:
    """For each game that has kicked off: was an opening and a near-kickoff number actually observed?"""
    games = storage.read_table(storage.games_path(league, season))
    snaps = storage.read_table(config.TABLES / "market" / "snapshots" / league / str(season) / f"W{week:02d}.csv")
    if games.empty:
        return {}
    wk = games[(games.week == week)]
    if wk.empty:
        return {}
    now = pd.Timestamp.now(tz="UTC")
    if not snaps.empty:
        snaps = snaps.copy()
        snaps["retrieved_at"] = pd.to_datetime(snaps.retrieved_at, utc=True, errors="coerce")
    started, with_close, with_open, missing = 0, 0, 0, []
    for _, g in wk.iterrows():
        if pd.isna(g.kickoff_utc):
            continue
        kick = pd.Timestamp(g.kickoff_utc)
        if kick > now:
            continue
        started += 1
        gs = snaps[snaps.game_id == g.game_id] if not snaps.empty else pd.DataFrame()
        pre = gs[gs.retrieved_at < kick] if not gs.empty else gs
        if pre.empty:
            missing.append({"game_id": g.game_id, "why": "no pre-kickoff snapshot at all"})
            continue
        last, first = pre.retrieved_at.max(), pre.retrieved_at.min()
        close_ok = (kick - last).total_seconds() / 60 <= CLOSING_WINDOW_MIN
        open_ok = (kick - first).total_seconds() / 3600 >= OPENING_MIN_HOURS
        with_close += bool(close_ok)
        with_open += bool(open_ok)
        if not close_ok:
            missing.append({"game_id": g.game_id, "why": f"last snapshot {round((kick - last).total_seconds()/3600,1)}h before kickoff"})
    return {"week": week, "started": started, "with_closing": with_close, "with_opening": with_open,
            "missing_closing": missing[:20], "missing_closing_n": len(missing)}


def summary_line(source: str, league: str) -> str:
    g = gaps(source, league)
    if g["last_ok"] is None:
        return f"{source} {league}: no successful pull in the last 7 days ({g['attempts']} attempts)"
    return (f"{source} {league}: last pull {g['hours_since']}h ago, longest gap {g['longest_gap_h']}h over 7 days, "
            f"{g['empty_pulls']} of {g['attempts']} attempts returned nothing")
