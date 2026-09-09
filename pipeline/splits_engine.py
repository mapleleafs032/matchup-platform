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
