"""NFL baseline: time-weighted ridge ratings.

What it predicts
    Home margin and game total for one game, then moneyline, spread-cover and
    over/under probabilities from a normal score distribution.
Data used
    Only final scores of past games (no market data, no post-game facts),
    selected by `result_available_at <= cutoff`.
Method
    margin_i = hfa * (not neutral) + r_home - r_away + e
    total_i  = mu + t_home + t_away + e
    Weighted ridge regression; weight = 0.5 ** (age_days / half_life). Ratings
    shrink toward league average (0). Parameter uncertainty comes from the
    ridge posterior covariance and is propagated into probability intervals.
Abstains when
    fewer than `min_games` usable games, or a team has no game in the window.
Known limitations
    Normal margins ignore NFL key numbers (3, 7); no player availability, no
    play-level efficiency, no weather. This is a baseline, not the final model.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from edge.core.config import MarketType
from edge.markets.schemas import Side
from edge.predict.contracts import Factor, Prediction, PredictionStatus, SideProbability
from edge.sources.nflverse import CANONICAL_KEYS, NflGameRecord

MODEL_NAME = "nfl-ridge-baseline"
TEAMS: tuple[str, ...] = tuple(sorted(CANONICAL_KEYS))
_IDX = {t: i for i, t in enumerate(TEAMS)}
Z90 = 1.6448536269514722


@dataclass(frozen=True, slots=True)
class RatingParams:
    half_life_days: float = 240.0
    ridge_margin: float = 5.0
    ridge_total: float = 10.0
    lookback_days: int = 800
    min_games: int = 128
    margin_sd: float = 13.5  # game-level noise; replaced by the fitted value in backtests
    total_sd: float = 13.5

    @property
    def version(self) -> str:
        return (
            f"{MODEL_NAME}/hl{self.half_life_days:g}-rm{self.ridge_margin:g}"
            f"-rt{self.ridge_total:g}-sd{self.margin_sd:.2f}-{self.total_sd:.2f}"
        )


@dataclass(frozen=True, slots=True)
class RatingsFit:
    cutoff: datetime
    n_games: int
    beta_m: np.ndarray  # [hfa, r_1..r_32]
    cov_m: np.ndarray
    beta_t: np.ndarray  # [mu, t_1..t_32]
    cov_t: np.ndarray
    teams_seen: frozenset[str]

    @property
    def hfa(self) -> float:
        return float(self.beta_m[0])

    def rating(self, team: str) -> float:
        return float(self.beta_m[1 + _IDX[team]])


class InsufficientData(Exception):
    pass


def _ridge(
    x: np.ndarray, y: np.ndarray, w: np.ndarray, penalty: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    xtw = x.T * w
    a = xtw @ x + np.diag(penalty)
    a_inv = np.linalg.inv(a)
    beta = a_inv @ (xtw @ y)
    resid = y - x @ beta
    dof = max(1.0, float(w.sum()) - x.shape[1] * 0.5)
    s2 = float((w * resid**2).sum()) / dof
    return beta, a_inv * s2


def fit_ratings(
    history: Sequence[NflGameRecord], cutoff: datetime, params: RatingParams
) -> RatingsFit:
    """Fit using only games whose result was available by `cutoff`."""
    start = cutoff - timedelta(days=params.lookback_days)
    games = [
        g for g in history if g.completed and g.result_available_at <= cutoff and g.kickoff >= start
    ]
    if len(games) < params.min_games:
        raise InsufficientData(f"{len(games)} games before cutoff (need {params.min_games})")
    n, k = len(games), 1 + len(TEAMS)
    xm, xt = np.zeros((n, k)), np.zeros((n, k))
    ym, yt, w = np.empty(n), np.empty(n), np.empty(n)
    for i, g in enumerate(games):
        h, a = 1 + _IDX[g.home], 1 + _IDX[g.away]
        xm[i, 0] = 0.0 if g.neutral else 1.0
        xm[i, h], xm[i, a] = 1.0, -1.0
        xt[i, 0], xt[i, h], xt[i, a] = 1.0, 1.0, 1.0
        assert g.home_score is not None and g.away_score is not None
        ym[i] = g.home_score - g.away_score
        yt[i] = g.home_score + g.away_score
        age = (cutoff - g.kickoff).total_seconds() / 86400.0
        w[i] = 0.5 ** (age / params.half_life_days)
    pen_m = np.full(k, params.ridge_margin)
    pen_m[0] = 1e-3
    pen_t = np.full(k, params.ridge_total)
    pen_t[0] = 1e-6
    beta_m, cov_m = _ridge(xm, ym, w, pen_m)
    beta_t, cov_t = _ridge(xt, yt, w, pen_t)
    seen = frozenset({g.home for g in games} | {g.away for g in games})
    return RatingsFit(cutoff, n, beta_m, cov_m, beta_t, cov_t, seen)


@dataclass(frozen=True, slots=True)
class Projection:
    margin: float
    margin_se: float
    total: float
    total_se: float


def project(fit: RatingsFit, game: NflGameRecord) -> Projection:
    for t in (game.home, game.away):
        if t not in fit.teams_seen:
            raise InsufficientData(f"no games for {t} in the rating window")
    k = len(fit.beta_m)
    xm, xt = np.zeros(k), np.zeros(k)
    xm[0] = 0.0 if game.neutral else 1.0
    xm[1 + _IDX[game.home]], xm[1 + _IDX[game.away]] = 1.0, -1.0
    xt[0], xt[1 + _IDX[game.home]], xt[1 + _IDX[game.away]] = 1.0, 1.0, 1.0
    return Projection(
        margin=float(xm @ fit.beta_m),
        margin_se=math.sqrt(float(xm @ fit.cov_m @ xm)),
        total=float(xt @ fit.beta_t),
        total_se=math.sqrt(float(xt @ fit.cov_t @ xt)),
    )


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prob_exceeds(mu: float, sd: float, threshold: float) -> tuple[float, float]:
    """For an integer-valued outcome X ~ approx Normal(mu, sd):
    returns (P(X > threshold), P(X == threshold)). The push mass is non-zero
    only for whole-number thresholds (continuity correction)."""
    if float(threshold).is_integer():
        win = 1.0 - _phi((threshold + 0.5 - mu) / sd)
        push = _phi((threshold + 0.5 - mu) / sd) - _phi((threshold - 0.5 - mu) / sd)
        return win, push
    return 1.0 - _phi((threshold - mu) / sd), 0.0


def _side(
    mtype: MarketType,
    line: float | None,
    side: Side,
    mu: float,
    se: float,
    game_sd: float,
    threshold: float,
    upper: bool,
) -> SideProbability:
    sd = math.sqrt(game_sd**2 + se**2)
    vals = []
    for m in (mu - Z90 * se, mu, mu + Z90 * se):
        win, push = prob_exceeds(m, sd, threshold)
        if not upper:  # probability of falling below the threshold
            win = max(0.0, 1.0 - win - push)
        vals.append((win, push))
    lo, mid, hi = sorted(v[0] for v in vals)[0], vals[1][0], sorted(v[0] for v in vals)[2]
    return SideProbability(mtype, line, side, mid, lo, hi, vals[1][1])


def side_probabilities(
    proj: Projection, game: NflGameRecord, params: RatingParams
) -> list[SideProbability]:
    out: list[SideProbability] = []
    m, se = proj.margin, proj.margin_se
    # Moneyline: home wins if margin > 0 (ties push).
    out.append(_side(MarketType.MONEYLINE, None, Side.HOME, m, se, params.margin_sd, 0.0, True))
    out.append(_side(MarketType.MONEYLINE, None, Side.AWAY, m, se, params.margin_sd, 0.0, False))
    if game.home_handicap is not None:
        h = game.home_handicap  # home covers if margin + h > 0  <=> margin > -h
        out.append(_side(MarketType.SPREAD, h, Side.HOME, m, se, params.margin_sd, -h, True))
        out.append(_side(MarketType.SPREAD, h, Side.AWAY, m, se, params.margin_sd, -h, False))
    if game.total_line is not None:
        L = game.total_line
        out.append(
            _side(
                MarketType.TOTAL, L, Side.OVER, proj.total, proj.total_se, params.total_sd, L, True
            )
        )
        out.append(
            _side(
                MarketType.TOTAL,
                L,
                Side.UNDER,
                proj.total,
                proj.total_se,
                params.total_sd,
                L,
                False,
            )
        )
    return out


def predict_game(
    history: Sequence[NflGameRecord],
    game: NflGameRecord,
    cutoff: datetime,
    params: RatingParams,
    *,
    fit: RatingsFit | None = None,
    now: datetime | None = None,
) -> Prediction:
    created = now or cutoff
    try:
        f = fit or fit_ratings(history, cutoff, params)
        proj = project(f, game)
    except InsufficientData as exc:
        return Prediction(
            game.game_id,
            params.version,
            created,
            cutoff,
            PredictionStatus.INSUFFICIENT_DATA,
            abstain_reason=str(exc),
        )
    factors = [
        Factor(f"{game.home} rating", f.rating(game.home)),
        Factor(f"{game.away} rating", -f.rating(game.away)),
        Factor("Home field", 0.0 if game.neutral else f.hfa),
    ]
    return Prediction(
        game.game_id,
        params.version,
        created,
        cutoff,
        PredictionStatus.OK,
        projected_home=(proj.total + proj.margin) / 2,
        projected_away=(proj.total - proj.margin) / 2,
        probabilities=side_probabilities(proj, game, params),
        top_factors=sorted(factors, key=lambda x: -abs(x.contribution)),
    )
