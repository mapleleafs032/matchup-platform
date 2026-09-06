"""
Opponent adjustment (§18) via ridge regression, fit strictly on games with effective_at < cutoff.

For each adjusted metric m (per team-game, offense perspective):
    m_ig = intercept + off_effect[team] + def_effect[opponent] + hfa * is_home + e
Solved jointly as one least-squares system with an L2 penalty (ridge) so early-season teams with few
games are shrunk toward average instead of exploding. Same treatment for both leagues; lambda is
league-specific in config and will be tuned in the backtest.

Outputs, per team: off_effect (points/EPA above average offense), def_effect (below-average means better
defense for *allowed* metrics; we store def_effect with the sign convention "positive = good defense"),
and the intercept. Ratings on the margin scale (points) come from adjusting points_for / points_against.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config

ADJUSTED_METRICS = ["off_ppa_play", "off_success_rate", "off_explosiveness", "off_ppa_pass", "off_ppa_rush",
                    "yards_per_play_off", "points"]


def fit_ridge(tg: pd.DataFrame, metric: str, lam: float) -> dict:
    """
    tg: team-game rows (one per team per game, offense perspective) with columns
        team_id, opponent_id, is_home, <metric>, weight (FCS down-weight), and neutral_site.
    Returns {"intercept", "hfa", "off": {team: effect}, "def": {team: effect}} where def effect is on the
    'allowed' scale (negative = opponent produces less against you = good defense).
    """
    d = tg[tg[metric].notna()].copy()
    if len(d) < 10:
        return {"intercept": None, "hfa": None, "off": {}, "def": {}, "n": len(d)}
    teams = sorted(set(d.team_id) | set(d.opponent_id))
    idx = {t: i for i, t in enumerate(teams)}
    n, k = len(d), len(teams)
    X = np.zeros((n, 2 * k + 1))
    for r, (t, o, h, neu) in enumerate(zip(d.team_id, d.opponent_id, d.is_home, d.neutral_site)):
        X[r, idx[t]] = 1.0
        X[r, k + idx[o]] = 1.0
        X[r, 2 * k] = 0.0 if neu else (1.0 if h else -1.0)
    y = d[metric].astype(float).to_numpy()
    w = d.weight.astype(float).to_numpy() if "weight" in d else np.ones(n)
    mu = np.average(y, weights=w)
    yc = y - mu
    sw = np.sqrt(w)[:, None]
    Xw, yw = X * sw, yc * sw[:, 0]
    # ridge: penalize team effects, not the HFA term (small penalty for stability)
    pen = np.full(2 * k + 1, lam); pen[-1] = lam * 0.1
    A = Xw.T @ Xw + np.diag(pen)
    b = Xw.T @ yw
    beta = np.linalg.solve(A, b)
    off = {t: float(beta[idx[t]]) for t in teams}
    deff = {t: float(beta[k + idx[t]]) for t in teams}
    return {"intercept": float(mu), "hfa": float(beta[-1]), "off": off, "def": deff, "n": n}


def build_ratings(tg_all: pd.DataFrame, league: str, season: int, as_of_week: int, cutoff: pd.Timestamp) -> tuple[pd.DataFrame, dict]:
    """
    Returns (team_ratings rows for as_of_week, fitted effects by metric) using only rows with
    effective_at < cutoff. Ratings are on the points scale from the 'points' fit.
    """
    d = tg_all[pd.to_datetime(tg_all.effective_at, utc=True) < cutoff]
    lam = config.RIDGE_LAMBDA[league]
    fits = {m: fit_ridge(d, m, lam) for m in ADJUSTED_METRICS if m in d.columns}
    pts = fits.get("points", {})
    if not pts.get("off"):
        return pd.DataFrame(), fits
    rows = []
    teams = sorted(pts["off"].keys())
    for t in teams:
        off_pts = pts["off"][t]
        def_pts = -pts["def"][t]            # positive = allows fewer points than average
        rows.append({"team_id": t, "season": season, "as_of_week": as_of_week, "rating_overall": off_pts + def_pts,
                     "rating_off": off_pts, "rating_def": def_pts,
                     "rating_pass_off": fits.get("off_ppa_pass", {}).get("off", {}).get(t),
                     "rating_pass_def": -fits.get("off_ppa_pass", {}).get("def", {}).get(t, 0.0) if fits.get("off_ppa_pass", {}).get("def") else None,
                     "rating_rush_off": fits.get("off_ppa_rush", {}).get("off", {}).get(t),
                     "rating_rush_def": -fits.get("off_ppa_rush", {}).get("def", {}).get(t, 0.0) if fits.get("off_ppa_rush", {}).get("def") else None,
                     "rating_st": None, "sos": None, "sos_rank": None, "hfa_team": None,
                     "hfa_league": pts["hfa"], "games_in_fit": int((d.team_id == t).sum()),
                     "method": "ridge_v1", "build_version": config.PIPELINE_VERSION, "built_at": pd.Timestamp.now(tz="UTC").isoformat()})
    out = pd.DataFrame(rows)
    # strength of schedule: mean opponent overall rating faced so far (as-of), rank descending
    rating_map = dict(zip(out.team_id, out.rating_overall))
    sos = d.groupby("team_id").opponent_id.apply(lambda s: float(np.mean([rating_map.get(o, 0.0) for o in s])))
    out["sos"] = out.team_id.map(sos)
    out["sos_rank"] = out.sos.rank(ascending=False, method="min").astype("Int64")
    return out, fits


def adjust_game_values(tg: pd.DataFrame, fits: dict) -> pd.DataFrame:
    """
    Opponent-adjust per-game offense metrics: value - def_effect[opponent] - hfa_term.
    Returns a copy with '<metric>_adj' columns. Metrics without a fit are left NULL.
    """
    out = tg.copy()
    for m, f in fits.items():
        if not f.get("def"):
            out[f"{m}_adj"] = None
            continue
        hfa = f["hfa"] or 0.0
        h_term = np.where(out.neutral_site, 0.0, np.where(out.is_home, hfa, -hfa))
        out[f"{m}_adj"] = out[m].astype(float) - out.opponent_id.map(f["def"]).fillna(0.0) - h_term
    return out
