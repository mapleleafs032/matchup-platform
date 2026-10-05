from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from edge.core.config import MarketType
from edge.markets.schemas import Side
from edge.models.nfl_ratings import (
    TEAMS,
    RatingParams,
    fit_ratings,
    predict_game,
    prob_exceeds,
)
from edge.predict.contracts import PredictionStatus
from edge.sources.nflverse import NflGameRecord

START = datetime(2020, 9, 10, 17, 0, tzinfo=UTC)
TRUE = {t: r for t, r in zip(TEAMS, np.linspace(-8, 8, len(TEAMS)), strict=True)}
HFA = 2.0


def _game(
    i: int,
    home: str,
    away: str,
    kick: datetime,
    margin: int | None,
    total: int = 44,
    handicap: float | None = -3.0,
) -> NflGameRecord:
    hs = None if margin is None else (total + margin) // 2
    as_ = None if margin is None or hs is None else hs - margin
    return NflGameRecord(
        game_id=f"g{i}",
        season=kick.year,
        game_type="REG",
        week=1 + i // 16,
        kickoff=kick,
        kickoff_time_known=True,
        result_available_at=kick + timedelta(hours=4),
        home=home,
        away=away,
        neutral=False,
        home_score=hs,
        away_score=as_,
        overtime=False,
        home_handicap=handicap,
        total_line=44.5,
        home_ml=None,
        away_ml=None,
        home_spread_odds=None,
        away_spread_odds=None,
        over_odds=None,
        under_odds=None,
        div_game=False,
        roof=None,
    )


def _league(weeks: int = 20, seed: int = 1) -> list[NflGameRecord]:
    rng = np.random.default_rng(seed)
    out, i = [], 0
    for w in range(weeks):
        order = rng.permutation(TEAMS)
        kick = START + timedelta(days=7 * w)
        for h, a in zip(order[::2], order[1::2], strict=True):
            m = round(HFA + TRUE[h] - TRUE[a] + rng.normal(0, 6))
            out.append(_game(i, str(h), str(a), kick, int(m), total=44 + 2 * (m % 2)))
            i += 1
    return out


P = RatingParams(half_life_days=10_000, ridge_margin=1.0, min_games=64, margin_sd=6.0)


def test_recovers_known_ratings_and_home_field() -> None:
    games = _league()
    fit = fit_ratings(games, START + timedelta(days=200), P)
    est = np.array([fit.rating(t) for t in TEAMS])
    truth = np.array([TRUE[t] for t in TEAMS])
    assert np.corrcoef(est, truth)[0, 1] > 0.95
    assert fit.hfa == pytest.approx(HFA, abs=1.0)


def test_replay_future_results_cannot_change_a_prediction() -> None:
    """Leakage test: altering every game after the cutoff leaves predictions unchanged."""
    games = _league()
    cutoff = START + timedelta(days=7 * 10) - timedelta(minutes=1)
    target = next(g for g in games if g.kickoff > cutoff)
    before = predict_game(games, target, cutoff, P)
    tampered = [replace(g, home_score=99, away_score=0) if g.kickoff > cutoff else g for g in games]
    after = predict_game(tampered, target, cutoff, P)
    assert before.probabilities == after.probabilities
    assert before.projected_home == after.projected_home


def test_results_not_yet_available_are_excluded() -> None:
    games = _league(weeks=12)
    last_kick = max(g.kickoff for g in games)
    cutoff = last_kick + timedelta(hours=1)  # last week's games kicked off, results not out
    fit_a = fit_ratings(games, cutoff, P)
    fit_b = fit_ratings([g for g in games if g.kickoff < last_kick], cutoff, P)
    assert fit_a.n_games == fit_b.n_games
    assert np.allclose(fit_a.beta_m, fit_b.beta_m)


def test_too_little_history_abstains() -> None:
    games = _league(weeks=2)
    target = _game(999, "KC", "BUF", START + timedelta(days=30), None)
    pred = predict_game(games, target, START + timedelta(days=29), P)
    assert pred.status is PredictionStatus.INSUFFICIENT_DATA and "need" in (
        pred.abstain_reason or ""
    )


def test_prediction_probabilities_are_coherent() -> None:
    games = _league()
    cutoff = START + timedelta(days=200)
    target = _game(999, TEAMS[-1], TEAMS[0], cutoff + timedelta(days=1), None, handicap=-7.0)
    pred = predict_game(games, target, cutoff, P)
    assert pred.status is PredictionStatus.OK
    ml_h = pred.for_side(MarketType.MONEYLINE, None, Side.HOME)
    ml_a = pred.for_side(MarketType.MONEYLINE, None, Side.AWAY)
    sp_h = pred.for_side(MarketType.SPREAD, -7.0, Side.HOME)
    sp_a = pred.for_side(MarketType.SPREAD, -7.0, Side.AWAY)
    assert ml_h and ml_a and sp_h and sp_a
    assert ml_h.p > 0.9  # best team at home vs worst
    assert ml_h.p + ml_a.p + ml_h.p_push == pytest.approx(1.0)
    assert sp_h.p + sp_a.p + sp_h.p_push == pytest.approx(1.0)
    assert sp_h.p_push > 0  # whole-number line can push
    for sp in pred.probabilities:
        assert sp.p_low <= sp.p <= sp.p_high


def test_prob_exceeds_push_only_on_whole_numbers() -> None:
    win, push = prob_exceeds(3.0, 13.0, 3.0)
    assert push > 0 and win < 0.5
    win_h, push_h = prob_exceeds(3.0, 13.0, 3.5)
    assert push_h == 0 and win_h < win + push
