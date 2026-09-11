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
    assert cal["tier_basis"] == "unmeasured" and cal["break_even"] == pe.BREAK_EVEN
    assert all(v["hit_rate"] is None and v["range"] is None for v in cal["tiers"].values())
    assert not cal["bands"]


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
    assert sum(b["n"] for b in cal["bands"]) == n
    assert ap["hit_rate"] < 0.5 and ap["beats_break_even"] is False          # best band is still a losing band
    assert all(not b["beats_break_even"] for b in cal["bands"])
    assert ap["significant"] is False and not cal["any_band_beats_break_even"]
    assert ap["ci_high"] < pe.BREAK_EVEN          # 40% on 300 plays is decisively below break-even


def test_tiers_are_named_by_measured_performance_not_by_edge_size(tmp_path, monkeypatch):
    """The finding that motivated this: in CFB the biggest disagreements performed WORST.
    A+ must therefore attach to the band that won most, not the band that disagreed most."""
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(pe, "MODEL", tmp_path / "tables" / "model")
    d = tmp_path / "tables" / "model" / "backtest" / "CFB"; d.mkdir(parents=True)
    rows = []
    # moderate disagreement (score ~2.0) wins often; large disagreement (score ~6.0) loses often
    for _ in range(200):
        rows.append({"season": 2024, "edge_vs_market": 3.0, "data_quality": 1.0, "model_ats_result": "WIN", "in_sample_warning": False})
    for _ in range(100):
        rows.append({"season": 2024, "edge_vs_market": 3.0, "data_quality": 1.0, "model_ats_result": "LOSS", "in_sample_warning": False})
    for _ in range(80):
        rows.append({"season": 2024, "edge_vs_market": 6.0, "data_quality": 1.0, "model_ats_result": "WIN", "in_sample_warning": False})
    for _ in range(220):
        rows.append({"season": 2024, "edge_vs_market": 6.0, "data_quality": 1.0, "model_ats_result": "LOSS", "in_sample_warning": False})
    pd.DataFrame(rows).to_csv(d / "evaluation_CFB_v1.0.csv", index=False)
    cal = pe.calibrate("CFB")
    ap, worst = cal["tiers"]["A+"], cal["tiers"]["A"]
    assert ap["hit_rate"] > worst["hit_rate"]
    assert ap["range"][0] <= 3.0 < (ap["range"][1] or 99)      # A+ is the MODERATE band
    assert cal["any_band_beats_break_even"] is True            # the moderate band genuinely clears it here
    # a play with a huge score must now be tiered into the poorly performing band, not celebrated
    plays = pd.DataFrame([{"market": "SPREAD", "edge_points": 6.0, "data_quality": 1.0, "signals": ""}])
    t = pe.assign_tiers(pe.score(plays), cal)
    assert t.tier.iloc[0] == "A"


# ---- gates: a statistical edge alone is not a play ------------------------------------------------
def test_rlm_detected_only_when_the_line_moves_against_the_ticket_majority():
    # 72% of tickets on home, yet the home number moved from -3.5 to -2.5 (toward the away side)
    assert pe.rlm_state(+1.0, 0.72) == "toward_away"
    # the line following the crowd is not reverse movement
    assert pe.rlm_state(-1.0, 0.72) is None
    # not enough movement, or not enough of a ticket majority
    assert pe.rlm_state(+0.2, 0.72) is None and pe.rlm_state(+1.0, 0.55) is None
    # totals: 75% of tickets on the over with the total RISING is the line following the crowd
    assert pe.rlm_state(+1.5, 0.75, is_total=True) is None
    # the same crowd with the total FALLING is reverse movement
    assert pe.rlm_state(-1.5, 0.75, is_total=True) == "toward_under"
    assert pe.rlm_state(None, 0.72) is None and pe.rlm_state(1.0, None) is None


def test_lopsided_requires_both_tickets_and_money():
    assert pe.lopsided_side(0.74, 0.71, 0.70) == "home"
    assert pe.lopsided_side(0.20, 0.25, 0.70) == "away"
    assert pe.lopsided_side(0.74, 0.55, 0.70) is None      # tickets heavy but money is not
    assert pe.lopsided_side(None, 0.80, 0.70) is None


def test_gates_veto_lopsided_rlm_and_adverse_movement():
    ctx = {"marquee_ok": True, "marquee_why": "NFL", "home": "SEA", "away": "NE"}
    # a qualifying play now needs market evidence backing it, not merely an absence of red flags
    ok = {"market": "SPREAD", "tickets_pct_side": 0.45, "money_pct_side": 0.52, "_lopsided_side": None, "_rlm": None,
          "_move_against": 0.5, "signals": "money_agrees"}
    assert pe.apply_gates(dict(ok), ctx) == []
    lop = {**ok, "_lopsided_side": "home"}
    assert any("lopsided" in r for r in pe.apply_gates(lop, ctx))
    rlm = {**ok, "_rlm": "toward_away", "_rlm_against_us": True}
    assert any("reverse line movement" in r for r in pe.apply_gates(rlm, ctx))
    moved = {**ok, "_move_against": 2.0}
    assert any("moved 2.0 against" in r for r in pe.apply_gates(moved, ctx))


def test_gate_requires_splits_before_a_play_can_qualify():
    ctx = {"marquee_ok": True, "marquee_why": "NFL", "home": "SEA", "away": "NE"}
    no_splits = {"market": "SPREAD", "tickets_pct_side": None, "money_pct_side": None, "_lopsided_side": None, "_rlm": None, "_move_against": None}
    r = pe.apply_gates(no_splits, ctx)
    assert len(r) == 1 and "no betting splits" in r[0]


def test_marquee_keeps_nfl_and_filters_thin_college_games():
    teams = pd.DataFrame([{"team_id": "CFB_ALA", "conference": "SEC"}, {"team_id": "CFB_UTEP", "conference": "Conference USA"},
                          {"team_id": "CFB_UGA", "conference": "SEC"}]).set_index("team_id")
    g_big = pd.Series({"home_team_id": "CFB_ALA", "away_team_id": "CFB_UGA"})
    g_thin = pd.Series({"home_team_id": "CFB_ALA", "away_team_id": "CFB_UTEP"})
    assert pe.marquee("NFL", g_big, teams, {}, {}, True)[0] is True
    assert pe.marquee("CFB", g_big, teams, {}, {}, True)[0] is True                      # SEC vs SEC
    ok, why = pe.marquee("CFB", g_thin, teams, {}, {"CFB_ALA": 0.9, "CFB_UTEP": 0.1}, True)
    assert ok is False and "low-rated" in why
    assert pe.marquee("CFB", g_big, teams, {}, {}, False)[0] is False                    # no moneyline posted
    assert pe.marquee("CFB", g_thin, teams, {"CFB_UTEP": 25}, {}, True)[0] is True       # ranked team involved


def test_edge_alone_is_not_a_play():
    """The rule: a statistical edge AND market support. An edge with nothing backing it is rejected."""
    ctx = {"marquee_ok": True, "marquee_why": "NFL", "home": "SEA", "away": "NE"}
    base = {"market": "SPREAD", "tickets_pct_side": 0.48, "money_pct_side": 0.50, "_lopsided_side": None,
            "_rlm": None, "_move_against": 0.0}
    edge_only = {**base, "signals": ""}
    r = pe.apply_gates(edge_only, ctx)
    assert any("no market evidence" in x for x in r)
    # a favourable key number is positional, not market behaviour, so it does not confirm on its own
    assert any("no market evidence" in x for x in pe.apply_gates({**base, "signals": "key_number"}, ctx))
    # any one piece of market behaviour is enough
    for sig in ("money_agrees", "rlm_agrees", "line_agrees"):
        assert pe.apply_gates({**base, "signals": sig}, ctx) == [], sig


def test_reverse_line_movement_toward_our_side_confirms_rather_than_vetoes():
    """Money moving the number onto our side against the crowd is the strongest confirmation there is."""
    ctx = {"marquee_ok": True, "marquee_why": "NFL", "home": "SEA", "away": "NE"}
    ours = {"market": "SPREAD", "tickets_pct_side": 0.30, "money_pct_side": 0.55, "_lopsided_side": None,
            "_rlm": "toward_home", "_rlm_against_us": False, "_move_against": 0.0, "signals": "rlm_agrees"}
    assert pe.apply_gates(ours, ctx) == []
    against = {**ours, "_rlm_against_us": True, "signals": ""}
    assert any("reverse line movement" in x for x in pe.apply_gates(against, ctx))
