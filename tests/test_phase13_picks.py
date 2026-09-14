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


def test_losing_bands_are_excluded_and_tiers_order_by_score(tmp_path, monkeypatch):
    """The real CFB finding: the largest edges measured as losing outright. Those plays are dropped,
    and tiers stay ordered by score so A+ always means the largest qualifying disagreement."""
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(pe, "MODEL", tmp_path / "tables" / "model")
    d = tmp_path / "tables" / "model" / "backtest" / "CFB"; d.mkdir(parents=True)
    rows = []
    for _ in range(120):     # moderate band, roughly break-even
        rows.append({"season": 2024, "edge_vs_market": 3.5, "data_quality": 1.0, "model_ats_result": "WIN", "in_sample_warning": False})
    for _ in range(110):
        rows.append({"season": 2024, "edge_vs_market": 3.5, "data_quality": 1.0, "model_ats_result": "LOSS", "in_sample_warning": False})
    for _ in range(200):     # large band, decisively losing on a big sample
        rows.append({"season": 2024, "edge_vs_market": 6.5, "data_quality": 1.0, "model_ats_result": "WIN", "in_sample_warning": False})
    for _ in range(400):
        rows.append({"season": 2024, "edge_vs_market": 6.5, "data_quality": 1.0, "model_ats_result": "LOSS", "in_sample_warning": False})
    pd.DataFrame(rows).to_csv(d / "evaluation_CFB_v1.0.csv", index=False)
    cal = pe.calibrate("CFB")
    big = [b for b in cal["bands"] if b["lo"] >= 5.0][0]
    assert big["measurably_losing"] is True and cal["losing_bands"]
    plays = pd.DataFrame([{"market": "SPREAD", "edge_points": 6.5, "data_quality": 1.0, "signals": "money_agrees"},
                          {"market": "SPREAD", "edge_points": 3.5, "data_quality": 1.0, "signals": "money_agrees"}])
    t = pe.assign_tiers(pe.score(plays), cal)
    # assign_tiers flags the losing band; build_week is what drops and reports it
    assert bool(t[t.score_edge_only >= 5.0].band_measurably_losing.iloc[0]) is True
    assert bool(t[t.score_edge_only < 5.0].band_measurably_losing.iloc[0]) is False
    ok = t[~t.band_measurably_losing]
    assert len(ok) == 1 and pd.notna(ok.band_hit_rate.iloc[0]) and ok.band_n.iloc[0] > 0


def test_tier_record_pools_every_band_it_spans():
    """A tier spanning two bands must report the pooled result, not just the band at its floor."""
    bands = [{"lo": 4.0, "hi": 5.0, "n": 81, "hit_rate": 0.518, "ci_low": 0.41, "ci_high": 0.62,
              "beats_break_even": False, "significant": False, "measurably_losing": False},
             {"lo": 5.0, "hi": None, "n": 105, "hit_rate": 0.533, "ci_low": 0.44, "ci_high": 0.63,
              "beats_break_even": True, "significant": False, "measurably_losing": False}]
    t = pe.combine_bands(bands, 4.0, None)
    assert t["n"] == 186 and t["bands"] == 2
    assert abs(t["hit_rate"] - (0.518 * 81 + 0.533 * 105) / 186) < 1e-4
    thin = pe.combine_bands([{"lo": 1.4, "hi": 2.5, "n": 2, "hit_rate": 0.0, "ci_low": 0.0, "ci_high": 0.66,
                              "beats_break_even": False, "significant": False, "measurably_losing": True}], 1.4, 2.5)
    assert thin["n"] == 2 and thin["hit_rate"] is None      # two plays is not a measurement


def test_tier_labels_follow_score_not_lucky_bands(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(pe, "MODEL", tmp_path / "tables" / "model")
    cal = pe.calibrate("NFL")                                   # nothing measured
    plays = pd.DataFrame([{"market": "SPREAD", "edge_points": e, "data_quality": 1.0, "signals": "money_agrees"}
                          for e in (6.0, 3.0, 1.5)])
    t = pe.assign_tiers(pe.score(plays), cal)
    assert list(t.tier) == ["A+", "A", "B"] and t.score.is_monotonic_decreasing

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


def test_ncaa_passing_efficiency_formula():
    """The number ncaa.com ranks QBs by: (8.4*yds + 330*td + 100*cmp - 200*int) / att."""
    from pipeline.jobs.build_site import _passer_rating
    # a 300-yard, 3-TD, 0-INT, 20/30 game
    assert _passer_rating(20, 30, 300, 3, 0) == 183.7 == round((8.4 * 300 + 330 * 3 + 100 * 20) / 30, 1)
    # interceptions subtract
    assert _passer_rating(20, 30, 300, 3, 2) == round((8.4 * 300 + 330 * 3 + 100 * 20 - 200 * 2) / 30, 1)
    assert _passer_rating(0, 0, 0, 0, 0) is None          # no attempts -> unavailable, not a divide by zero


def test_espn_sos_manual_override_and_honest_source_label(tmp_path, monkeypatch):
    """ESPN's endpoint does not carry the SOS rank, so the number is either entered by hand or is our
    own. Whichever it is, the page must say so rather than claim a source it is not using."""
    import config
    from pipeline.jobs import build_site as bs
    monkeypatch.setattr(config, "DATA", tmp_path)
    (tmp_path / "manual").mkdir()

    class S:
        league, season = "CFB", 2026
    assert bs._espn_sos_manual(S()) == {}                       # no file -> nothing claimed
    (tmp_path / "manual" / "espn_sos.csv").write_text("team,sos_rank\n")
    assert bs._espn_sos_manual(S()) == {}                       # empty file is still nothing
    (tmp_path / "manual" / "espn_sos.csv").write_text("team,sos_rank\nCFB_TXST,1\nCFB_CLEM,5\n")
    m = bs._espn_sos_manual(S())
    assert m == {"CFB_TXST": 1, "CFB_CLEM": 5}                  # team_ids pass straight through


def test_site_json_never_contains_nan():
    """json.dumps writes a bare NaN by default, which is invalid JSON — a browser rejects the whole
    file, so one stray value blanks an entire page. Every site write goes through dumps()."""
    import json as _json
    import numpy as _np
    from pipeline.jobs.build_site import dumps
    payload = {"a": float("nan"), "b": _np.float64("nan"), "c": [1, float("nan")],
               "d": {"e": _np.int64(3), "f": pd.NaT}, "g": "fine", "h": True, "i": None}
    txt = dumps(payload)
    assert "NaN" not in txt and "Infinity" not in txt
    back = _json.loads(txt)                       # must parse, which a NaN would prevent
    assert back["a"] is None and back["b"] is None and back["c"] == [1, None]
    assert back["d"]["e"] == 3 and back["d"]["f"] is None
    assert back["g"] == "fine" and back["h"] is True and back["i"] is None


def test_grading_is_a_full_rescan_and_never_double_counts(tmp_path, monkeypatch):
    """A pick made days before kickoff must still be graded even if no run happened when the game
    ended. Grading re-scans every stored week and is keyed on pick_id, so repeats are free."""
    import config
    from pipeline.jobs import build_picks as bp
    from pipeline.log import JobRun
    import pipeline.log as L
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(bp, "MODEL", tmp_path / "tables" / "model")
    monkeypatch.setattr(L, "JOB_LOG", tmp_path / "tables" / "ops" / "job_log.csv")
    (tmp_path / "tables" / "results" / "NFL").mkdir(parents=True)
    pd.DataFrame([{"game_id": "G1", "away_score": 20, "home_score": 27, "margin_home": 7, "total": 47},
                  {"game_id": "G2", "away_score": 10, "home_score": 13, "margin_home": 3, "total": 23}]
                 ).to_csv(tmp_path / "tables" / "results" / "NFL" / "2026.csv", index=False)
    d = tmp_path / "tables" / "model" / "picks" / "NFL" / "2026"; d.mkdir(parents=True)
    mk = lambda pid, gid, side_home, line: {"pick_id": pid, "game_id": gid, "week": 1, "market": "SPREAD",
        "side": "H", "side_is_home": side_home, "line": line, "price": -110, "tier": "A", "score": 3.0,
        "edge_points": 3.0, "signals": ""}
    pd.DataFrame([mk("p1", "G1", True, -3.5)]).to_parquet(d / "W01.parquet")       # home -3.5, won by 7 -> WIN
    pd.DataFrame([mk("p2", "G2", False, 6.5)]).to_parquet(d / "W02.parquet")       # away +6.5, lost by 3 -> WIN
    with JobRun("PICKS", "NFL") as job:
        n = bp.grade("NFL", 2026, job)
    assert n == 2                                            # an older week is graded, not skipped
    ev = pd.read_csv(tmp_path / "tables" / "model" / "picks_evaluation" / "NFL" / "2026.csv")
    assert set(ev.result) == {"WIN"} and set(ev.pick_id) == {"p1", "p2"}
    with JobRun("PICKS", "NFL") as job:
        assert bp.grade("NFL", 2026, job) == 0               # re-running adds nothing


def test_leans_are_separated_from_ranked_plays():
    """A play whose only failing is missing market confirmation is a lean: shown, never tiered, never
    graded. The rule is edge AND market support — an edge alone cannot earn an A+."""
    from pipeline import picks_engine as pe
    rejected = pd.DataFrame([
        {"pick_id": "p1", "veto_reasons": pe.LEAN_ONLY_REASON},
        {"pick_id": "p2", "veto_reasons": f"{pe.LEAN_ONLY_REASON} | lopsided support"},
        {"pick_id": "p3", "veto_reasons": "reverse line movement against our side"},
    ])
    vetoed, leans = pe.split_leans(rejected)
    assert list(leans.pick_id) == ["p1"] and leans.tier.iloc[0] == "LEAN"
    assert set(vetoed.pick_id) == {"p2", "p3"}      # anything with a real veto is not a lean


def test_alias_warnings_are_deduplicated_but_counted():
    """Tens of thousands of identical alias warnings buried every other validation event."""
    from pipeline.log import ValidationLog
    v = ValidationLog("t", "roster")
    for _ in range(500):
        v.warn("ALIAS_UNMATCHED", "Villanova", "team", "Villanova", "known team")
    v.warn("RANGE", "x", "pct", 1.5, "0..1")
    assert len(v.rows) == 2                         # one per distinct alias, plus the unrelated warning
    v._apply_alias_counts()
    assert "seen on 500 rows" in v.rows[0]["expected"]
