"""
Play generation and tiering (extends §25, §34, §66).

A "pick" is a side the model prefers to the market: a spread side, a total side, or a moneyline.
Nothing here invents a number. Every input comes from tables built earlier:

    model      predictions (projected margin, total, win probability, data quality)
    market     market_analysis / market snapshots (current line, opening line, movement, key numbers)
    splits     betting splits (ticket %, money %, divergence, reverse line movement)

Scoring is transparent and additive, in "points of edge equivalent":

    base        model-vs-market disagreement (points for spread/total; EV-converted for moneyline)
    quality     scaled by the prediction's data-quality score, so thin early-season games score lower
    signals     small bonuses when independent market evidence agrees with the model's side:
                  line moved toward our side against the ticket majority (RLM)
                  the money share leans our way while tickets lean the other
                  the current number sits on the good side of a key number

Tiers are cut on that score, and each tier is stamped with the hit rate plays like it ACTUALLY achieved
in the backtest (calibrate()). Break-even at -110 pricing is 52.38%; the tab shows it next to every rate.
The tiering is a ranking for research. It is not a claim of profitability, and where the measured rate is
below break-even the payload says so explicitly.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from pipeline import storage

AN = config.TABLES / "analytics"
MODEL = config.TABLES / "model"
BREAK_EVEN = 0.5238            # -110 both sides
MARKETS = ("SPREAD", "TOTAL", "MONEYLINE")


# ---- helpers ---------------------------------------------------------------------------------
def american_to_prob(ml: float | None) -> float | None:
    if ml is None or pd.isna(ml):
        return None
    ml = float(ml)
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def american_profit(ml: float) -> float:
    """Profit per 1 unit staked."""
    return float(ml) / 100 if ml > 0 else 100 / -float(ml)


def _key_number_side(spread_home: float | None, side_home: bool) -> str | None:
    """Whether our side sits on the favourable side of 3 or 7 (getting more than, or laying less than)."""
    if spread_home is None or pd.isna(spread_home):
        return None
    line_for_side = spread_home if side_home else -spread_home
    for k in (7, 3):                       # report the HIGHEST key number cleared, not the first one checked
        if line_for_side > k:
            return f"getting more than {k}"
    return None


# ---- candidate generation ---------------------------------------------------------------------
def candidates(league: str, season: int, week: int) -> pd.DataFrame:
    games = storage.read_table(storage.games_path(league, season))
    if games.empty:
        return pd.DataFrame()
    wk = games[(games.week == week) & (games.season_type == "REG") & (games.status == "SCHEDULED")]
    wk = wk[~wk.home_team_id.str.startswith("CFB_FCS") & ~wk.away_team_id.str.startswith("CFB_FCS")]
    if wk.empty:
        return pd.DataFrame()
    preds = storage.read_table(MODEL / "predictions" / league / f"{season}.csv")
    if preds.empty:
        return pd.DataFrame()
    preds = preds.sort_values("predicted_at").drop_duplicates("game_id", keep="last").set_index("game_id")
    mkt = storage.read_table(AN / "market_analysis" / league / str(season) / f"W{week:02d}.parquet")
    mkt = mkt.set_index("game_id") if not mkt.empty else mkt
    teams = storage.read_table(config.TABLES / "ref" / "teams.parquet")
    teams = teams.set_index("team_id") if not teams.empty else teams
    sp_path = config.TABLES / "market" / "splits" / league / str(season) / f"W{week:02d}.csv"
    splits = pd.read_csv(sp_path) if sp_path.exists() else pd.DataFrame()
    if not splits.empty:
        splits["retrieved_at"] = pd.to_datetime(splits.retrieved_at, utc=True, errors="coerce")
        splits = splits[splits.period == "FULL"].sort_values("retrieved_at")
    now = pd.Timestamp.now(tz="UTC")
    rows = []
    for _, g in wk.iterrows():
        gid = g.game_id
        if gid not in preds.index:
            continue
        p = preds.loc[gid]
        if pd.notna(g.kickoff_utc) and pd.Timestamp(g.kickoff_utc) <= now:
            continue
        m = mkt.loc[gid] if not mkt.empty and gid in mkt.index else None
        sg = splits[splits.game_id == gid] if not splits.empty else pd.DataFrame()
        last_split = sg.iloc[-1] if not sg.empty else None
        first_split = sg.iloc[0] if not sg.empty else None
        abbr = lambda t: (teams.abbr.get(t, t.split("_")[-1]) if not teams.empty else t.split("_")[-1])
        base = {"game_id": gid, "league": league, "season": season, "week": week, "kickoff_utc": g.kickoff_utc,
                "home_team_id": g.home_team_id, "away_team_id": g.away_team_id, "home": abbr(g.home_team_id), "away": abbr(g.away_team_id),
                "model_version": p.model_version, "data_quality": float(p.data_quality),
                "market_spread_home": None if m is None else _f(m.current_spread_home), "market_total": None if m is None else _f(m.current_total),
                "open_spread_home": None if m is None else _f(m.open_spread_home), "open_total": None if m is None else _f(m.open_total),
                "spread_move": None if m is None else _f(m.spread_move), "key_numbers": None if m is None else (m.key_numbers or None),
                "book": None if m is None else m.primary_book,
                "proj_margin_home": float(p.proj_margin_home), "proj_total": float(p.proj_total), "win_prob_home": float(p.win_prob_home)}
        rows += _spread_play(base, p, last_split, first_split)
        rows += _total_play(base, p, last_split, first_split)
        rows += _moneyline_play(base, p, gid, league, season, week, last_split)
    return pd.DataFrame([r for r in rows if r])


def _f(x):
    return None if x is None or (isinstance(x, float) and pd.isna(x)) or (hasattr(x, "__float__") and pd.isna(x)) else float(x)


def _split_val(row, col):
    if row is None or col not in row or pd.isna(row[col]):
        return None
    return float(row[col])


def _spread_play(base, p, last_split, first_split) -> list[dict]:
    sh = base["market_spread_home"]
    if sh is None:
        return []
    market_margin = -sh
    diff = base["proj_margin_home"] - market_margin
    if abs(diff) < config.PICK_MIN_EDGE[base["league"]]["SPREAD"]:
        return []
    side_home = diff > 0
    side = base["home"] if side_home else base["away"]
    line_for_side = sh if side_home else -sh
    signals, notes = [], []
    tp = _split_val(last_split, "spread_ticket_pct_home")
    mp = _split_val(last_split, "spread_money_pct_home")
    if tp is not None:
        our_tickets = tp if side_home else 1 - tp
        if mp is not None:
            our_money = mp if side_home else 1 - mp
            if our_money - our_tickets >= config.PICK_SIGNAL["money_divergence"]:
                signals.append("money_agrees")
                notes.append(f"{our_money*100:.0f}% of money on {side} against {our_tickets*100:.0f}% of tickets.")
        move = base["spread_move"]
        if move is not None and abs(move) >= config.RLM_MIN_MOVE:
            moved_home = move < 0
            majority_home = tp >= 0.5
            if moved_home == side_home and majority_home != side_home and abs(tp - 0.5) >= (config.RLM_MIN_TICKET_PCT - 0.5):
                signals.append("rlm_agrees")
                notes.append(f"The line moved {abs(move):.1f} toward {side} while {max(tp,1-tp)*100:.0f}% of tickets sat on the other side.")
    kn = _key_number_side(sh, side_home)
    if kn:
        signals.append("key_number"); notes.append(f"{side} is {kn}.")
    return [{**base, "market": "SPREAD", "side": side, "side_is_home": side_home, "line": line_for_side,
             "price": -110, "edge_points": round(abs(diff), 2), "model_number": round(base["proj_margin_home"] if side_home else -base["proj_margin_home"], 2),
             "market_number": round(market_margin if side_home else -market_margin, 2),
             "tickets_pct_side": None if tp is None else round(tp if side_home else 1 - tp, 3),
             "money_pct_side": None if mp is None else round(mp if side_home else 1 - mp, 3),
             "signals": ",".join(signals), "signal_notes": " ".join(notes)}]


def _total_play(base, p, last_split, first_split) -> list[dict]:
    t = base["market_total"]
    if t is None:
        return []
    diff = base["proj_total"] - t
    if abs(diff) < config.PICK_MIN_EDGE[base["league"]]["TOTAL"]:
        return []
    over = diff > 0
    side = "Over" if over else "Under"
    signals, notes = [], []
    tp = _split_val(last_split, "total_ticket_pct_home")     # share on the OVER
    mp = _split_val(last_split, "total_money_pct_home")
    if tp is not None and mp is not None:
        our_tickets = tp if over else 1 - tp
        our_money = mp if over else 1 - mp
        if our_money - our_tickets >= config.PICK_SIGNAL["money_divergence"]:
            signals.append("money_agrees")
            notes.append(f"{our_money*100:.0f}% of money on the {side.lower()} against {our_tickets*100:.0f}% of tickets.")
    if base["open_total"] is not None:
        move = t - base["open_total"]
        if abs(move) >= 1.0 and ((move > 0) == over):
            signals.append("line_agrees"); notes.append(f"The total has moved {abs(move):.1f} toward the {side.lower()} since opening.")
    return [{**base, "market": "TOTAL", "side": side, "side_is_home": None, "line": t, "price": -110,
             "edge_points": round(abs(diff), 2), "model_number": round(base["proj_total"], 2), "market_number": round(t, 2),
             "tickets_pct_side": None if tp is None else round(tp if over else 1 - tp, 3),
             "money_pct_side": None if mp is None else round(mp if over else 1 - mp, 3),
             "signals": ",".join(signals), "signal_notes": " ".join(notes)}]


def _moneyline_play(base, p, gid, league, season, week, last_split) -> list[dict]:
    mp_path = config.SITE_JSON / "market" / f"{gid}.json"
    ml_home = ml_away = None
    if mp_path.exists():
        try:
            cur = (json.loads(mp_path.read_text()).get("current") or {})
            ml_home, ml_away = cur.get("ml_home"), cur.get("ml_away")
        except Exception:
            pass
    if ml_home is None or ml_away is None:
        return []
    wp = base["win_prob_home"]
    out = []
    for side_home, ml, wp_side in ((True, ml_home, wp), (False, ml_away, 1 - wp)):
        implied = american_to_prob(ml)
        if implied is None:
            continue
        ev = wp_side * american_profit(ml) - (1 - wp_side)
        if ev < config.PICK_MIN_EDGE[league]["MONEYLINE_EV"]:
            continue
        side = base["home"] if side_home else base["away"]
        tp = _split_val(last_split, "moneyline_ticket_pct_home")
        mpc = _split_val(last_split, "moneyline_money_pct_home")
        signals, notes = [], []
        if tp is not None and mpc is not None:
            our_t = tp if side_home else 1 - tp
            our_m = mpc if side_home else 1 - mpc
            if our_m - our_t >= config.PICK_SIGNAL["money_divergence"]:
                signals.append("money_agrees"); notes.append(f"{our_m*100:.0f}% of moneyline money on {side} against {our_t*100:.0f}% of tickets.")
        out.append({**base, "market": "MONEYLINE", "side": side, "side_is_home": side_home, "line": float(ml), "price": float(ml),
                    "edge_points": round(ev * config.PICK_EV_TO_POINTS, 2), "model_number": round(wp_side, 4), "market_number": round(implied, 4),
                    "expected_value": round(ev, 4), "no_vig_prob": None,
                    "tickets_pct_side": None if tp is None else round(tp if side_home else 1 - tp, 3),
                    "money_pct_side": None if mpc is None else round(mpc if side_home else 1 - mpc, 3),
                    "signals": ",".join(signals), "signal_notes": " ".join(notes)})
    return out


# ---- scoring and tiering -------------------------------------------------------------------------
def score(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    d = df.copy()
    cap = d.market.map(lambda m: config.PICK_EDGE_CAP.get(m, 10.0))
    d["score_base"] = np.minimum(d.edge_points, cap)
    d["score_quality"] = d.data_quality.clip(0.3, 1.0)
    bonus = d.signals.fillna("").map(lambda s: sum(config.PICK_SIGNAL_BONUS.get(x, 0.0) for x in s.split(",") if x))
    d["score_signals"] = bonus
    d["score"] = (d.score_base * d.score_quality + d.score_signals).round(3)
    return d


def calibrate(league: str) -> dict:
    """
    Measured hit rate for each tier, from the walk-forward backtest (out-of-sample seasons only) plus any
    graded live picks. Historical rows carry no splits, so the reconstruction uses the model-edge component
    alone; that limitation is reported in the payload.
    """
    out = {"source": "backtest", "note": "", "tiers": {}, "break_even": BREAK_EVEN}
    ev_path = MODEL / "backtest" / league / f"evaluation_{league}_v1.0.csv"
    if not ev_path.exists():
        out["note"] = "No backtest evaluation found; tiers are unmeasured."
        return out
    ev = pd.read_csv(ev_path)
    if "in_sample_warning" in ev.columns:
        ev = ev[~ev.in_sample_warning.astype(bool)]
    elif "season" in ev.columns:
        ev = ev[ev.season != ev.season.min()]
    ev = ev[ev.model_ats_result.isin(["WIN", "LOSS"]) & ev.edge_vs_market.notna()]
    if ev.empty:
        out["note"] = "No graded out-of-sample spread results; tiers are unmeasured."
        return out
    cap = config.PICK_EDGE_CAP["SPREAD"]
    q = ev.data_quality.clip(0.3, 1.0) if "data_quality" in ev.columns else 1.0
    ev = ev.assign(_score=(np.minimum(ev.edge_vs_market, cap) * q).round(3))
    for tier, lo in config.PICK_TIERS.items():
        hi = config.PICK_TIER_UPPER.get(tier)
        b = ev[(ev._score >= lo) & ((ev._score < hi) if hi else True)]
        if len(b) >= config.PICK_MIN_CALIBRATION_N:
            rate = float((b.model_ats_result == "WIN").mean())
            out["tiers"][tier] = {"n": int(len(b)), "hit_rate": round(rate, 4), "beats_break_even": rate > BREAK_EVEN,
                                  "seasons": sorted(b.season.unique().tolist()) if "season" in b.columns else []}
        else:
            out["tiers"][tier] = {"n": int(len(b)), "hit_rate": None, "beats_break_even": None, "seasons": []}
    live = MODEL / "picks_evaluation" / league / f"{config.SEASON}.csv"
    if live.exists():
        lv = pd.read_csv(live)
        lv = lv[lv.result.isin(["WIN", "LOSS"])]
        if not lv.empty:
            out["live"] = {t: {"n": int(len(g)), "hit_rate": round(float((g.result == "WIN").mean()), 4)}
                           for t, g in lv.groupby("tier")}
            out["live_total"] = {"n": int(len(lv)), "hit_rate": round(float((lv.result == "WIN").mean()), 4)}
    out["note"] = ("Measured on spread plays from the walk-forward backtest, using the model-edge component only "
                   "(betting splits do not exist for past seasons, so signal bonuses are not reflected here).")
    return out


def assign_tiers(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    d = df.copy()
    def tier_of(s):
        for t, lo in sorted(config.PICK_TIERS.items(), key=lambda kv: -kv[1]):
            if s >= lo:
                return t
        return None
    d["tier"] = d.score.map(tier_of)
    return d[d.tier.notna()].sort_values(["score"], ascending=False)


def build_week(league: str, season: int, week: int) -> tuple[pd.DataFrame, dict]:
    c = candidates(league, season, week)
    if c.empty:
        return c, calibrate(league)
    picks = assign_tiers(score(c))
    picks["built_at"] = datetime.now(timezone.utc).isoformat()
    picks["pick_id"] = (picks.game_id + "_" + picks.market + "_" + picks.side.astype(str).str.replace(" ", "") + "_" + picks.model_version)
    return picks, calibrate(league)
