from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import config
from pipeline import picks_engine as pe


def test_american_odds_conversions():
    assert abs(pe.american_to_prob(-150) - 0.6) < 1e-9 and abs(pe.american_to_prob(150) - 0.4) < 1e-9
    assert pe.american_profit(150) == 1.5 and pe.american_profit(-200) == 0.5
    assert pe.american_to_prob(None) is None


def test_score_respects_quality_and_caps_absurd_edges():
    d = pd.DataFrame([
        {"market": "SPREAD", "edge_points": 4.0, "data_quality": 1.0, "signals": ""},
        {"market": "SPREAD", "edge_points": 4.0, "data_quality": 0.5, "signals": ""},
        {"market": "SPREAD", "edge_points": 40.0, "data_quality": 1.0, "signals": ""},
    ])
    s = pe.score(d)
    assert s.score.iloc[0] == 4.0
    assert s.score.iloc[1] == 2.0                       # thin data halves the score
    assert s.score.iloc[2] == config.PICK_EDGE_CAP["SPREAD"]   # a 40-point disagreement is capped, not celebrated


def test_signal_bonuses_only_help_and_are_bounded():
    d = pd.DataFrame([{"market": "SPREAD", "edge_points": 3.0, "data_quality": 1.0, "signals": ""},
                      {"market": "SPREAD", "edge_points": 3.0, "data_quality": 1.0, "signals": "rlm_agrees,money_agrees,key_number"}])
    s = pe.score(d)
    assert s.score.iloc[1] > s.score.iloc[0]
    assert s.score.iloc[1] - s.score.iloc[0] == pytest.approx(0.8 + 0.5 + 0.4)


def test_tiers_are_ordered_and_weak_plays_are_dropped():
    d = pd.DataFrame([{"market": "SPREAD", "edge_points": e, "data_quality": 1.0, "signals": ""} for e in (6.0, 3.0, 1.5, 0.5)])
    t = pe.assign_tiers(pe.score(d))
    assert list(t.tier) == ["A+", "A", "B"]             # the 0.5-point play is not a play at all
    assert t.score.is_monotonic_decreasing


def test_key_number_side_logic():
    assert pe._key_number_side(3.5, side_home=True) == "getting more than 3"      # home +3.5
    assert pe._key_number_side(-3.5, side_home=True) is None                       # home laying 3.5
    assert pe._key_number_side(-7.5, side_home=False) == "getting more than 7"     # away +7.5
    assert pe._key_number_side(None, True) is None


def test_calibration_reports_unmeasured_rather_than_guessing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(pe, "MODEL", tmp_path / "tables" / "model")
    cal = pe.calibrate("NFL")
    assert cal["tiers"] == {} and "unmeasured" in cal["note"].lower() and cal["break_even"] == pe.BREAK_EVEN


def test_calibration_marks_tiers_below_break_even(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(pe, "MODEL", tmp_path / "tables" / "model")
    d = tmp_path / "tables" / "model" / "backtest" / "NFL"; d.mkdir(parents=True)
    rng = np.random.default_rng(1)
    n = 300
    rows = pd.DataFrame({"season": 2024, "edge_vs_market": rng.uniform(4.5, 7.0, n), "data_quality": 1.0,
                         "model_ats_result": ["WIN"] * 120 + ["LOSS"] * 180, "in_sample_warning": False})
    rows.to_csv(d / "evaluation_NFL_v1.0.csv", index=False)
    cal = pe.calibrate("NFL")
    ap = cal["tiers"]["A+"]
    assert ap["n"] == n and ap["hit_rate"] == 0.4 and ap["beats_break_even"] is False
