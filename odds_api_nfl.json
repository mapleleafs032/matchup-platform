from __future__ import annotations
import json

import numpy as np
import pandas as pd
import pytest

import config
from pipeline import matchup_engine as me


def test_score_thresholds():
    assert me._score(None) is None and me._score(0.1) == 0 and me._score(0.5) == 1 and me._score(-1.0) == -2 and me._score(2.0) == 3 and me._score(-3.5) == -3


class _FakeWeek:
    """Minimal stand-in for matchup_engine.Week with hand-set z-scores and values."""
    def __init__(self, league="NFL"):
        self.league = league; self.zs = {}; self.vals = {}
        self.inj = pd.DataFrame(); self.depth = pd.DataFrame(); self.weather = pd.DataFrame(); self.qb = pd.DataFrame()
        self.season, self.week = 2026, 3
        self.games = pd.DataFrame()
    def z(self, t, gid, key, window="BLEND", adj="OPP_ADJ"):
        return self.zs.get((t, key))
    def val(self, t, gid, key, window="BLEND", adj="OPP_ADJ"):
        return self.vals.get((t, key))


def _game():
    return pd.Series({"game_id": "G", "home_team_id": "H", "away_team_id": "A", "neutral_site": False, "kickoff_utc": "2026-09-20T17:00:00Z"})


def test_unit_vs_unit_is_an_interaction():
    w = _FakeWeek(); g = _game()
    # great home offense (+2) vs great away defense (+2 = good D) -> nets to 0; away offense average vs bad home defense (-1) -> +1 for away
    w.zs.update({("H", "off_ppa_pass"): 2.0, ("A", "def_ppa_pass"): 2.0, ("A", "off_ppa_pass"): 0.0, ("H", "def_ppa_pass"): -1.0})
    home_edge, away_edge, inputs = me.unit_vs_unit(w, g, ["off_ppa_pass"], ["def_ppa_pass"])
    assert home_edge == 0.0 and away_edge == 1.0
    # missing metric on one side -> None, never 0
    w2 = _FakeWeek(); w2.zs.update({("H", "off_ppa_pass"): 2.0})
    assert me.unit_vs_unit(w2, g, ["off_ppa_pass"], ["def_ppa_pass"]) [0] is None


def test_style_fit_rewards_attacking_a_weakness():
    w = _FakeWeek(); g = _game()
    # Home passes a lot (+1.5); away pass defense is bad (def_ppa_pass z = -1.5, i.e. below-average D); run D fine (0).
    w.zs.update({("H", "off_pass_rate"): 1.5, ("A", "def_ppa_pass"): -1.5, ("A", "def_ppa_rush"): 0.0,
                 ("A", "off_pass_rate"): 0.0, ("H", "def_ppa_pass"): 0.0, ("H", "def_ppa_rush"): 0.0})
    raw, inp = me.cat_style_fit(w, g)
    assert raw is not None and raw > 0 and inp["home_terms"]["pass_lean_vs_pass_d"] > 0
    # Same tendencies but the away pass D is elite (+1.5): the lean now runs INTO strength -> negative
    w.zs[("A", "def_ppa_pass")] = 1.5
    raw2, _ = me.cat_style_fit(w, g)
    assert raw2 < 0 < raw


def test_injury_weighting_by_position_and_depth():
    w = _FakeWeek(); g = _game()
    w.inj = pd.DataFrame([
        {"season": 2026, "week": 3, "team_id": "H", "player_id": "HQB", "position": "QB", "status": "OUT", "player_name": "Home QB"},
        {"season": 2026, "week": 3, "team_id": "A", "player_id": "AWR2", "position": "WR", "status": "OUT", "player_name": "Away WR backup"},
        {"season": 2026, "week": 3, "team_id": "A", "player_id": "ALB", "position": "LB", "status": "QUESTIONABLE", "player_name": "ignored"},
    ])
    w.depth = pd.DataFrame([{"team_id": "H", "player_id": "HQB", "rank_in_slot": 1}, {"team_id": "A", "player_id": "AWR2", "rank_in_slot": 2}])
    raw, inp, un = me.cat_injury(w, g)
    assert not un and inp["home_burden"] == 3.0 and abs(inp["away_burden"] - 0.6 * 0.35) < 1e-9
    assert raw < 0                                   # home lost its QB -> away advantage (negative = away)
    assert len(inp["away"]) == 1                     # QUESTIONABLE rows are not counted


def test_weather_indoor_and_wind():
    w = _FakeWeek(); g = _game()
    w.weather = pd.DataFrame([{"game_id": "G", "retrieved_at": "2026-09-19T00:00:00Z", "is_indoor": True, "wind_mph": None, "wind_gust_mph": None, "temp_f": None, "precip_prob": None, "hours_to_kickoff": 20.0}])
    raw, inp, un = me.cat_weather(w, g); assert raw == 0.0 and inp["indoor"] and not un
    w.weather = pd.DataFrame([{"game_id": "G", "retrieved_at": "2026-09-19T00:00:00Z", "is_indoor": False, "wind_mph": 22.0, "wind_gust_mph": 30.0, "temp_f": 55.0, "precip_prob": 0.2, "hours_to_kickoff": 20.0}])
    w.zs.update({("H", "off_pass_rate"): -1.0, ("A", "off_pass_rate"): 1.0})   # home runs, away throws
    raw, inp, un = me.cat_weather(w, g); assert raw > 0 and inp["wind_factor"] == 1.0
    w.weather = pd.DataFrame(); assert me.cat_weather(w, g)[2] is True     # no forecast -> unavailable


def test_rest_context_and_edge():
    w = _FakeWeek(); g = _game()
    w.games = pd.DataFrame([
        {"game_id": "P1", "home_team_id": "H", "away_team_id": "X", "kickoff_utc": "2026-09-06T17:00:00Z", "neutral_site": False},
        {"game_id": "P2", "home_team_id": "Y", "away_team_id": "A", "kickoff_utc": "2026-09-13T17:00:00Z", "neutral_site": False},
        {"game_id": "P3", "home_team_id": "Z", "away_team_id": "A", "kickoff_utc": "2026-09-16T00:20:00Z", "neutral_site": False},
    ])
    raw, ctx = me.cat_rest(w, g)
    assert ctx["home"]["rest_days"] == 14 and ctx["home"]["off_bye"] and ctx["away"]["rest_days"] == 4 and ctx["away"]["short_week"]
    assert ctx["away"]["consecutive_road_before"] == 2 and raw > 0


@pytest.mark.live
def test_nfl_2025_week10_edges_exist():
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "data/tables/analytics/matchup_edges/NFL/2025/W10.parquet"
    if not p.exists():
        pytest.skip("run build_matchups NFL 2025 --weeks 10 first")
    e = pd.read_parquet(p)
    assert set(e.category) == set(me.CATEGORIES) and e.game_id.nunique() == 14
    hf = e[e.category == "HOME_FIELD"]
    non_neutral = hf[hf.margin_contribution > 0]
    assert non_neutral.margin_contribution.round(2).nunique() == 1          # league HFA in points, same for every true home game
    assert (hf[hf.game_id == "2025_NFL_W10_ATL_IND"].margin_contribution == 0).all()   # Berlin game: neutral site -> no HFA
