"""Walk-forward evaluation of the NFL baseline.

Protocol (fixed before any results were seen; ADR 0004):
- TRAIN 2006-2017: choose half-life and ridge strengths by out-of-sample
  margin / total RMSE; set game-level SDs to the out-of-sample residual SDs.
- VALIDATION 2018-2022: report only; may inform the NEXT model version.
- TEST 2023-2025: evaluated once with frozen settings.
Each (season, week) is predicted from a fit whose cutoff is one minute before
that week's first kickoff, using only results available by then.

Betting simulation: bets at the nflverse closing line with its recorded odds.
Games without recorded odds are skipped (no assumed -110). Closing-line bets
have zero CLV by construction; this measures accuracy against the close only.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta
from typing import Any

import numpy as np

from edge.backtest import metrics as M
from edge.markets.odds import american_to_decimal, expected_value, no_vig_probabilities
from edge.models.nfl_ratings import (
    InsufficientData,
    RatingParams,
    fit_ratings,
    prob_exceeds,
    project,
)
from edge.sources.nflverse import NflGameRecord

SPLITS: dict[str, tuple[int, int]] = {
    "train": (2006, 2017),
    "validation": (2018, 2022),
    "test": (2023, 2025),
}
GRID_HALF_LIFE = (120.0, 180.0, 240.0, 365.0)
GRID_RIDGE_MARGIN = (2.0, 5.0, 10.0, 20.0)
GRID_RIDGE_TOTAL = (5.0, 10.0, 20.0, 40.0)
EV_THRESHOLDS = (0.0, 0.03, 0.05)


@dataclass(frozen=True, slots=True)
class Row:
    game: NflGameRecord
    margin_hat: float
    margin_se: float
    total_hat: float
    total_se: float


def walk_forward(
    games: Sequence[NflGameRecord], seasons: tuple[int, int], params: RatingParams
) -> tuple[list[Row], int]:
    """Returns predictions for completed games in `seasons` and the number of
    games the model abstained on."""
    weeks: dict[tuple[int, int], list[NflGameRecord]] = defaultdict(list)
    for g in games:
        if seasons[0] <= g.season <= seasons[1] and g.completed:
            weeks[(g.season, g.week)].append(g)
    rows: list[Row] = []
    abstained = 0
    for key in sorted(weeks):
        wk = weeks[key]
        cutoff = min(g.kickoff for g in wk) - timedelta(minutes=1)
        try:
            fit = fit_ratings(games, cutoff, params)
        except InsufficientData:
            abstained += len(wk)
            continue
        for g in wk:
            try:
                p = project(fit, g)
            except InsufficientData:
                abstained += 1
                continue
            rows.append(Row(g, p.margin, p.margin_se, p.total, p.total_se))
    return rows, abstained


def _margins(rows: Iterable[Row]) -> tuple[np.ndarray, np.ndarray]:
    rs = list(rows)
    return (np.array([r.margin_hat for r in rs]), np.array([float(r.game.margin or 0) for r in rs]))


def tune(games: Sequence[NflGameRecord]) -> tuple[RatingParams, dict[str, Any]]:
    """Grid search on TRAIN seasons only."""
    lo_hi = SPLITS["train"]
    log: dict[str, Any] = {"margin_grid": [], "total_grid": []}
    best: tuple[float, RatingParams] | None = None
    for hl, rm in itertools.product(GRID_HALF_LIFE, GRID_RIDGE_MARGIN):
        p = RatingParams(half_life_days=hl, ridge_margin=rm)
        rows, _ = walk_forward(games, lo_hi, p)
        pred, act = _margins(rows)
        score = M.rmse(pred, act)
        log["margin_grid"].append(
            {"half_life": hl, "ridge_margin": rm, "rmse": score, "n": len(rows)}
        )
        if best is None or score < best[0]:
            best = (score, p)
    assert best is not None
    chosen = best[1]
    best_t: tuple[float, float] | None = None
    for rt in GRID_RIDGE_TOTAL:
        p = replace(chosen, ridge_total=rt)
        rows, _ = walk_forward(games, lo_hi, p)
        score = M.rmse(
            [r.total_hat for r in rows],
            [float((r.game.home_score or 0) + (r.game.away_score or 0)) for r in rows],
        )
        log["total_grid"].append({"ridge_total": rt, "rmse": score, "n": len(rows)})
        if best_t is None or score < best_t[0]:
            best_t = (score, rt)
    assert best_t is not None
    chosen = replace(chosen, ridge_total=best_t[1])
    rows, _ = walk_forward(games, lo_hi, chosen)
    # Game-level noise = out-of-sample residual SD net of parameter uncertainty.
    m_res = np.array([float(r.game.margin or 0) - r.margin_hat for r in rows])
    t_res = np.array(
        [float((r.game.home_score or 0) + (r.game.away_score or 0)) - r.total_hat for r in rows]
    )
    m_sd = math.sqrt(max(1.0, float(np.mean(m_res**2) - np.mean([r.margin_se**2 for r in rows]))))
    t_sd = math.sqrt(max(1.0, float(np.mean(t_res**2) - np.mean([r.total_se**2 for r in rows]))))
    chosen = replace(chosen, margin_sd=m_sd, total_sd=t_sd)
    log["chosen"] = asdict(chosen)
    return chosen, log


@dataclass(slots=True)
class BetLog:
    n: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    profit: float = 0.0
    skipped_no_odds: int = 0
    details: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        decided = self.wins + self.losses
        lo, hi = M.wilson_interval(self.wins, decided)
        roi_lo, roi_hi = M.bootstrap_mean_ci(self.details) if self.details else (math.nan, math.nan)
        return {
            "bets": self.n,
            "wins": self.wins,
            "losses": self.losses,
            "pushes": self.pushes,
            "win_rate": self.wins / decided if decided else math.nan,
            "win_rate_95ci": [lo, hi],
            "units": self.profit,
            "roi": self.profit / self.n if self.n else math.nan,
            "roi_90ci": [roi_lo, roi_hi],
            "skipped_no_odds": self.skipped_no_odds,
        }


def _settle(log: BetLog, outcome: float, dec: float) -> None:
    """outcome > 0 win, == 0 push, < 0 loss (in points vs the line)."""
    log.n += 1
    if outcome > 0:
        log.wins += 1
        pnl = dec - 1.0
    elif outcome == 0:
        log.pushes += 1
        pnl = 0.0
    else:
        log.losses += 1
        pnl = -1.0
    log.profit += pnl
    log.details.append(pnl)


def evaluate(rows: Sequence[Row], params: RatingParams) -> dict[str, Any]:
    out: dict[str, Any] = {"games": len(rows)}
    pred, act = _margins(rows)
    mk = [r for r in rows if r.game.home_handicap is not None]
    mp, ma = _margins(mk)
    mm = np.array([-(r.game.home_handicap or 0.0) for r in mk])
    out["margin"] = {
        "n": len(mk),
        "model_mae": M.mae(mp, ma),
        "market_mae": M.mae(mm, ma),
        "model_rmse": M.rmse(mp, ma),
        "market_rmse": M.rmse(mm, ma),
        "all_games_model_mae": M.mae(pred, act),
    }
    tk = [r for r in rows if r.game.total_line is not None]
    t_act = [float((r.game.home_score or 0) + (r.game.away_score or 0)) for r in tk]
    out["total"] = {
        "n": len(tk),
        "model_mae": M.mae([r.total_hat for r in tk], t_act),
        "market_mae": M.mae([r.game.total_line or 0.0 for r in tk], t_act),
    }

    # Moneyline probability quality (ties excluded).
    ml = [r for r in rows if r.game.margin != 0]
    y = [1 if (r.game.margin or 0) > 0 else 0 for r in ml]
    p_model = []
    for r in ml:
        sd = math.sqrt(params.margin_sd**2 + r.margin_se**2)
        win, push = prob_exceeds(r.margin_hat, sd, 0.0)
        p_model.append(win / (1 - push))  # conditional on no tie
    slope, intercept = M.calibration_slope_intercept(p_model, y)
    out["moneyline_model"] = {
        "n": len(ml),
        "log_loss": M.log_loss(p_model, y),
        "brier": M.brier(p_model, y),
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "bins": [asdict(b) for b in M.calibration_bins(p_model, y)],
    }
    mkt = [
        (i, r)
        for i, r in enumerate(ml)
        if r.game.home_ml is not None and r.game.away_ml is not None
    ]
    if mkt:
        pm = [
            no_vig_probabilities(
                [american_to_decimal(r.game.home_ml or 0), american_to_decimal(r.game.away_ml or 0)]
            )[0]
            for _, r in mkt
        ]
        ym = [y[i] for i, _ in mkt]
        pmod = [p_model[i] for i, _ in mkt]
        base = float(np.mean(y))
        out["moneyline_vs_market_same_games"] = {
            "n": len(mkt),
            "model_log_loss": M.log_loss(pmod, ym),
            "market_log_loss": M.log_loss(pm, ym),
            "model_brier": M.brier(pmod, ym),
            "market_brier": M.brier(pm, ym),
            "home_rate_log_loss": M.log_loss([base] * len(ym), ym),
        }

    # Betting at the close: spread and totals, by EV threshold.
    for name in ("spread", "total"):
        logs = {t: BetLog() for t in EV_THRESHOLDS}
        for r in rows:
            g = r.game
            if name == "spread":
                if g.home_handicap is None:
                    continue
                line, mu, se, gsd = -g.home_handicap, r.margin_hat, r.margin_se, params.margin_sd
                odds_a, odds_b = g.home_spread_odds, g.away_spread_odds
                result = float(g.margin or 0) - line  # >0 home covers
            else:
                if g.total_line is None:
                    continue
                line, mu, se, gsd = g.total_line, r.total_hat, r.total_se, params.total_sd
                odds_a, odds_b = g.over_odds, g.under_odds
                result = float((g.home_score or 0) + (g.away_score or 0)) - line
            sd = math.sqrt(gsd**2 + se**2)
            p_a, push = prob_exceeds(mu, sd, line)
            p_b = max(0.0, 1 - p_a - push)
            for t, log in logs.items():
                if odds_a is None or odds_b is None:
                    log.skipped_no_odds += 1
                    continue
                da, db = american_to_decimal(odds_a), american_to_decimal(odds_b)
                ev_a, ev_b = expected_value(p_a, da, push), expected_value(p_b, db, push)
                if max(ev_a, ev_b) < t or max(ev_a, ev_b) <= 0:
                    continue
                if ev_a >= ev_b:
                    _settle(log, result, da)
                else:
                    _settle(log, -result, db)
        out[f"{name}_bets_at_close"] = {f"ev>={t:g}": log.summary() for t, log in logs.items()}
    return out


def run(games: Sequence[NflGameRecord]) -> dict[str, Any]:
    params, tuning = tune(games)
    report: dict[str, Any] = {"protocol": SPLITS, "tuning": tuning, "model_version": params.version}
    for split, seasons in SPLITS.items():
        rows, abstained = walk_forward(games, seasons, params)
        res = evaluate(rows, params)
        res["abstained"] = abstained
        res["seasons"] = seasons
        report[split] = res
    return report
