"""
Forward tests: pre-registered hypotheses, graded on games that had not been played when they were written.

Why this exists. The backtest can only suggest where the model might have an edge; slicing history
always turns up patterns that fade. The honest confirmation is on NEW games. Each cohort in
config.COHORTS names a hypothesis in advance, and this module grades it automatically every day.

How membership is decided -- exactly as the backtest measured it:
    projection  the model's last prediction made BEFORE kickoff
    line        the closing number: the last snapshot before kickoff (DraftKings first, then any book)
    edge        |projection - line|, and the cohort takes the side the model favours
There is no market filter. A cohort is graded whether or not a pick was made, so the confirmation gate
on live picks cannot shrink or bias it.

What is recorded for each game: the side, the closing line, the opening line, whether the line moved
toward the model's side before kickoff, and the result. Games are graded once and never rewritten.

The verdict is deliberately slow. The running record is shown every day, but no verdict is issued until
decide_at graded games, because checking daily and stopping when the numbers look good is a reliable way
to "confirm" noise. At decide_at, a single test against break-even decides it.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from pipeline import storage

BREAK_EVEN = 0.5238
COHORT_DIR = config.TABLES / "model" / "cohorts"


def _p_above(wins: int, n: int) -> float:
    """One-sided p-value that a side truly winning at break-even produced a record this good."""
    if not n:
        return 1.0
    se = math.sqrt(BREAK_EVEN * (1 - BREAK_EVEN) / n)
    z = (wins / n - BREAK_EVEN) / se
    return 0.5 * math.erfc(z / math.sqrt(2))


def _pregame_predictions(league: str, season: int) -> pd.DataFrame:
    p = storage.read_table(config.TABLES / "model" / "predictions" / league / f"{season}.csv")
    if p.empty:
        return p
    p = p.copy()
    p["predicted_at"] = pd.to_datetime(p.predicted_at, utc=True, errors="coerce")
    return p


def _snapshots(league: str, season: int, week: int) -> pd.DataFrame:
    s = storage.read_table(config.TABLES / "market" / "snapshots" / league / str(season) / f"W{week:02d}.csv")
    if s.empty:
        return s
    s = s.copy()
    s["retrieved_at"] = pd.to_datetime(s.retrieved_at, utc=True, errors="coerce")
    return s


def _line(snaps: pd.DataFrame, col: str, before, which: str):
    """Opening or closing number before kickoff, DraftKings first for a consistent source."""
    if snaps.empty or col not in snaps.columns:
        return None
    d = snaps[snaps[col].notna()]
    if before is not None:
        d = d[d.retrieved_at < before]
    if d.empty:
        return None
    pref = d[d.book.astype(str).str.lower() == "draftkings"] if "book" in d.columns else d
    d = pref if not pref.empty else d
    d = d.sort_values("retrieved_at")
    return float((d.iloc[-1] if which == "close" else d.iloc[0])[col])


def evaluate_game(cohort: dict, game: pd.Series, pred: pd.DataFrame, snaps: pd.DataFrame,
                  result: pd.Series | None) -> dict | None:
    """Membership, side and (when final) result for one game. None if the game is not in the cohort."""
    kick = pd.Timestamp(game.kickoff_utc) if pd.notna(game.kickoff_utc) else None
    gp = pred[pred.game_id == game.game_id] if not pred.empty else pred
    if kick is not None and not gp.empty:
        gp = gp[gp.predicted_at < kick]
    if gp.empty:
        return None
    last = gp.sort_values("predicted_at").iloc[-1]
    gs = snaps[snaps.game_id == game.game_id] if not snaps.empty else snaps
    market = cohort["market"]
    if market == "TOTAL":
        proj = float(last.proj_total)
        close, opening = _line(gs, "total", kick, "close"), _line(gs, "total", kick, "open")
        if close is None:
            return None
        edge = proj - close
        side = "OVER" if edge > 0 else "UNDER"
        moved = None if opening is None else (close - opening)
        toward = None if moved is None or moved == 0 else ((moved > 0) == (side == "OVER"))
    else:  # SPREAD: home perspective
        proj = float(last.proj_margin_home)
        close_sp, open_sp = _line(gs, "spread_home", kick, "close"), _line(gs, "spread_home", kick, "open")
        if close_sp is None:
            return None
        edge = proj - (-close_sp)                          # model margin minus the market's margin
        side = "HOME" if edge > 0 else "AWAY"
        close, opening = close_sp, open_sp
        moved = None if open_sp is None else (close_sp - open_sp)
        toward = None if moved is None or moved == 0 else ((moved < 0) == (side == "HOME"))
    if abs(edge) < cohort["min_edge"]:
        return None
    rec = {"cohort_id": cohort["id"], "game_id": game.game_id, "league": cohort["league"],
           "season": int(game.season) if "season" in game.index and pd.notna(game.season) else None,
           "week": int(game.week), "kickoff_utc": str(game.kickoff_utc), "side": side,
           "projection": round(proj, 2), "close_line": close, "open_line": opening,
           "edge": round(abs(edge), 2), "line_moved_toward_model": toward,
           "predicted_at": last.predicted_at.isoformat(), "result": None, "graded_at": None}
    if result is not None:
        if market == "TOTAL":
            d = float(result.total) - close
            rec["result"] = "PUSH" if d == 0 else ("WIN" if (d > 0) == (side == "OVER") else "LOSS")
        else:
            cover = float(result.margin_home) + close
            rec["result"] = "PUSH" if cover == 0 else ("WIN" if (cover > 0) == (side == "HOME") else "LOSS")
        rec["graded_at"] = datetime.now(timezone.utc).isoformat()
    return rec


def update(league: str, season: int) -> dict:
    """Grade every finished game into its cohorts (once), and list this week's provisional members."""
    cohorts = [c for c in config.COHORTS if c["league"] == league]
    if not cohorts:
        return {}
    games = storage.read_table(storage.games_path(league, season))
    if games.empty:
        return {}
    games = games[games.season_type == "REG"] if "season_type" in games.columns else games
    res = storage.read_table(config.TABLES / "results" / league / f"{season}.csv")
    res = res.set_index("game_id") if not res.empty else res
    pred = _pregame_predictions(league, season)
    out = {}
    for c in cohorts:
        path = COHORT_DIR / f"{c['id']}.csv"
        done = storage.read_table(path)
        graded = set(done.game_id) if not done.empty else set()
        new_rows, watch = [], []
        # Only games kicking off after the hypothesis was registered count. Games played before it -- even
        # ones the analysis never saw -- are excluded, so the test is forward in the strict sense.
        registered = pd.Timestamp(c["registered"], tz="UTC")
        for wk, wg in games.groupby("week"):
            snaps = _snapshots(league, season, int(wk))
            for _, g in wg.iterrows():
                if pd.isna(g.kickoff_utc) or pd.Timestamp(g.kickoff_utc) < registered:
                    continue
                final = not res.empty and g.game_id in res.index
                if final and g.game_id in graded:
                    continue
                r = evaluate_game(c, g, pred, snaps, res.loc[g.game_id] if final else None)
                if r is None:
                    continue
                (new_rows if final else watch).append(r)
        if new_rows:
            storage.append_csv(path, pd.DataFrame(new_rows), ["cohort_id", "game_id"], on_duplicate="skip")
        out[c["id"]] = {"newly_graded": len(new_rows), "watching": watch}
    return out


def summary(cohort: dict) -> dict:
    """Running record plus the verdict, which is withheld until the pre-registered sample size."""
    df = storage.read_table(COHORT_DIR / f"{cohort['id']}.csv")
    decided = df[df.result.isin(["WIN", "LOSS"])] if not df.empty else df
    n = int(len(decided))
    wins = int((decided.result == "WIN").sum()) if n else 0
    pushes = int((df.result == "PUSH").sum()) if not df.empty else 0
    primaries = max(1, sum(1 for c in config.COHORTS if c.get("primary")))
    alpha = cohort["alpha"] / primaries if cohort.get("primary") else cohort["alpha"]
    p = _p_above(wins, n)
    moved = df[df.line_moved_toward_model.notna()] if not df.empty and "line_moved_toward_model" in df.columns else pd.DataFrame()
    toward = float(moved.line_moved_toward_model.astype(bool).mean()) if len(moved) else None
    if n < cohort["decide_at"]:
        verdict = "collecting"
        verdict_text = (f"Collecting: {n} of {cohort['decide_at']} graded games. The verdict is withheld until then — "
                        f"reading the rate early is how noise gets mistaken for an edge.")
    elif not cohort.get("primary"):
        verdict = "descriptive"
        verdict_text = "Tracked to show whether the effect strengthens at a higher threshold; not a separate test."
    elif p < alpha:
        verdict = "confirmed"
        verdict_text = (f"Confirmed: {wins}-{n - wins} ({wins / n:.1%}) clears break-even at p = {p:.4f}, "
                        f"below the pre-registered bar of {alpha:.4f}.")
    else:
        verdict = "not confirmed"
        verdict_text = (f"Not confirmed: {wins}-{n - wins} ({wins / n:.1%}) at p = {p:.3f} does not clear the "
                        f"pre-registered bar of {alpha:.4f}. The backtest pattern did not survive new games.")
    return {"id": cohort["id"], "label": cohort["label"], "hypothesis": cohort["hypothesis"],
            "evidence": cohort["evidence"], "registered": cohort["registered"], "primary": bool(cohort.get("primary")),
            "wins": wins, "losses": n - wins, "pushes": pushes, "n": n,
            "rate": round(wins / n, 4) if n else None, "p_value": round(p, 4) if n else None,
            "decide_at": cohort["decide_at"], "alpha": round(alpha, 4), "progress": round(min(1.0, n / cohort["decide_at"]), 3),
            "line_moved_toward_model": None if toward is None else round(toward, 3),
            "verdict": verdict, "verdict_text": verdict_text}


def watch_list(league: str, season: int, week: int) -> dict:
    """
    Read-only: this week's games currently qualifying for each cohort, on the latest line. Provisional --
    official membership is fixed by the closing number at kickoff, which can move a game in or out.
    """
    games = storage.read_table(storage.games_path(league, season))
    if games.empty:
        return {}
    wk = games[games.week == week]
    res = storage.read_table(config.TABLES / "results" / league / f"{season}.csv")
    final = set(res.game_id) if not res.empty else set()
    pred, snaps = _pregame_predictions(league, season), _snapshots(league, season, week)
    out = {}
    for c in [c for c in config.COHORTS if c["league"] == league]:
        rows = []
        for _, g in wk.iterrows():
            if g.game_id in final:
                continue
            r = evaluate_game(c, g, pred, snaps, None)
            if r is not None:
                rows.append({k: r[k] for k in ("game_id", "side", "projection", "close_line", "edge", "kickoff_utc")})
        out[c["id"]] = sorted(rows, key=lambda x: -x["edge"])
    return out
