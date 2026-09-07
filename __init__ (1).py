"""
Market engine (master prompt §23-26, §70). Everything here is reconstructed from our own timestamped
market_snapshots — never from a provider's "movement" field. Language is evidence-based: the engine
states what moved, when, and by how much, and what would have to be true for a stronger claim.

Per game it produces:
  open / current per book (consensus-first book priority), full history series
  movement in points and in KEY-NUMBER units (a move -2.5 -> -3 crosses 3; -4 -> -4.5 crosses nothing)
  steam flag: >= 1.0 pt (spread) move within STEAM_WINDOW_HOURS across >= 2 books in the same direction
  book disagreement: spread range across books at the latest snapshot
  no-vig implied win probabilities from the moneyline pair (§25)
  model vs market: spread_diff, total_diff, model win prob vs market implied prob
  public betting: UNAVAILABLE (no free structured feed) -> reverse-line-movement is explicitly not claimed
  a list of short factual sentences for the UI / AI package
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from pipeline import storage

KEY_NUMBERS_SPREAD = [3, 7, 10, 14, 17, 21]
KEY_NUMBERS_TOTAL_NFL = [37, 41, 43, 44, 47, 51]
SNAP_DIR = config.TABLES / "market" / "snapshots"


def _book_priority(book: str) -> int:
    try:
        return config.ODDS_BOOK_PRIORITY.index(book)
    except ValueError:
        return 99


def no_vig(ml_home: float | None, ml_away: float | None) -> tuple[float | None, float | None]:
    """Implied probabilities with the overround removed (proportional method)."""
    if ml_home is None or ml_away is None or pd.isna(ml_home) or pd.isna(ml_away):
        return None, None
    def imp(ml):
        ml = float(ml)
        return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)
    h, a = imp(ml_home), imp(ml_away)
    tot = h + a
    return (round(h / tot, 4), round(a / tot, 4)) if tot else (None, None)


def key_numbers_crossed(a: float | None, b: float | None, keys: list[int]) -> list[int]:
    """Key numbers k where exactly one of |a|, |b| is at or beyond k: moving onto, through, or off a key number all count."""
    if a is None or b is None or pd.isna(a) or pd.isna(b) or a == b:
        return []
    return [k for k in keys if (abs(a) >= k) != (abs(b) >= k)]


def load_history(league: str, season: int, week: int, game_id: str) -> pd.DataFrame:
    p = SNAP_DIR / league / str(season) / f"W{week:02d}.csv"
    if not p.exists():
        return pd.DataFrame()
    s = pd.read_csv(p)
    s = s[s.game_id == game_id].copy()
    s["retrieved_at"] = pd.to_datetime(s.retrieved_at, utc=True)
    return s.sort_values("retrieved_at")


def analyze_game(league: str, hist: pd.DataFrame, pred: pd.Series | None, kickoff: pd.Timestamp | None, now: pd.Timestamp) -> dict:
    if hist.empty:
        return {"available": False, "notes": ["No market snapshots for this game yet."], "public": None}
    hist = hist[hist.retrieved_at <= (kickoff if kickoff is not None else now)]
    if hist.empty:
        return {"available": False, "notes": ["No pre-kickoff market snapshots."], "public": None}
    books = sorted(hist.book.unique(), key=_book_priority)
    primary = books[0]
    h = hist[hist.book == primary]
    first, last = h.iloc[0], h.iloc[-1]
    latest_ts = hist.retrieved_at.max()
    latest_all = hist[hist.retrieved_at >= latest_ts - pd.Timedelta(hours=6)].sort_values("retrieved_at").drop_duplicates("book", keep="last")
    open_spread = first.provider_open_spread_home if pd.notna(first.get("provider_open_spread_home", np.nan)) else first.spread_home
    open_total = first.provider_open_total if pd.notna(first.get("provider_open_total", np.nan)) else first.total
    cur_spread, cur_total = last.spread_home, last.total
    spread_move = None if pd.isna(open_spread) or pd.isna(cur_spread) else round(float(cur_spread - open_spread), 1)
    total_move = None if pd.isna(open_total) or pd.isna(cur_total) else round(float(cur_total - open_total), 1)
    keys_spread = key_numbers_crossed(open_spread, cur_spread, KEY_NUMBERS_SPREAD)
    keys_total = key_numbers_crossed(open_total, cur_total, KEY_NUMBERS_TOTAL_NFL) if league == "NFL" else []
    # steam: >=1pt move within window across >=2 books, same direction
    steam = None
    if hist.book.nunique() >= 2 and len(hist) >= 4:
        w = hist[hist.retrieved_at >= latest_ts - pd.Timedelta(hours=config.STEAM_WINDOW_HOURS)]
        moves = {}
        for b, g in w.groupby("book"):
            g = g[g.spread_home.notna()]
            if len(g) >= 2:
                moves[b] = float(g.spread_home.iloc[-1] - g.spread_home.iloc[0])
        big = {b: m for b, m in moves.items() if abs(m) >= 1.0}
        if len(big) >= 2 and len({np.sign(m) for m in big.values()}) == 1:
            steam = {"direction": "toward_home" if list(big.values())[0] < 0 else "toward_away", "books": sorted(big), "window_hours": config.STEAM_WINDOW_HOURS,
                     "avg_move": round(float(np.mean(list(big.values()))), 2)}
    # disagreement across books at the latest snapshot
    spreads_now = latest_all.spread_home.dropna()
    disagreement = round(float(spreads_now.max() - spreads_now.min()), 1) if len(spreads_now) >= 2 else None
    # implied probabilities
    p_home, p_away = no_vig(last.ml_home, last.ml_away)
    series = [{"t": r.retrieved_at.isoformat(), "book": r.book, "spread_home": None if pd.isna(r.spread_home) else float(r.spread_home),
               "total": None if pd.isna(r.total) else float(r.total), "ml_home": None if pd.isna(r.ml_home) else int(r.ml_home),
               "ml_away": None if pd.isna(r.ml_away) else int(r.ml_away)} for _, r in hist.iterrows()]
    # model vs market
    mvm = None
    if pred is not None:
        mm = -float(cur_spread) if pd.notna(cur_spread) else None
        mvm = {"model_margin_home": float(pred.proj_margin_home), "market_margin_home": mm,
               "spread_diff": None if mm is None else round(float(pred.proj_margin_home) - mm, 1),
               "model_total": float(pred.proj_total), "market_total": None if pd.isna(cur_total) else float(cur_total),
               "total_diff": None if pd.isna(cur_total) else round(float(pred.proj_total - cur_total), 1),
               "model_win_prob_home": float(pred.win_prob_home), "market_win_prob_home": p_home,
               "prob_diff": None if p_home is None else round(float(pred.win_prob_home) - p_home, 3)}
    notes = _notes(league, primary, open_spread, cur_spread, spread_move, keys_spread, open_total, cur_total, total_move, steam, disagreement, mvm, len(hist), hist.book.nunique())
    return {"available": True, "primary_book": primary, "books": books, "n_snapshots": int(len(hist)), "first_snapshot": hist.retrieved_at.min().isoformat(),
            "last_snapshot": latest_ts.isoformat(),
            "open": {"spread_home": None if pd.isna(open_spread) else float(open_spread), "total": None if pd.isna(open_total) else float(open_total),
                     "ml_home": None if pd.isna(first.ml_home) else int(first.ml_home), "ml_away": None if pd.isna(first.ml_away) else int(first.ml_away)},
            "current": {"spread_home": None if pd.isna(cur_spread) else float(cur_spread), "total": None if pd.isna(cur_total) else float(cur_total),
                        "ml_home": None if pd.isna(last.ml_home) else int(last.ml_home), "ml_away": None if pd.isna(last.ml_away) else int(last.ml_away),
                        "book": primary, "retrieved_at": last.retrieved_at.isoformat()},
            "movement": {"spread_points": spread_move, "total_points": total_move, "key_numbers_spread": keys_spread, "key_numbers_total": keys_total},
            "steam": steam, "book_disagreement_spread": disagreement,
            "implied": {"home_win_prob_no_vig": p_home, "away_win_prob_no_vig": p_away},
            "model_vs_market": mvm, "public": None, "public_note": "Ticket and money percentages are unavailable (no free structured feed); reverse-line-movement is therefore not evaluated.",
            "series": series, "notes": notes}


def _fmt_spread(x, home_abbr="Home"):
    if x is None or pd.isna(x):
        return "n/a"
    return f"{home_abbr} {x:+.1f}".replace("+-", "-")


def _notes(league, book, os_, cs, sm, keys, ot, ct, tm, steam, disagreement, mvm, n, nb) -> list[str]:
    L = []
    if pd.notna(os_) and pd.notna(cs):
        if sm == 0:
            L.append(f"The spread has held at home {cs:+.1f} since our first snapshot ({book}).")
        else:
            dirn = "toward the home team" if sm < 0 else "toward the away team"
            L.append(f"The spread moved from home {os_:+.1f} to {cs:+.1f} ({abs(sm):.1f} points {dirn}, {book}).")
            if keys:
                L.append(f"That move crosses the key number{'s' if len(keys) > 1 else ''} {', '.join(str(k) for k in keys)}, which matters more than the raw half-points suggest.")
    if pd.notna(ot) and pd.notna(ct) and tm not in (None, 0):
        L.append(f"The total moved from {ot:.1f} to {ct:.1f} ({tm:+.1f}).")
    if steam:
        L.append(f"Between {', '.join(steam['books'])}, the spread moved at least a point {steam['direction'].replace('_', ' ')} within {steam['window_hours']} hours — a coordinated move, though without ticket/money data we cannot say who moved it.")
    if disagreement is not None and disagreement >= 1.0:
        L.append(f"Books disagree by {disagreement:.1f} points on the spread right now.")
    if mvm and mvm.get("spread_diff") is not None:
        d = mvm["spread_diff"]
        side = "the home team" if d > 0 else "the away team"
        L.append(f"The model's margin differs from the market by {abs(d):.1f} points, leaning to {side}." if abs(d) >= 1 else "The model and the market are within a point of each other on the spread.")
    if mvm and mvm.get("total_diff") is not None and abs(mvm["total_diff"]) >= 2:
        L.append(f"On the total the model is {abs(mvm['total_diff']):.1f} points {'over' if mvm['total_diff'] > 0 else 'under'} the market number.")
    L.append(f"Based on {n} snapshots across {nb} book{'s' if nb != 1 else ''}. Public ticket/money percentages are unavailable, so reverse line movement is not claimed.")
    return L


def build_week(league: str, season: int, week: int) -> tuple[pd.DataFrame, dict[str, dict]]:
    games = storage.read_table(storage.games_path(league, season))
    wk = games[(games.week == week) & (games.season_type == "REG")]
    preds = storage.read_table(config.TABLES / "model" / "predictions" / league / f"{season}.csv")
    latest_pred = preds.sort_values("predicted_at").drop_duplicates("game_id", keep="last").set_index("game_id") if not preds.empty else pd.DataFrame()
    now = pd.Timestamp.now(tz="UTC")
    rows, payloads = [], {}
    for _, g in wk.iterrows():
        hist = load_history(league, season, week, g.game_id)
        pred = latest_pred.loc[g.game_id] if not latest_pred.empty and g.game_id in latest_pred.index else None
        kick = pd.Timestamp(g.kickoff_utc) if pd.notna(g.kickoff_utc) else None
        a = analyze_game(league, hist, pred, kick, now)
        a.update({"game_id": g.game_id, "league": league, "season": season, "week": week, "generated_at": now.isoformat()})
        payloads[g.game_id] = a
        rows.append({"game_id": g.game_id, "available": a["available"], "primary_book": a.get("primary_book"), "n_snapshots": a.get("n_snapshots", 0),
                     "open_spread_home": (a.get("open") or {}).get("spread_home"), "current_spread_home": (a.get("current") or {}).get("spread_home"),
                     "spread_move": (a.get("movement") or {}).get("spread_points"), "key_numbers": ",".join(map(str, (a.get("movement") or {}).get("key_numbers_spread", []))),
                     "open_total": (a.get("open") or {}).get("total"), "current_total": (a.get("current") or {}).get("total"), "total_move": (a.get("movement") or {}).get("total_points"),
                     "steam": None if not a.get("steam") else a["steam"]["direction"], "book_disagreement": a.get("book_disagreement_spread"),
                     "market_wp_home": (a.get("implied") or {}).get("home_win_prob_no_vig"),
                     "model_spread_diff": (a.get("model_vs_market") or {}).get("spread_diff"), "model_total_diff": (a.get("model_vs_market") or {}).get("total_diff"),
                     "generated_at": now.isoformat()})
    return pd.DataFrame(rows), payloads
