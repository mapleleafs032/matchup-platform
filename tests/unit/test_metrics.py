import math

import numpy as np
import pytest

from edge.backtest import metrics as M


def test_log_loss_and_brier_known_values() -> None:
    assert M.log_loss([0.5, 0.5], [1, 0]) == pytest.approx(math.log(2))
    assert M.brier([1.0, 0.0], [1, 0]) == 0.0


def test_calibration_slope_near_one_for_calibrated_forecasts() -> None:
    rng = np.random.default_rng(0)
    p = rng.uniform(0.2, 0.8, 20000)
    y = (rng.uniform(size=p.size) < p).astype(int)
    slope, intercept = M.calibration_slope_intercept(p, y)
    assert slope == pytest.approx(1.0, abs=0.1) and intercept == pytest.approx(0.0, abs=0.1)
    # Overconfident forecasts give a slope below 1.
    q = np.clip(0.5 + (p - 0.5) * 1.6, 0.01, 0.99)
    assert M.calibration_slope_intercept(q, y)[0] < 0.8


def test_wilson_interval_contains_rate() -> None:
    lo, hi = M.wilson_interval(55, 100)
    assert lo < 0.55 < hi and hi - lo < 0.21
    assert all(math.isnan(x) for x in M.wilson_interval(0, 0))


def test_bootstrap_is_deterministic() -> None:
    v = [1.0, -1.0, 0.9, -1.0, 0.9]
    assert M.bootstrap_mean_ci(v) == M.bootstrap_mean_ci(v)


def test_calibration_bins_cover_all_points() -> None:
    bins = M.calibration_bins([0.05, 0.15, 0.95, 1.0], [0, 0, 1, 1])
    assert sum(b.n for b in bins) == 4
