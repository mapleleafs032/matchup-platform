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
from pipeline import storage, splits_engine

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



# ---- gates -------------------------------------------------------------------------------------
def rlm_state(line_move: float | None, ticket_pct_home: float | None, is_total: bool = False) -> str | None:
    """
    Reverse line movement: the number moved toward the side holding the MINORITY of tickets.
    Returns "toward_home"/"toward_away" (or "toward_over"/"toward_under" for totals), else None.
    """
    if line_move is None or ticket_pct_home is None or pd.isna(line_move) or pd.isna(ticket_pct_home):
        return None
    if abs(line_move) < config.RLM_MIN_MOVE or abs(ticket_pct_home - 0.5) < (config.RLM_MIN_TICKET_PCT - 0.5):
        return None
    moved_home = (line_move > 0) if is_total else (line_move < 0)   # totals: up = toward the over
    majority_home = ticket_pct_home >= 0.5
    if moved_home == majority_home:
        return None                                                  # the line followed the crowd: not RLM
    return ("toward_over" if moved_home else "toward_under") if is_total else ("toward_home" if moved_home else "toward_away")


def lopsided_side(ticket_pct_home: float | None, money_pct_home: float | None, threshold: float) -> str | None:
    """A side holding at least `threshold` of BOTH tickets and money. Returns "home"/"away" or None."""
    if ticket_pct_home is None or money_pct_home is None or pd.isna(ticket_pct_home) or pd.isna(money_pct_home):
        return None
    if ticket_pct_home >= threshold and money_pct_home >= threshold:
        return "home"
    if (1 - ticket_pct_home) >= threshold and (1 - money_pct_home) >= threshold:
        return "away"
    return None


def marquee(league: str, g, teams: pd.DataFrame, ranks: dict, rating_pct: dict, has_moneyline: bool) -> tuple[bool, str]:
    """
    Is this a game that draws real betting volume? NFL always qualifies. College needs a reason:
    both teams from power conferences, a ranked team involved, or both teams rating out respectably --
    and a posted moneyline, which books only offer on games they will take size on.
    """
    if league == "NFL":
        return True, "NFL"
    cfg = config.PICK_MARQUEE
    h, a = g.home_team_id, g.away_team_id
    if cfg["require_moneyline_posted"] and not has_moneyline:
        return False, "no moneyline posted (thin market)"
    if cfg["ranked_always_qualifies"] and (ranks.get(h) or ranks.get(a)):
        return True, "ranked team involved"
    ok_conf = set(cfg["power_conferences"]) | set(cfg["independents_ok"])
    conf = {}
    if not teams.empty:
        for t in (h, a):
            conf[t] = teams.conference.get(t)
    if conf.get(h) in ok_conf and conf.get(a) in ok_conf:
        return True, f"{conf.get(h)} vs {conf.get(a)}"
    ph, pa = rating_pct.get(h), rating_pct.get(a)
    if ph is not None and pa is not None and min(ph, pa) >= cfg["min_rating_pct"]:
        return True, "both teams rate respectably"
    weak = [t for t, p in ((h, ph), (a, pa)) if p is not None and p < cfg["min_rating_pct"]]
    if weak:
        return False, "low-rated team in a low-volume matchup"
    return False, f"non-power matchup ({conf.get(a)} at {conf.get(h)})"


def apply_gates(play: dict, ctx: dict) -> list[str]:
    """Return the list of reasons this play is not a play. Empty list means it survives."""
    G = config.PICK_GATES
    reasons = []
    if G["require_marquee"] and not ctx["marquee_ok"]:
        reasons.append(f"low-volume game: {ctx['marquee_why']}")
    if G["require_splits"] and play.get("tickets_pct_side") is None and play.get("money_pct_side") is None:
        reasons.append("no betting splits recorded, so market agreement cannot be checked")
        return reasons                                   # nothing below can be evaluated without splits
    if G["veto_lopsided"]:
        side = play.get("_lopsided_side")
        if side:
            who = ctx["home"] if side == "home" else ctx["away"]
            if play["market"] == "TOTAL":
                who = "the over" if side == "home" else "the under"
            reasons.append(f"lopsided support: {who} holds at least {G['lopsided_threshold']*100:.0f}% of both tickets and money")
    if G["veto_rlm"]:
        st = play.get("_rlm")
        if st:
            against_us = play.get("_rlm_against_us")
            if (not G["rlm_only_against_us"]) or against_us:
                reasons.append("reverse line movement in this market" + (" against our side" if against_us else ""))
    mv = play.get("_move_against")
    if mv is not None and mv > G["max_line_move_against"]:
        reasons.append(f"the number has moved {mv:.1f} against this side since opening")
    return reasons


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
    ranks = {}
    if league == "CFB":
        rk = storage.read_table(config.TABLES / "context" / "rankings" / f"{season}.parquet")
        if not rk.empty:
            ap = rk[rk.poll == "AP"]
            ap = ap[ap.week == ap.week.max()]
            ranks = dict(zip(ap.team_id, ap["rank"].astype(int)))
    rat = storage.read_table(AN / "team_ratings" / league / f"{season}.parquet")
    rating_pct = {}
    if not rat.empty:
        cur = rat[rat.as_of_week == rat.as_of_week.max()]
        if not cur.empty:
            rating_pct = dict(zip(cur.team_id, cur.rating_overall.rank(pct=True)))
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
        abbr0 = lambda t: (teams.abbr.get(t, t.split("_")[-1]) if not teams.empty else t.split("_")[-1])
        kick_ts = pd.Timestamp(g.kickoff_utc) if pd.notna(g.kickoff_utc) else None
        mstate = (splits_engine.current_state(sg, "FULL", abbr0(g.home_team_id), abbr0(g.away_team_id), kick_ts)
                  if not sg.empty else {"rlm_active": {}, "rlm_ever": {}, "lopsided": {}, "recent_move": {}, "events": []})
        abbr = lambda t: (teams.abbr.get(t, t.split("_")[-1]) if not teams.empty else t.split("_")[-1])
        base = {"game_id": gid, "league": league, "season": season, "week": week, "kickoff_utc": g.kickoff_utc,
                "home_team_id": g.home_team_id, "away_team_id": g.away_team_id, "home": abbr(g.home_team_id), "away": abbr(g.away_team_id),
                "model_version": p.model_version, "data_quality": float(p.data_quality),
                "market_spread_home": None if m is None else _f(m.current_spread_home), "market_total": None if m is None else _f(m.current_total),
                "open_spread_home": None if m is None else _f(m.open_spread_home), "open_total": None if m is None else _f(m.open_total),
                "spread_move": None if m is None else _f(m.spread_move), "key_numbers": None if m is None else (m.key_numbers or None),
                "book": None if m is None else m.primary_book,
                "proj_margin_home": float(p.proj_margin_home), "proj_total": float(p.proj_total), "win_prob_home": float(p.win_prob_home)}
        ml_present = False
        mkt_json = config.SITE_JSON / "market" / f"{gid}.json"
        if mkt_json.exists():
            try:
                cur = json.loads(mkt_json.read_text()).get("current") or {}
                ml_present = cur.get("ml_home") is not None and cur.get("ml_away") is not None
            except Exception:
                ml_present = False
        ok, why = marquee(league, g, teams, ranks, rating_pct, ml_present)
        ctx = {"marquee_ok": ok, "marquee_why": why, "home": base["home"], "away": base["away"]}
        base["_mstate"] = mstate
        plays = _spread_play(base, p, last_split, first_split) + _total_play(base, p, last_split, first_split) \
            + _moneyline_play(base, p, gid, league, season, week, last_split)
        base.pop("_mstate", None)
        for pl in plays:
            pl.pop("_mstate", None)
            pl["marquee_ok"] = ok
            pl["marquee_why"] = why
            reasons = apply_gates(pl, ctx)
            pl["veto_reasons"] = " | ".join(reasons)
            pl["qualified"] = not reasons
            for k in [k for k in pl if k.startswith("_")]:
                pl[k.lstrip("_")] = pl.pop(k)          # promote gate internals to reportable fields
        rows += plays
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
    ms = base.get("_mstate") or {}
    rlm_now = (ms.get("rlm_active") or {}).get("spread")
    rlm_past = (ms.get("rlm_ever") or {}).get("spread")
    rlm = None if not rlm_now else ("toward_home" if rlm_now["toward_home"] else "toward_away")
    lop = lopsided_side(tp, mp, config.PICK_GATES["lopsided_threshold"])
    move_against = None
    if base["open_spread_home"] is not None and sh is not None:
        moved = sh - base["open_spread_home"]                      # negative = toward the home team
        move_against = round(moved if side_home else -moved, 2)    # positive = against our side
    return [{**base, "market": "SPREAD",
             "_rlm": rlm, "_rlm_against_us": (rlm == ("toward_home" if not side_home else "toward_away")) if rlm else False,
             "_lopsided_side": lop, "_move_against": move_against, "side": side, "side_is_home": side_home, "line": line_for_side,
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
    move_against = None
    if base["open_total"] is not None:
        move = t - base["open_total"]
        if abs(move) >= 1.0 and ((move > 0) == over):
            signals.append("line_agrees"); notes.append(f"The total has moved {abs(move):.1f} toward the {side.lower()} since opening.")
        move_against = round(-move if over else move, 2)           # positive = against our side
    ms = base.get("_mstate") or {}
    rlm_now = (ms.get("rlm_active") or {}).get("total")
    rlm_past = (ms.get("rlm_ever") or {}).get("total")
    rlm = None if not rlm_now else ("toward_over" if rlm_now["toward_home"] else "toward_under")
    lop = lopsided_side(tp, mp, config.PICK_GATES["lopsided_threshold"])
    return [{**base, "market": "TOTAL",
             "_rlm": rlm, "_rlm_against_us": (rlm == ("toward_under" if over else "toward_over")) if rlm else False,
             "_lopsided_side": lop, "_move_against": move_against, "side": side, "side_is_home": None, "line": t, "price": -110,
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
        ms = base.get("_mstate") or {}
        rlm_now = (ms.get("rlm_active") or {}).get("spread")
        rlm = None if not rlm_now else ("toward_home" if rlm_now["toward_home"] else "toward_away")
        lop = lopsided_side(tp, mpc, config.PICK_GATES["lopsided_threshold"])
        out.append({**base, "market": "MONEYLINE",
                    "_rlm": rlm, "_rlm_against_us": (rlm == ("toward_home" if not side_home else "toward_away")) if rlm else False,
                    "_lopsided_side": lop, "_move_against": None,
                    "side": side, "side_is_home": side_home, "line": float(ml), "price": float(ml),
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


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a hit rate. On a few hundred plays this interval is wide, which is the point."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _historical_scores(league: str) -> pd.DataFrame:
    """Reconstruct the score each past spread play would have received, alongside its graded result."""
    ev_path = MODEL / "backtest" / league / f"evaluation_{league}_v1.0.csv"
    if not ev_path.exists():
        return pd.DataFrame()
    ev = pd.read_csv(ev_path)
    if "in_sample_warning" in ev.columns:
        ev = ev[~ev.in_sample_warning.astype(bool)]
    elif "season" in ev.columns:
        ev = ev[ev.season != ev.season.min()]
    ev = ev[ev.model_ats_result.isin(["WIN", "LOSS"]) & ev.edge_vs_market.notna()]
    if ev.empty:
        return ev
    ev = ev[ev.edge_vs_market >= config.PICK_MIN_EDGE[league]["SPREAD"]]   # only plays the engine would have made
    if ev.empty:
        return ev
    q = ev.data_quality.clip(0.3, 1.0) if "data_quality" in ev.columns else 1.0
    return ev.assign(_score=(np.minimum(ev.edge_vs_market, config.PICK_EDGE_CAP["SPREAD"]) * q).round(3),
                     _win=(ev.model_ats_result == "WIN").astype(int))


def calibrate(league: str) -> dict:
    """
    Measure how the score actually relates to winning, then let the data name the tiers.

    Past spread plays are split into score bands; each reports hit rate, sample size and a 95% interval.
    Tiers are assigned by measured performance, so A+ is the band that historically won most often -- not
    the band with the largest disagreement. Where no band's interval clears break-even, the payload says so.
    """
    out = {"source": "backtest", "note": "", "tiers": {}, "bands": [], "break_even": BREAK_EVEN,
           "tier_basis": "measured", "any_band_beats_break_even": False}
    hs = _historical_scores(league)
    if hs.empty:
        out["tier_basis"] = "unmeasured"
        out["note"] = "No graded out-of-sample spread results yet; tiers fall back to ranking by disagreement."
        out["tiers"] = {t: {"n": 0, "hit_rate": None, "beats_break_even": None, "range": None} for t in ("A+", "A", "B")}
        return out
    edges = config.PICK_SCORE_BANDS
    bands = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        b = hs[(hs._score >= lo) & (hs._score < hi)]
        if b.empty:
            continue
        wins, n = int(b._win.sum()), int(len(b))
        lo_ci, hi_ci = wilson(wins, n)
        bands.append({"lo": lo, "hi": None if hi == float("inf") else hi, "n": n, "hit_rate": round(wins / n, 4),
                      "ci_low": round(lo_ci, 4), "ci_high": round(hi_ci, 4),
                      "beats_break_even": (wins / n) > BREAK_EVEN, "significant": lo_ci > BREAK_EVEN})
    merged = []
    for b in bands:                                   # bands too small to say anything join their neighbour
        if merged and b["n"] < config.PICK_MIN_CALIBRATION_N:
            m = merged[-1]
            tot = m["n"] + b["n"]
            rate = (m["hit_rate"] * m["n"] + b["hit_rate"] * b["n"]) / tot
            lo_ci, hi_ci = wilson(int(round(rate * tot)), tot)
            merged[-1] = {"lo": m["lo"], "hi": b["hi"], "n": tot, "hit_rate": round(rate, 4),
                          "ci_low": round(lo_ci, 4), "ci_high": round(hi_ci, 4),
                          "beats_break_even": rate > BREAK_EVEN, "significant": lo_ci > BREAK_EVEN}
        else:
            merged.append(b)
    out["bands"] = merged
    out["any_band_beats_break_even"] = any(b["significant"] for b in merged)
    ranked = sorted([b for b in merged if b["n"] >= config.PICK_MIN_CALIBRATION_N], key=lambda b: -b["hit_rate"])
    for tier, b in zip(("A+", "A", "B"), ranked):
        out["tiers"][tier] = {"n": b["n"], "hit_rate": b["hit_rate"], "ci_low": b["ci_low"], "ci_high": b["ci_high"],
                              "beats_break_even": b["beats_break_even"], "significant": b["significant"],
                              "range": [b["lo"], b["hi"]]}
    for tier in ("A+", "A", "B"):
        out["tiers"].setdefault(tier, {"n": 0, "hit_rate": None, "beats_break_even": None, "range": None})
    best = ranked[0]["hit_rate"] if ranked else None
    out["note"] = ("Tiers are named by measured performance: A+ is the score band that historically won most often, "
                   "not the band with the biggest disagreement. Measured on out-of-sample spread plays from the "
                   "walk-forward backtest. Split-based bonuses are not reflected, because betting splits do not exist "
                   "for past seasons." + ("" if best is None else f" Best band: {best * 100:.1f}%."))
    live = MODEL / "picks_evaluation" / league / f"{config.SEASON}.csv"
    if live.exists():
        lv = pd.read_csv(live)
        lv = lv[lv.result.isin(["WIN", "LOSS"])]
        if not lv.empty:
            out["live"] = {t: {"n": int(len(g)), "hit_rate": round(float((g.result == "WIN").mean()), 4)} for t, g in lv.groupby("tier")}
            out["live_total"] = {"n": int(len(lv)), "hit_rate": round(float((lv.result == "WIN").mean()), 4)}
    return out


def assign_tiers(df: pd.DataFrame, calib: dict | None = None) -> pd.DataFrame:
    """Map each play's score into the band that measured best, so a tier label reflects evidence."""
    if df.empty:
        return df
    d = df.copy()
    ranges = [(t, v["range"]) for t, v in ((calib or {}).get("tiers") or {}).items() if v.get("range")]
    if ranges:
        def tier_of(sc):
            for t, (lo, hi) in ranges:
                if sc >= lo and (hi is None or sc < hi):
                    return t
            return None
    else:
        def tier_of(sc):
            for t, lo in sorted(config.PICK_TIERS.items(), key=lambda kv: -kv[1]):
                if sc >= lo:
                    return t
            return None
    d["tier"] = d.score.map(tier_of)
    return d[d.tier.notna()].sort_values("score", ascending=False)


def build_week(league: str, season: int, week: int) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Returns (qualified picks, rejected candidates with reasons, calibration)."""
    c = candidates(league, season, week)
    calib = calibrate(league)
    if c.empty:
        return c, c, calib
    scored = score(c)
    rejected = scored[~scored.qualified].copy()
    picks = assign_tiers(scored[scored.qualified], calib)
    stamp = datetime.now(timezone.utc).isoformat()
    for df in (picks, rejected):
        if not df.empty:
            df["built_at"] = stamp
            df["pick_id"] = (df.game_id + "_" + df.market + "_" + df.side.astype(str).str.replace(" ", "") + "_" + df.model_version)
    return picks, rejected, calib
