"""
Betting-splits analysis (master prompt §23, §25). Runs only on splits we actually hold; with no splits
for a game the output says so rather than implying anything.

Per game and period it produces:
  series       every snapshot: ticket % and money % (home/over side) for spread, total, moneyline,
               with the line in force at that moment, so the chart can show splits and line together
  latest       the most recent snapshot per market
  divergence   ticket % minus money % in percentage points; large gaps mean a few big bets lean the
               other way from the crowd. Reported as evidence, never as "sharp money" (§25).
  rlm          reverse line movement: the line moved TOWARD the side holding the minority of tickets.
               This is the one inference the master prompt allows, and only with real ticket data.
  notes        short factual sentences for the UI and the AI package
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from pipeline import storage

MARKETS = ("spread", "total", "moneyline")
SPLITS = config.TABLES / "market" / "splits"


def load(league: str, season: int, week: int) -> pd.DataFrame:
    p = SPLITS / league / str(season) / f"W{week:02d}.csv"
    if not p.exists():
        return pd.DataFrame()
    d = pd.read_csv(p)
    d["retrieved_at"] = pd.to_datetime(d.retrieved_at, utc=True, errors="coerce")
    return d.dropna(subset=["retrieved_at"]).sort_values("retrieved_at")


def _side_label(market: str, pct_home: float | None, home_abbr: str, away_abbr: str) -> str | None:
    if pct_home is None or pd.isna(pct_home):
        return None
    if market == "total":
        return "the over" if pct_home >= 0.5 else "the under"
    return home_abbr if pct_home >= 0.5 else away_abbr


def analyze_game(hist: pd.DataFrame, period: str, home_abbr: str, away_abbr: str, kickoff: pd.Timestamp | None) -> dict:
    h = hist[hist.period == period]
    if kickoff is not None:
        h = h[h.retrieved_at < kickoff]
    if h.empty:
        return {"available": False, "period": period, "notes": [], "series": [], "latest": {}, "divergence": {}, "rlm": {}}
    series = []
    for _, r in h.iterrows():
        row = {"t": r.retrieved_at.isoformat(), "book": r.book,
               "line_spread_home": None if pd.isna(r.get("line_spread_home")) else float(r.line_spread_home),
               "line_total": None if pd.isna(r.get("line_total")) else float(r.line_total)}
        for m in MARKETS:
            for k in ("ticket", "money"):
                c = f"{m}_{k}_pct_home"
                row[f"{m}_{k}"] = None if c not in r or pd.isna(r[c]) else round(float(r[c]), 4)
        series.append(row)
    last = h.iloc[-1]
    first = h.iloc[0]
    latest, divergence, notes = {}, {}, []
    for m in MARKETS:
        tc, mc = f"{m}_ticket_pct_home", f"{m}_money_pct_home"
        t = None if tc not in last or pd.isna(last[tc]) else float(last[tc])
        mo = None if mc not in last or pd.isna(last[mc]) else float(last[mc])
        latest[m] = {"ticket_pct_home": t, "money_pct_home": mo,
                     "ticket_side": _side_label(m, t, home_abbr, away_abbr), "money_side": _side_label(m, mo, home_abbr, away_abbr)}
        if t is not None and mo is not None:
            gap = round((t - mo) * 100, 1)
            divergence[m] = {"points": gap, "notable": abs(gap) >= config.SPLITS_DIVERGENCE_PTS}
            if abs(gap) >= config.SPLITS_DIVERGENCE_PTS:
                crowd = _side_label(m, t, home_abbr, away_abbr); money = _side_label(m, mo, home_abbr, away_abbr)
                if crowd != money:
                    notes.append(f"On the {m}, {t*100:.0f}% of tickets are on {crowd} but only {mo*100:.0f}% of the money is — the dollars lean {money}.")
                else:
                    notes.append(f"On the {m}, tickets ({t*100:.0f}%) and money ({mo*100:.0f}%) are on {crowd} but differ by {abs(gap):.0f} points, so bet sizes are uneven.")
    # reverse line movement: line moved toward the minority-ticket side
    rlm = {}
    for m, line_col in (("spread", "line_spread_home"), ("total", "line_total")):
        tc = f"{m}_ticket_pct_home"
        if line_col not in h.columns or tc not in h.columns:
            continue
        lines = h[h[line_col].notna()]
        tickets = h[h[tc].notna()]
        if len(lines) < 2 or tickets.empty:
            continue
        move = float(lines[line_col].iloc[-1] - lines[line_col].iloc[0])
        t_now = float(tickets[tc].iloc[-1])
        if abs(move) < config.RLM_MIN_MOVE or abs(t_now - 0.5) < (config.RLM_MIN_TICKET_PCT - 0.5):
            continue
        majority_home = t_now >= 0.5
        # spread: a more negative home number means the line moved toward the home team.
        # total: a higher number means the line moved toward the over.
        moved_home = (move < 0) if m == "spread" else (move > 0)
        if moved_home != majority_home:
            crowd = _side_label(m, t_now, home_abbr, away_abbr)
            toward = _side_label(m, 1.0 if moved_home else 0.0, home_abbr, away_abbr)
            rlm[m] = {"line_move": round(move, 1), "ticket_pct_majority": round(t_now if majority_home else 1 - t_now, 3),
                      "crowd_side": crowd, "line_moved_toward": toward}
            notes.append(f"The {m} moved {abs(move):.1f} toward {toward} while {max(t_now, 1-t_now)*100:.0f}% of tickets sat on {crowd} — movement against the ticket majority.")
    if not notes:
        notes.append("Tickets and money are broadly aligned and the line has not moved against the crowd.")
    notes.append(f"Splits from {last.book} ({len(h)} snapshot{'s' if len(h) != 1 else ''} since {first.retrieved_at.strftime('%b %d %H:%M')} UTC), {'full game' if period == 'FULL' else 'first half'}.")
    return {"available": True, "period": period, "book": last.book, "n_snapshots": int(len(h)),
            "first_snapshot": first.retrieved_at.isoformat(), "last_snapshot": last.retrieved_at.isoformat(),
            "series": series, "latest": latest, "divergence": divergence, "rlm": rlm, "notes": notes}


def build_week(league: str, season: int, week: int, games: pd.DataFrame, teams: pd.DataFrame) -> dict[str, dict]:
    hist = load(league, season, week)
    out: dict[str, dict] = {}
    wk = games[(games.week == week) & (games.season_type == "REG")]
    for _, g in wk.iterrows():
        gh = hist[hist.game_id == g.game_id] if not hist.empty else pd.DataFrame()
        ha = teams.abbr.get(g.home_team_id, g.home_team_id.split("_")[-1]) if not teams.empty else g.home_team_id.split("_")[-1]
        aa = teams.abbr.get(g.away_team_id, g.away_team_id.split("_")[-1]) if not teams.empty else g.away_team_id.split("_")[-1]
        kick = pd.Timestamp(g.kickoff_utc) if pd.notna(g.kickoff_utc) else None
        periods = {p: analyze_game(gh, p, ha, aa, kick) if not gh.empty else {"available": False, "period": p, "notes": [], "series": [], "latest": {}, "divergence": {}, "rlm": {}}
                   for p in config.SPLITS_PERIODS}
        out[g.game_id] = {"game_id": g.game_id, "home_abbr": ha, "away_abbr": aa, "periods": periods,
                          "any_available": any(v["available"] for v in periods.values())}
    return out

# ---- market events over time ---------------------------------------------------------------------
def _dir_label(market: str, toward_home: bool, home_abbr: str, away_abbr: str) -> str:
    if market == "total":
        return "the over" if toward_home else "the under"
    return home_abbr if toward_home else away_abbr


def detect_events(hist: pd.DataFrame, period: str, home_abbr: str, away_abbr: str,
                  kickoff: pd.Timestamp | None = None) -> list[dict]:
    """
    Walk the snapshot history and record what happened, when. Each event carries a timestamp so the
    charts can mark it and the gates can ask "is this happening NOW?" rather than "did it ever happen?".

    Events: rlm (the number moved toward the minority-ticket side), steam (a fast move), lopsided
    (a side crossed the threshold on both tickets and money), key_number (the spread crossed 3 or 7).
    """
    h = hist[hist.period == period].sort_values("retrieved_at")
    if kickoff is not None:
        h = h[h.retrieved_at < kickoff]
    if len(h) < 2:
        return []
    out: list[dict] = []
    prev_lop = {}
    for (mkt, line_col, is_total) in (("spread", "line_spread_home", False), ("total", "line_total", True)):
        tc, mc = f"{mkt}_ticket_pct_home", f"{mkt}_money_pct_home"
        if line_col not in h.columns or tc not in h.columns:
            continue
        rows = h[h[line_col].notna()]
        for i in range(1, len(rows)):
            a, b = rows.iloc[i - 1], rows.iloc[i]
            delta = float(b[line_col]) - float(a[line_col])
            if abs(delta) < config.RLM_MIN_MOVE:
                continue
            toward_home = (delta > 0) if is_total else (delta < 0)
            tp = b[tc] if pd.notna(b[tc]) else a[tc]
            hours = max((b.retrieved_at - a.retrieved_at).total_seconds() / 3600.0, 0.01)
            if pd.notna(tp) and abs(float(tp) - 0.5) >= (config.RLM_MIN_TICKET_PCT - 0.5):
                majority_home = float(tp) >= 0.5
                if toward_home != majority_home:
                    out.append({"t": b.retrieved_at.isoformat(), "kind": "rlm", "market": mkt,
                                "toward": _dir_label(mkt, toward_home, home_abbr, away_abbr), "toward_home": bool(toward_home),
                                "move": round(delta, 1), "ticket_pct_majority": round(max(float(tp), 1 - float(tp)), 3),
                                "detail": f"{mkt} moved {abs(delta):.1f} toward {_dir_label(mkt, toward_home, home_abbr, away_abbr)} "
                                          f"while {max(float(tp), 1-float(tp))*100:.0f}% of tickets sat the other way"})
            is_steam = abs(delta) >= config.STEAM_MIN_MOVE and hours <= config.STEAM_WINDOW_HOURS
            is_rlm = any(e["kind"] == "rlm" and e["t"] == b.retrieved_at.isoformat() and e["market"] == mkt for e in out)
            if not is_steam and not is_rlm:
                out.append({"t": b.retrieved_at.isoformat(), "kind": "line_move", "market": mkt,
                            "toward": _dir_label(mkt, toward_home, home_abbr, away_abbr), "toward_home": bool(toward_home),
                            "move": round(delta, 1),
                            "detail": f"{mkt} moved {abs(delta):.1f} toward {_dir_label(mkt, toward_home, home_abbr, away_abbr)}"})
            if is_steam:
                out.append({"t": b.retrieved_at.isoformat(), "kind": "steam", "market": mkt,
                            "toward": _dir_label(mkt, toward_home, home_abbr, away_abbr), "toward_home": bool(toward_home),
                            "move": round(delta, 1),
                            "detail": f"{mkt} moved {abs(delta):.1f} toward {_dir_label(mkt, toward_home, home_abbr, away_abbr)} within {hours:.1f}h"})
            if not is_total:
                for k in (3, 7, 10, 14):
                    if (abs(float(a[line_col])) < k) != (abs(float(b[line_col])) < k):
                        out.append({"t": b.retrieved_at.isoformat(), "kind": "key_number", "market": mkt, "toward": None,
                                    "move": round(delta, 1), "key": k,
                                    "detail": f"spread crossed {k} ({a[line_col]:+.1f} to {b[line_col]:+.1f})"})
        prev_div = False
        for i in range(len(rows)):
            r = rows.iloc[i]
            if pd.notna(r.get(tc)) and pd.notna(r.get(mc)):
                gap = abs(float(r[tc]) - float(r[mc])) * 100
                now_div = gap >= config.SPLITS_DIVERGENCE_PTS
                if now_div and not prev_div:
                    crowd = _dir_label(mkt, float(r[tc]) >= 0.5, home_abbr, away_abbr)
                    money = _dir_label(mkt, float(r[mc]) >= 0.5, home_abbr, away_abbr)
                    out.append({"t": r.retrieved_at.isoformat(), "kind": "divergence", "market": mkt,
                                "toward": money, "toward_home": float(r[mc]) >= 0.5, "move": None,
                                "detail": (f"tickets and money split by {gap:.0f} points"
                                           + (f": tickets on {crowd}, money on {money}" if crowd != money else f", both on {crowd}"))})
                prev_div = now_div
            side = None
            if pd.notna(r.get(tc)) and pd.notna(r.get(mc)):
                side = lopsided(float(r[tc]), float(r[mc]))
            if side and prev_lop.get(mkt) != side:
                who = _dir_label(mkt, side == "home", home_abbr, away_abbr)
                out.append({"t": r.retrieved_at.isoformat(), "kind": "lopsided", "market": mkt, "toward": who,
                            "toward_home": side == "home", "move": None,
                            "detail": f"{who} crossed {config.PICK_GATES['lopsided_threshold']*100:.0f}% of both tickets and money"})
            prev_lop[mkt] = side
    return sorted(out, key=lambda e: (e["t"], e["kind"]))


def lopsided(ticket_pct_home: float, money_pct_home: float) -> str | None:
    th = config.PICK_GATES["lopsided_threshold"]
    if ticket_pct_home >= th and money_pct_home >= th:
        return "home"
    if (1 - ticket_pct_home) >= th and (1 - money_pct_home) >= th:
        return "away"
    return None


def current_state(hist: pd.DataFrame, period: str, home_abbr: str, away_abbr: str,
                  kickoff: pd.Timestamp | None = None, window_hours: float | None = None) -> dict:
    """
    What is true RIGHT NOW, judged over a recent window rather than over the whole week.
    A line that moved against a side on Tuesday and came back by Friday is NOT currently in reverse movement.
    """
    window_hours = window_hours or config.MARKET_STATE_WINDOW_HOURS
    events = detect_events(hist, period, home_abbr, away_abbr, kickoff)
    h = hist[hist.period == period].sort_values("retrieved_at")
    if kickoff is not None:
        h = h[h.retrieved_at < kickoff]
    if h.empty:
        return {"rlm_active": {}, "rlm_ever": {}, "lopsided": {}, "recent_move": {}, "window_hours": window_hours, "events": events}
    last_t = h.retrieved_at.max()
    cutoff = last_t - pd.Timedelta(hours=window_hours)
    recent = h[h.retrieved_at >= cutoff]
    state = {"rlm_active": {}, "rlm_ever": {}, "lopsided": {}, "recent_move": {}, "window_hours": window_hours, "events": events}
    for mkt, line_col, is_total in (("spread", "line_spread_home", False), ("total", "line_total", True)):
        ever = [e for e in events if e["kind"] == "rlm" and e["market"] == mkt]
        state["rlm_ever"][mkt] = ever[-1] if ever else None
        rows = recent[recent[line_col].notna()] if line_col in recent.columns else recent.iloc[0:0]
        if len(rows) >= 2:
            delta = float(rows[line_col].iloc[-1]) - float(rows[line_col].iloc[0])
            state["recent_move"][mkt] = round(delta, 2)
            tc = f"{mkt}_ticket_pct_home"
            tp = rows[tc].dropna()
            if abs(delta) >= config.RLM_MIN_MOVE and len(tp):
                toward_home = (delta > 0) if is_total else (delta < 0)
                t_now = float(tp.iloc[-1])
                if abs(t_now - 0.5) >= (config.RLM_MIN_TICKET_PCT - 0.5) and toward_home != (t_now >= 0.5):
                    state["rlm_active"][mkt] = {"toward": _dir_label(mkt, toward_home, home_abbr, away_abbr),
                                                "toward_home": bool(toward_home), "move": round(delta, 2),
                                                "ticket_pct_majority": round(max(t_now, 1 - t_now), 3)}
        else:
            state["recent_move"][mkt] = None
        last = h.iloc[-1]
        tc, mc = f"{mkt}_ticket_pct_home", f"{mkt}_money_pct_home"
        if tc in last and mc in last and pd.notna(last[tc]) and pd.notna(last[mc]):
            side = lopsided(float(last[tc]), float(last[mc]))
            state["lopsided"][mkt] = None if side is None else _dir_label(mkt, side == "home", home_abbr, away_abbr)
    for mkt in ("moneyline",):
        tc, mc = f"{mkt}_ticket_pct_home", f"{mkt}_money_pct_home"
        last = h.iloc[-1]
        if tc in last and mc in last and pd.notna(last[tc]) and pd.notna(last[mc]):
            side = lopsided(float(last[tc]), float(last[mc]))
            state["lopsided"][mkt] = None if side is None else (home_abbr if side == "home" else away_abbr)
    return state
