"""Evaluation metrics. Every function reports or accepts sample sizes; nothing
here decides whether a result is 'good'."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike

_EPS = 1e-12


def log_loss(p: ArrayLike, y: ArrayLike) -> float:
    pa, ya = np.clip(np.asarray(p, float), _EPS, 1 - _EPS), np.asarray(y, float)
    return float(-np.mean(ya * np.log(pa) + (1 - ya) * np.log(1 - pa)))


def brier(p: ArrayLike, y: ArrayLike) -> float:
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


def mae(pred: ArrayLike, actual: ArrayLike) -> float:
    return float(np.mean(np.abs(np.asarray(pred, float) - np.asarray(actual, float))))


def rmse(pred: ArrayLike, actual: ArrayLike) -> float:
    return float(np.sqrt(np.mean((np.asarray(pred, float) - np.asarray(actual, float)) ** 2)))


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lo: float
    hi: float
    n: int
    mean_p: float
    observed: float


def calibration_bins(p: ArrayLike, y: ArrayLike, n_bins: int = 10) -> list[CalibrationBin]:
    pa, ya = np.asarray(p, float), np.asarray(y, float)
    edges = np.linspace(0, 1, n_bins + 1)
    out = []
    for lo, hi in itertools.pairwise(edges):
        m = (pa >= lo) & ((pa < hi) if hi < 1 else (pa <= hi))
        if m.any():
            out.append(
                CalibrationBin(
                    float(lo), float(hi), int(m.sum()), float(pa[m].mean()), float(ya[m].mean())
                )
            )
    return out


def calibration_slope_intercept(p: ArrayLike, y: ArrayLike) -> tuple[float, float]:
    """Logistic regression of outcomes on logit(p). Perfect calibration = (1, 0)."""
    pa = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    x = np.column_stack([np.ones_like(pa), np.log(pa / (1 - pa))])
    ya = np.asarray(y, float)
    beta = np.array([0.0, 1.0])
    for _ in range(50):  # IRLS
        mu = 1 / (1 + np.exp(-(x @ beta)))
        wv = mu * (1 - mu) + 1e-9
        step = np.linalg.solve((x.T * wv) @ x, x.T @ (ya - mu))
        beta = beta + step
        if np.max(np.abs(step)) < 1e-10:
            break
    return float(beta[1]), float(beta[0])


def wilson_interval(wins: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    ph = wins / n
    denom = 1 + z * z / n
    center = (ph + z * z / (2 * n)) / denom
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def bootstrap_mean_ci(
    values: ArrayLike, *, n_boot: int = 2000, seed: int = 7, level: float = 0.90
) -> tuple[float, float]:
    v = np.asarray(values, float)
    if v.size == 0:
        return (math.nan, math.nan)
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, v.size, size=(n_boot, v.size))].mean(axis=1)
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))
