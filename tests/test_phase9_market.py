from __future__ import annotations
import numpy as np
import pandas as pd

from pipeline import market_engine as me, model as M


def test_no_vig_removes_overround():
    h, a = me.no_vig(-150, 130)
    assert abs(h + a - 1.0) < 1e-9 and 0.55 < h < 0.60
    assert me.no_vig(None, 130) == (None, None) and me.no_vig(-100000, 5000)[0] > 0.97


def test_key_numbers_crossed():
    assert me.key_numbers_crossed(-2.5, -3.0, me.KEY_NUMBERS_SPREAD) == [3]
    assert me.key_numbers_crossed(-4.0, -4.5, me.KEY_NUMBERS_SPREAD) == []
    assert me.key_numbers_crossed(-6.5, -7.5, me.KEY_NUMBERS_SPREAD) == [7]
    assert me.key_numbers_crossed(-3.0, -2.5, me.KEY_NUMBERS_SPREAD) == [3]          # moving off 3 also matters
    assert me.key_numbers_crossed(2.5, 3.5, me.KEY_NUMBERS_SPREAD) == [3]            # away favorites use absolute value
    assert me.key_numbers_crossed(-10.0, -10.0, me.KEY_NUMBERS_SPREAD) == []


def _hist(rows):
    df = pd.DataFrame(rows)
    df["retrieved_at"] = pd.to_datetime(df.retrieved_at, utc=True)
    for c in ("ml_home", "ml_away", "total", "provider_open_spread_home", "provider_open_total"):
        if c not in df.columns:
            df[c] = np.nan
    return df.sort_values("retrieved_at")


def test_movement_steam_and_notes():
    t0 = "2026-09-10T"
    rows = []
    for i, (sp_dk, sp_fd) in enumerate([(-2.5, -2.5), (-2.5, -2.5), (-3.0, -3.0), (-3.5, -3.5)]):
        rows.append({"game_id": "G", "book": "draftkings", "retrieved_at": f"{t0}{10+i:02d}:00:00Z", "spread_home": sp_dk, "total": 44.5, "ml_home": -150, "ml_away": 130})
        rows.append({"game_id": "G", "book": "fanduel", "retrieved_at": f"{t0}{10+i:02d}:05:00Z", "spread_home": sp_fd, "total": 44.0, "ml_home": -148, "ml_away": 126})
    h = _hist(rows)
    pred = pd.Series({"proj_margin_home": 6.0, "proj_total": 47.0, "win_prob_home": 0.66})
    a = me.analyze_game("NFL", h, pred, pd.Timestamp("2026-09-11T00:20:00Z"), pd.Timestamp("2026-09-10T14:00:00Z"))
    assert a["available"] and a["primary_book"] == "draftkings"
    assert a["open"]["spread_home"] == -2.5 and a["current"]["spread_home"] == -3.5 and a["movement"]["spread_points"] == -1.0
    assert a["movement"]["key_numbers_spread"] == [3]
    assert a["steam"] is not None and a["steam"]["direction"] == "toward_home" and set(a["steam"]["books"]) == {"draftkings", "fanduel"}
    assert a["model_vs_market"]["spread_diff"] == 2.5 and a["implied"]["home_win_prob_no_vig"] > 0.55
    assert a["public"] is None and any("crosses the key number" in n for n in a["notes"]) and any("unavailable" in n for n in a["notes"])
    # snapshots after kickoff are excluded
    late = _hist(rows + [{"game_id": "G", "book": "draftkings", "retrieved_at": "2026-09-11T01:00:00Z", "spread_home": -1.0, "total": 40.0}])
    a2 = me.analyze_game("NFL", late, pred, pd.Timestamp("2026-09-11T00:20:00Z"), pd.Timestamp("2026-09-11T02:00:00Z"))
    assert a2["current"]["spread_home"] == -3.5


def test_platt_calibration_and_score_floor():
    rng = np.random.default_rng(3)
    margin = rng.normal(0, 6, 4000); won = (margin * 1.8 + rng.normal(0, 10, 4000) > 0).astype(float)   # true slope steeper than 1
    a, b = M.fit_platt(margin, won)
    assert abs(a) < 0.15 and 0.2 < b < 0.4
    feats = pd.DataFrame({f: [0.0] for f in M.MARGIN_FEATURES}); feats["league"] = "CFB"; feats["game_id"] = "G"; feats["kickoff_utc"] = "x"
    for f in M.TOTAL_FEATURES:
        feats[f] = 0.0
    class Dummy:
        def __init__(self, v): self.v = v
        def predict(self, df): return np.array([self.v] * len(df))
        def contributions(self, df): return pd.DataFrame(0.0, index=df.index, columns=M.MARGIN_FEATURES)
    models = {"margin": Dummy(37.0), "total": Dummy(49.0), "sigma_margin": 16.0, "platt_a": 0.0, "platt_b": 0.12}
    p = M.predict_rows(models, feats, "t", True).iloc[0]
    assert p.proj_away_pts >= 10.0 and p.proj_home_pts - p.proj_away_pts == 37.0 and p.proj_total == 57.0
