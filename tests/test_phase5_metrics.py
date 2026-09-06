from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import config
from pipeline import asof, ratings
from pipeline.metric_registry import REGISTRY


def _rows():
    base = {"weight": 1.0, "neutral_site": False, "conference_game": True, "is_night": False, "is_favorite": True, "opp_ranked": False, "is_fcs_game": False}
    return pd.DataFrame([
        {**base, "game_id": "g1", "team_id": "A", "opponent_id": "B", "is_home": True, "effective_at": pd.Timestamp("2026-09-06T00:00Z"),
         "off_ppa_play": 0.2, "off_plays": 60, "total_yards": 400, "plays": 60, "third_down_conv": 6, "third_down_att": 12, "points": 30, "points_allowed": 20, "off_play_action_rate": 0.3},
        {**base, "game_id": "g2", "team_id": "A", "opponent_id": "C", "is_home": False, "effective_at": pd.Timestamp("2026-09-13T00:00Z"),
         "off_ppa_play": -0.1, "off_plays": 40, "total_yards": 200, "plays": 40, "third_down_conv": 2, "third_down_att": 8, "points": 10, "points_allowed": 24, "off_play_action_rate": None},
        {**base, "game_id": "g3", "team_id": "A", "opponent_id": "D", "is_home": True, "effective_at": pd.Timestamp("2026-09-20T00:00Z"),
         "off_ppa_play": 0.5, "off_plays": 50, "total_yards": 450, "plays": 50, "third_down_conv": 8, "third_down_att": 10, "points": 45, "points_allowed": 7, "off_play_action_rate": 0.4},
    ])


def test_aggregate_rules():
    r = _rows()
    r["points_per_game"] = r.points
    out = asof.aggregate(r)
    # wmean by plays: (0.2*60 - 0.1*40 + 0.5*50)/150
    assert abs(out["off_ppa_play"][0] - (12 - 4 + 25) / 150) < 1e-9 and out["off_ppa_play"][1] == 3
    # ratio: sum(num)/sum(den)
    assert abs(out["third_down_pct_off"][0] - 16 / 30) < 1e-9
    assert abs(out["yards_per_play_off"][0] - 1050 / 150) < 1e-9
    # mean
    assert abs(out["points_per_game"][0] - 85 / 3) < 1e-9
    # NULL-tolerant wmean: game 2 has no play-action value -> excluded, n=2, never treated as 0
    pa = out["off_play_action_rate"]
    assert abs(pa[0] - (0.3 * 60 + 0.4 * 50) / 110) < 1e-9 and pa[1] == 2
    # metric with no column at all -> (None, 0)
    assert out["def_blitz_rate"] == (None, 0)


def test_window_cutoff_excludes_future_games():
    r = _rows()
    cut = pd.Timestamp("2026-09-19T20:00Z")           # before game 3
    assert len(asof.window_rows(r, "SEASON", cut)) == 2
    assert len(asof.window_rows(r, "LAST3", cut)) == 2
    assert len(asof.window_rows(r, "HOME", cut)) == 1 and len(asof.window_rows(r, "AWAY", cut)) == 1
    assert len(asof.window_rows(r, "SEASON", pd.Timestamp("2026-09-01T00:00Z"))) == 0
    out = asof.aggregate(asof.window_rows(r, "SEASON", cut))
    assert abs(out["off_ppa_play"][0] - (12 - 4) / 100) < 1e-9   # game 3's 0.5 never leaks in


def test_prior_blend_and_weights():
    cur = {"off_ppa_play": (0.10, 3), "def_ppa_play": (None, 0)}
    prior = {"off_ppa_play": 0.30, "def_ppa_play": -0.05}
    out, flags = asof.apply_prior(cur, prior, 0.5)
    assert abs(out["off_ppa_play"][0] - 0.20) < 1e-9 and out["def_ppa_play"] == (-0.05, 0) and "PRIOR_BLENDED" in flags
    out, flags = asof.apply_prior(cur, None, 0.5)
    assert out == cur and "PRIOR_MISSING" in flags
    assert asof.prior_weight("CFB", 1) == 0.85 and asof.prior_weight("CFB", 12) == 0.05 and asof.prior_weight("NFL", 3) == 0.40


def test_ridge_recovers_known_effects():
    rng = np.random.default_rng(0)
    teams = [f"T{i}" for i in range(12)]
    off = {t: rng.normal(0, 3) for t in teams}; deff = {t: rng.normal(0, 3) for t in teams}
    rows = []
    for _ in range(400):
        a, b = rng.choice(teams, 2, replace=False)
        rows.append({"team_id": a, "opponent_id": b, "is_home": True, "neutral_site": False, "weight": 1.0, "effective_at": pd.Timestamp("2025-10-01T00:00:00Z"),
                     "points": 24 + off[a] + deff[b] + 1.5 + rng.normal(0, 2)})
        rows.append({"team_id": b, "opponent_id": a, "is_home": False, "neutral_site": False, "weight": 1.0, "effective_at": pd.Timestamp("2025-10-01T00:00:00Z"),
                     "points": 24 + off[b] + deff[a] - 1.5 + rng.normal(0, 2)})
    d = pd.DataFrame(rows)
    fit = ratings.fit_ridge(d, "points", lam=0.5)
    est_off = np.array([fit["off"][t] for t in teams]); true_off = np.array([off[t] for t in teams])
    assert np.corrcoef(est_off, true_off)[0, 1] > 0.95 and abs(fit["hfa"] - 1.5) < 0.5


def test_registry_integrity():
    assert REGISTRY.metric_key.is_unique and len(REGISTRY) >= 80
    assert set(REGISTRY.side) <= {"OFF", "DEF", "ST", "QB", "ROSTER", "MARKET", "CONTEXT"}
    assert REGISTRY.description.str.len().gt(15).all()
    assert REGISTRY["agg"].str.match(r"^(mean|wmean:\w+|ratio:\w+/\w+)$").all()


@pytest.mark.live
def test_nfl_2025_week10_build_matches_independent_recompute():
    import json, pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "data/tables/analytics/team_metrics_asof/NFL/2025/W10.parquet"
    if not path.exists():
        pytest.skip("run build_metrics NFL 2025 --weeks 10 first")
    m = pd.read_parquet(path)
    tg = asof.load_team_games("NFL", 2025)
    row = m[(m.team_id == "NFL_ARI") & (m.window == "SEASON") & (m.adjustment == "RAW")].iloc[0]
    cut = pd.Timestamp(row.as_of_ts)
    t = tg[(tg.team_id == "NFL_ARI") & (tg.effective_at < cut)]
    indep = (t.off_ppa_play * t.off_plays).sum() / t.off_plays.sum()
    assert abs(json.loads(row.metrics)["off_ppa_play"]["v"] - indep) < 1e-3
    assert (tg[tg.team_id == "NFL_ARI"].effective_at >= cut).sum() > 0     # future games existed and were excluded
