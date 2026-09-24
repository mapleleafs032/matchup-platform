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
        {"market": "SPREAD", "edge_points": 4.0, "data_quality": 1.0, "signals": "", "signal_ages": {}},
        {"market": "SPREAD", "edge_points": 4.0, "data_quality": 0.5, "signals": "", "signal_ages": {}},
        {"market": "SPREAD", "edge_points": 40.0, "data_quality": 1.0, "signals": "", "signal_ages": {}},
    ])
    s = pe.score(d)
    assert s.model_component.iloc[1] < s.model_component.iloc[0]        # thin data lowers the model half
    assert s.score.iloc[1] < s.score.iloc[0]
    assert s.model_component.iloc[2] == 1.0                            # a 40-point disagreement caps, not celebrated
    assert s.score.iloc[2] == pytest.approx(config.PICK_SCORE_SCALE * config.PICK_WEIGHTS["model"])


def test_market_signals_only_help_and_are_bounded():
    d = pd.DataFrame([{"market": "SPREAD", "edge_points": 3.0, "data_quality": 1.0, "signals": "", "signal_ages": {}},
                      {"market": "SPREAD", "edge_points": 3.0, "data_quality": 1.0,
                       "signals": "rlm_agrees,money_agrees,key_number", "signal_ages": {"rlm_agrees": 2, "money_agrees": 2, "key_number": 2}}])
    s = pe.score(d)
    assert s.score.iloc[1] > s.score.iloc[0]
    assert s.market_component.iloc[0] == 0.0 and 0 < s.market_component.iloc[1] <= 1.0
    assert s.score.iloc[1] <= config.PICK_SCORE_SCALE


def test_tiers_are_ordered_and_weak_plays_are_dropped():
    sig = "steam,rlm_agrees,money_agrees"
    ages = {"steam": 2, "rlm_agrees": 2, "money_agrees": 2}
    d = pd.DataFrame([{"market": "SPREAD", "edge_points": e, "data_quality": 1.0,
                       "signals": sig if e > 1.0 else "", "signal_ages": ages if e > 1.0 else {}}
                      for e in (7.0, 3.0, 1.5, 0.5)])
    t = pe.assign_tiers(pe.score(d))
    assert t.tier.iloc[0] == "A+" and t.score.is_monotonic_decreasing
    # a flawless edge with no market support cannot be A+
    assert "A+" not in list(pe.assign_tiers(pe.score(pd.DataFrame(
        [{"market": "SPREAD", "edge_points": 7.0, "data_quality": 1.0, "signals": "", "signal_ages": {}}]))).tier)
    # nor can a barely-qualifying edge carried entirely by the market
    thin = pe.assign_tiers(pe.score(pd.DataFrame([{"market": "SPREAD", "edge_points": 1.5, "data_quality": 1.0,
        "signals": "steam,rlm_agrees,money_agrees", "signal_ages": {"steam": 2, "rlm_agrees": 2, "money_agrees": 2}}])))
    assert thin.tier.iloc[0] == "A"


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
    assert sum(b["n"] for b in cal["bands"]) == n
    assert all(not b["beats_break_even"] for b in cal["bands"])
    assert not cal["any_band_beats_break_even"]
    worst = max(cal["bands"], key=lambda b: b["n"])
    assert worst["hit_rate"] < 0.5 and worst["ci_high"] < pe.BREAK_EVEN   # decisively below break-even
    # no per-tier rate is quoted: tiers are half market, the bands measure edge alone
    assert cal["tier_history_available"] is False
    assert cal["tiers"]["A+"]["hit_rate"] is None


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
    plays = pd.DataFrame([{"market": "SPREAD", "edge_points": 6.5, "data_quality": 1.0, "signals": "money_agrees", "signal_ages": {"money_agrees": 2}},
                          {"market": "SPREAD", "edge_points": 3.5, "data_quality": 1.0, "signals": "money_agrees", "signal_ages": {"money_agrees": 2}}])
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
    plays = pd.DataFrame([{"market": "SPREAD", "edge_points": e, "data_quality": q, "signals": sg, "signal_ages": ag}
                          for e, q, sg, ag in ((7.0, 1.0, "steam,rlm_agrees,money_agrees", {"steam": 2, "rlm_agrees": 2, "money_agrees": 2}),
                                               (5.0, 1.0, "money_agrees", {"money_agrees": 30}),
                                               (2.0, 0.6, "line_agrees", {"line_agrees": 60}))])
    t = pe.assign_tiers(pe.score(plays), cal)
    assert t.tier.iloc[0] == "A+" and t.score.is_monotonic_decreasing
    assert set(t.tier) <= {"A+", "A", "B"}

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


def test_market_carries_half_the_score_so_edge_alone_cannot_reach_a_plus():
    """Early in a season the money knows more than a model running on last year's prior. Market
    behaviour is half the score by construction, not a bonus on top of the model."""
    import config
    from pipeline import picks_engine as pe
    rows = [
        {"market": "SPREAD", "edge_points": 7.0, "data_quality": 1.0, "signals": "", "signal_ages": {}},
        {"market": "SPREAD", "edge_points": 6.0, "data_quality": 0.95,
         "signals": "steam,rlm_agrees,money_agrees", "signal_ages": {"steam": 3, "rlm_agrees": 5, "money_agrees": 2}},
        {"market": "SPREAD", "edge_points": 2.0, "data_quality": 0.9,
         "signals": "steam,money_agrees", "signal_ages": {"steam": 2, "money_agrees": 4}},
    ]
    d = pe.score(pd.DataFrame(rows))
    edge_only, strong, weak_edge = d.iloc[0], d.iloc[1], d.iloc[2]
    # a flawless model edge with nothing from the market tops out at half the scale
    assert edge_only.score == pytest.approx(config.PICK_SCORE_SCALE * config.PICK_WEIGHTS["model"])
    assert edge_only.score < config.PICK_TIERS["A+"]        # and therefore cannot be A+
    assert strong.market_share >= 0.5                       # market supplies at least half where it is strong
    assert weak_edge.market_share > 0.5                     # a modest edge ranks mainly on the market


def test_late_signals_count_for_more_than_old_ones():
    from pipeline import picks_engine as pe
    late = pe.market_component("steam", {"steam": 2})
    old = pe.market_component("steam", {"steam": 200})
    assert late > old
    assert pe.market_component("steam,rlm_agrees,money_agrees,line_agrees", {k: 1 for k in
           ("steam", "rlm_agrees", "money_agrees", "line_agrees")}) == 1.0     # saturates, never runs away


def test_only_moves_toward_our_side_count_as_support():
    """Steam pushing the number away from us is not confirmation."""
    from pipeline import picks_engine as pe
    kick = pd.Timestamp("2026-09-14T23:00:00Z")
    events = [{"market": "spread", "kind": "steam", "toward_home": True, "t": "2026-09-14T20:00:00Z"},
              {"market": "spread", "kind": "rlm", "toward_home": False, "t": "2026-09-14T21:00:00Z"}]
    ours = pe.signal_ages_from_events(events, "SPREAD", True, kick)
    assert "steam" in ours and "rlm_agrees" not in ours       # the away-side RLM is not our signal
    theirs = pe.signal_ages_from_events(events, "SPREAD", False, kick)
    assert "rlm_agrees" in theirs and "steam" not in theirs


def _synthetic_league(prior_sd, seed=7, seasons=(2021, 2022, 2023, 2024, 2025)):
    import numpy as np
    from pipeline import model as M
    rng = np.random.default_rng(seed)
    out = {}
    for season in seasons:
        rows = []
        for wk in range(1, 15):
            for g in range(16):
                th, ta = rng.normal(0, 6), rng.normal(0, 6)
                noise = 8.0 / np.sqrt(wk)
                r = {f: 0.0 for f in M.MARGIN_FEATURES}; r.update({f: 0.0 for f in M.TOTAL_FEATURES})
                r.update({"game_id": f"{season}_{wk}_{g}", "season": season, "week": wk, "home_field": 1.0,
                          "kickoff_utc": f"{season}-10-01T17:00:00Z", "league": "NFL",
                          "cur_rating_home": th + rng.normal(0, noise), "cur_rating_away": ta + rng.normal(0, noise),
                          "prior_rating_home": th + rng.normal(0, prior_sd), "prior_rating_away": ta + rng.normal(0, prior_sd),
                          "continuity_home": 0.85, "continuity_away": 0.85,
                          "margin_home": (th - ta) + 2.0 + rng.normal(0, 10), "total": 44.0})
                rows.append(r)
        out[season] = pd.DataFrame(rows)
    return out


def test_blend_sweep_finds_the_truth_in_either_direction():
    """The sweep must pick what history supports, not what anyone hoped: slower decay where last
    season is informative, faster where it is not."""
    from pipeline import model as M
    from pipeline.jobs import tune_blend as TB
    base = M.blend_policy("NFL")["schedule"]
    def mae(raw, k):
        pol = {"schedule": TB.stretched_schedule(base, k), "continuity_strength": 0.0, "early_weeks": 0}
        return TB.score(TB.walk_forward(raw, "NFL", pol))["mae"]
    informative = _synthetic_league(prior_sd=1.5)
    assert mae(informative, 2.0) < mae(informative, 1.0) < mae(informative, 0.75)
    weak = _synthetic_league(prior_sd=9.0)
    assert mae(weak, 0.75) < mae(weak, 1.0) < mae(weak, 2.0)


def test_blend_sweep_keeps_current_policy_when_the_gain_is_noise(tmp_path, monkeypatch):
    """With no real signal to find, nothing may be adopted: a candidate has to beat the current policy
    by a meaningful margin AND in most seasons."""
    import config
    from pipeline.jobs import tune_blend as TB
    from pipeline.log import JobRun
    import pipeline.log as L
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(L, "JOB_LOG", tmp_path / "tables" / "ops" / "job_log.csv")
    flat = _synthetic_league(prior_sd=4.0, seed=11)
    with JobRun("TUNE", "NFL") as job:
        v = TB.run("NFL", apply=True, job=job, raw=flat, grid=[(1.0, 0.0, 0), (1.02, 0.0, 0)])
    assert v["adopt"] is False                              # a near-identical curve is not an improvement
    assert not (tmp_path / "tables" / "model" / "blend_policy.json").exists()


def _write_lines(tmp_path, league, season, n, flip, seed=3):
    import numpy as np
    import config
    from pipeline import storage
    rng = np.random.default_rng(seed)
    true = rng.normal(0, 10, n)
    spread = -np.round(true + rng.normal(0, 7, n))            # honest line: negative when home is better
    margin = np.round(true + rng.normal(0, 12, n))
    if flip:
        spread = -spread                                       # the bug: every favourite made an underdog
    storage.write_parquet(config.TABLES / "market" / "closing_lines" / league / f"{season}.parquet",
                          pd.DataFrame({"game_id": [f"G{i}" for i in range(n)], "spread_home": spread, "book": "x"}))
    (config.TABLES / "results" / league).mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"game_id": [f"G{i}" for i in range(n)], "margin_home": margin}).to_csv(
        config.TABLES / "results" / league / f"{season}.csv", index=False)


def test_line_audit_detects_a_reversed_season_and_only_fixes_a_confirmed_one(tmp_path, monkeypatch):
    """The CFB 2023 result (34.5% against the close on 730 games) is the signature of spreads stored
    with their sign reversed. The audit must find it, and the fix must refuse a healthy season."""
    import config
    from pipeline.jobs import audit_lines as AL
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    _write_lines(tmp_path, "CFB", 2022, 600, flip=False)
    _write_lines(tmp_path, "CFB", 2023, 600, flip=True)
    healthy, reversed_ = AL.season_audit("CFB", 2022), AL.season_audit("CFB", 2023)
    assert healthy["verdict"] == "healthy" and healthy["corr_market_vs_result"] > 0.3
    assert reversed_["verdict"] == "SIGNS REVERSED" and reversed_["corr_market_vs_result"] < -0.3
    assert AL.fix_sign("CFB", 2022) is False                   # a healthy season is never touched
    assert AL.fix_sign("CFB", 2023) is True
    assert AL.season_audit("CFB", 2023)["verdict"] == "healthy"
    backups = list((tmp_path / "tables" / "market" / "closing_lines" / "CFB").glob("2023.before_sign_fix_*.parquet"))
    assert len(backups) == 1                                  # the original is preserved


def test_an_analysis_older_than_the_projection_is_withheld():
    """The Giants at Rams contradiction: text written before Week 1 said Rams by 13.5 while the header,
    rebuilt afterwards, said Rams by 4. An analysis may never sit under a projection it predates."""
    from pipeline.jobs.build_site import ai_current_or_stale
    ai = {"withheld": False, "sections": {"model_projection": "Rams by 13.49"}, "generated_at": "2026-09-08T12:00:00Z"}
    stale = ai_current_or_stale(ai, {"predicted_at": "2026-09-19T12:00:00Z"})
    assert stale["withheld"] is True and stale["stale"] is True and "sections" not in stale
    assert ai_current_or_stale(ai, {"predicted_at": "2026-09-07T12:00:00Z"}) is ai    # current analysis still shown


def _edge_league(tmp_path, seed, edge):
    import numpy as np
    import config
    rng = np.random.default_rng(seed); rows = []
    for season in (2021, 2022, 2023, 2024, 2025):
        for i in range(800):
            spread = float(rng.choice([-10.5, -7, -6.5, -3.5, -3, -1, 1, 3, 3.5, 6.5, 7, 10.5]))
            side_home = bool(rng.random() < 0.5); lfs = spread if side_home else -spread
            p = edge if (side_home and lfs > 0) else 0.50
            rows.append({"game_id": f"{season}_{i}", "season": season, "week": int(rng.integers(1, 18)),
                         "close_spread_home": spread, "close_total": 44.0, "proj_total": 44.0 + rng.normal(0, 3),
                         "model_side_home": side_home, "model_ats_result": "WIN" if rng.random() < p else "LOSS",
                         "model_ou_result": "WIN" if rng.random() < .5 else "LOSS", "edge_vs_market": abs(rng.normal(0, 2.5))})
    d = tmp_path / f"s{seed}" / "model" / "backtest" / "NFL"; d.mkdir(parents=True)
    pd.DataFrame(rows).to_csv(d / "evaluation_NFL_v1.0.csv", index=False)
    return tmp_path / f"s{seed}"


def test_edge_slicing_finds_real_edges_and_rejects_luck(tmp_path, monkeypatch):
    """Slicing manufactures edges by chance. Clearing break-even twice let 1.5 coin-flip slices through
    per run; with a significance test corrected for the number of slices, a league with no edge yields
    none, while a planted edge is still found."""
    import contextlib, io
    import config
    from pipeline.jobs import analyze_edges as AE
    found = false_in_null = 0
    for seed in range(6):
        monkeypatch.setattr(config, "TABLES", _edge_league(tmp_path, seed, 0.58))
        with contextlib.redirect_stdout(io.StringIO()):
            found += "home underdog pick" in AE.analyze("NFL")["held"]
        monkeypatch.setattr(config, "TABLES", _edge_league(tmp_path, 100 + seed, 0.50))
        with contextlib.redirect_stdout(io.StringIO()):
            false_in_null += len(AE.analyze("NFL")["held"])
    assert found >= 4                 # a real 58% edge is found most of the time
    assert false_in_null == 0         # a league with no edge produces no "edges"


class _Pick(dict):
    __getattr__ = dict.get


def test_closing_line_value_is_measured_from_our_side():
    """Positive means we beat the close: more points taken, fewer laid, a better total or price."""
    from pipeline.jobs.build_picks import closing_line_value as clv
    assert clv(_Pick(market="SPREAD", side_is_home=False, line=3.5), {"spread_home": -2.5})["clv_points"] == 1.0
    assert clv(_Pick(market="SPREAD", side_is_home=True, line=-3.0), {"spread_home": -4.0})["clv_points"] == 1.0
    worse = clv(_Pick(market="SPREAD", side_is_home=True, line=-3.0), {"spread_home": -2.0})
    assert worse["clv_points"] == -1.0 and worse["beat_close"] is False
    assert clv(_Pick(market="TOTAL", side="Over", line=44.0), {"total": 45.5})["clv_points"] == 1.5
    assert clv(_Pick(market="TOTAL", side="Under", line=44.0), {"total": 45.5})["clv_points"] == -1.5
    assert clv(_Pick(market="MONEYLINE", side_is_home=True, price=150), {"ml_home": 130})["clv_prob"] > 0
    assert clv(_Pick(market="SPREAD", side_is_home=True, line=-3.0), None)["beat_close"] is None


def test_line_shopping_finds_the_best_number_for_our_side():
    """The one improvement that needs no prediction: take the best price on offer."""
    from pipeline.picks_engine import best_available
    books = pd.DataFrame([
        {"game_id": "G", "book": "draftkings", "spread_home": -3.0, "total": 44.5, "ml_home": -150, "ml_away": 130},
        {"game_id": "G", "book": "fanduel", "spread_home": -2.5, "total": 45.5, "ml_home": -145, "ml_away": 125},
        {"game_id": "G", "book": "betmgm", "spread_home": -3.5, "total": 44.0, "ml_home": -160, "ml_away": 135}])
    home = best_available(books, "G", "SPREAD", True, None, -3.0)
    assert home["best_book"] == "fanduel" and home["best_line"] == -2.5 and home["line_gain"] == 0.5
    away = best_available(books, "G", "SPREAD", False, None, 3.0)
    assert away["best_book"] == "betmgm" and away["line_gain"] == 0.5
    assert best_available(books, "G", "TOTAL", None, True, 44.5)["best_line"] == 44.0      # over wants it low
    assert best_available(books, "G", "TOTAL", None, False, 44.5)["best_line"] == 45.5     # under wants it high
    assert best_available(books, "G", "MONEYLINE", False, None, 130)["best_line"] == 135.0
    assert best_available(pd.DataFrame(), "G", "SPREAD", True, None, -3.0)["books_compared"] == 0


def test_win_probability_starts_at_the_spread_and_ends_at_the_result():
    """The in-game curve opens where the spread puts it and closes on the final score."""
    from pipeline import winprob
    assert abs(winprob.stern_home_wp(0, 3600, -7, "NFL") - 0.693) < 0.01      # 7-point favourite
    assert winprob.stern_home_wp(0, 3600, 0, "NFL") == 0.5
    assert winprob.stern_home_wp(0, 3600, -7, "CFB") < winprob.stern_home_wp(0, 3600, -7, "NFL")
    plays = pd.DataFrame([{"offense_team_id": "H", "game_sec_remaining": 3600 - t, "score_diff_pre": 0,
                           "period": t // 900 + 1, "wp_pre": None} for t in range(0, 3600, 60)])
    pts = winprob.series(plays, "CFB", "H", "A", -3.5, 21, 17)
    assert pts[0]["wp"] > 0.5 and pts[-1]["wp"] == 1.0
    # NFL uses nflfastR's value, flipped to the home side when the away team has the ball
    nfl = pd.DataFrame([{"offense_team_id": "A", "game_sec_remaining": 1800, "score_diff_pre": 7,
                         "period": 3, "wp_pre": 0.80}])
    assert winprob.series(nfl, "NFL", "H", "A", 0, None, None)[1]["wp"] == 0.2


def _nfl_plays(weeks=(1, 2, 3), n_games=4, seed=4):
    import numpy as np
    rng = np.random.default_rng(seed); out = {}
    for wk in weeks:
        rows = []
        for g in range(n_games):
            for team in (f"T{2*g}", f"T{2*g+1}"):
                for k in range(60):
                    r = {"play_id": f"{wk}_{team}_{k}", "game_id": f"W{wk}_G{g}", "offense_team_id": team,
                         "passer_id": None, "rusher_id": None, "receiver_id": None, "ppa": rng.normal(0, 1),
                         "is_success": bool(rng.random() < .45), "yards_gained": int(rng.integers(-2, 20)),
                         "is_dropback": False, "is_sack": False, "is_scramble": False, "is_complete": False,
                         "air_yards": None, "cpoe": None, "is_garbage_time": False}
                    u = rng.random()
                    if u < .55:
                        r.update(is_dropback=True, passer_id=f"{team}_QB", receiver_id=f"{team}_WR{rng.integers(1,5)}",
                                 is_complete=bool(rng.random() < .65), air_yards=float(rng.integers(0, 25)), cpoe=rng.normal(0, 8))
                    elif u < .60:
                        r.update(is_dropback=True, passer_id=f"{team}_QB", is_sack=True)
                    elif u < .63:
                        r.update(is_dropback=True, is_scramble=True, rusher_id=f"{team}_QB")
                    else:
                        r.update(rusher_id=f"{team}_RB{rng.integers(1,3)}")
                    rows.append(r)
        out[wk] = pd.DataFrame(rows)
    return out


def test_player_metrics_are_as_of_and_attributed_correctly(tmp_path, monkeypatch):
    """Game-on-Paper-style player metrics from our own play-by-play: nothing from later weeks, scrambles
    belong to the quarterback, and a receiver's target share is out of his own team's targets."""
    import config
    from pipeline import storage, player_metrics as PM
    monkeypatch.setattr(config, "TABLES", tmp_path / "t")
    monkeypatch.setattr(PM, "STATS", tmp_path / "t" / "stats")
    for wk, df in _nfl_plays().items():
        storage.write_parquet(tmp_path / "t" / "stats" / "plays" / "NFL" / "2026" / f"W{wk:02d}.parquet", df)
    df = PM.nfl_players(2026, 3)
    assert set(df.games.unique()) == {2}                                  # week 3 itself is never counted
    assert not df[df.role == "rusher"].player_id.str.contains("_QB").any()   # scrambles are passing plays
    assert (df[df.role == "receiver"].groupby("team_id").target_share.sum().round(6) == 1.0).all()
    qb = df[df.role == "qb"]
    assert qb.epa_per_dropback_rank.notna().all() and qb.epa_per_dropback_rank.min() == 1
    keys = PM.team_key_players(df, "T0")
    assert len(keys["qb"]) == 1 and 1 <= len(keys["rusher"]) <= 3 and 1 <= len(keys["receiver"]) <= 4


def test_low_volume_players_are_not_ranked():
    """A back with four carries is not ranked against one with two hundred."""
    from pipeline import player_metrics as PM
    df = pd.DataFrame([
        {"player_id": "A", "team_id": "T", "role": "rusher", "volume": 200, "games": 10, "epa_per_rush": 0.10},
        {"player_id": "B", "team_id": "T", "role": "rusher", "volume": 4, "games": 10, "epa_per_rush": 0.90}])
    out = PM._add_ranks(df)
    assert out.loc[out.player_id == "A", "epa_per_rush_rank"].iloc[0] == 1
    assert pd.isna(out.loc[out.player_id == "B", "epa_per_rush_rank"].iloc[0])      # huge rate, tiny sample: unranked


def test_college_box_keeps_rushing_receiving_and_defence():
    """These arrive in the same CFBD response as the QB rows and were being discarded."""
    from pipeline import ids
    from providers import cfbd_stats
    from datetime import datetime, timezone
    games = pd.DataFrame([{"game_id": "2026_CFB_W03_AAA_BBB", "home_team_id": "CFB_BBB", "away_team_id": "CFB_AAA",
                           "kickoff_utc": pd.Timestamp("2026-09-20T17:00:00Z"), "provider_game_ids": '{"cfbd":9}'}])
    r = ids.AliasResolver.load()
    r.add([{"provider": "cfbd", "alias": "Bravo", "provider_id": None, "team_id": "CFB_BBB", "season_from": None, "season_to": None}])
    payload = [{"id": 9, "teams": [{"team": "Bravo", "categories": [
        {"name": "passing", "types": [{"name": "C/ATT", "athletes": [{"id": 1, "name": "Q B", "stat": "20/30"}]},
                                      {"name": "YDS", "athletes": [{"id": 1, "name": "Q B", "stat": "250"}]}]},
        {"name": "rushing", "types": [{"name": "CAR", "athletes": [{"id": 2, "name": "R B", "stat": "18"}, {"id": 1, "name": "Q B", "stat": "5"}]},
                                      {"name": "YDS", "athletes": [{"id": 2, "name": "R B", "stat": "96"}, {"id": 1, "name": "Q B", "stat": "30"}]}]},
        {"name": "receiving", "types": [{"name": "REC", "athletes": [{"id": 3, "name": "W R", "stat": "7"}]},
                                        {"name": "YDS", "athletes": [{"id": 3, "name": "W R", "stat": "112"}]}]},
        {"name": "defensive", "types": [{"name": "TOT", "athletes": [{"id": 4, "name": "L B", "stat": "9"}]}]}]}]}]
    df = cfbd_stats.normalize_player_box(payload, games, r, datetime.now(timezone.utc), set())
    assert not df.empty, "the fixture's game did not resolve"
    by = df.set_index("player_id")
    assert by.loc["CFB_P_1"].pass_att == 30 and by.loc["CFB_P_1"].rush_att == 5      # QB rushing kept on his row
    assert by.loc["CFB_P_2"].rush_yds == 96 and by.loc["CFB_P_3"].rec_yds == 112
    assert by.loc["CFB_P_4"].tackles == 9


def test_starter_detection_only_considers_players_who_threw():
    """The box table now holds every player. A game missing its QB row must not crown a receiver."""
    import inspect
    from pipeline import roster_engine
    src = inspect.getsource(roster_engine)
    assert "pass_att" in src and "> 0]" in src


def test_closing_line_is_chosen_per_game_not_per_season(tmp_path, monkeypatch):
    """The CFB 2023 "anomaly": one book was chosen for the whole season and every game it did not cover
    lost its closing line, so the season's ATS was scored on about three dozen games. The best book must
    be chosen game by game."""
    import config
    from pipeline import storage
    from pipeline.jobs import backtest as BT
    monkeypatch.setattr(config, "TABLES", tmp_path / "t")
    rows = [{"game_id": f"G{i}", "book": "bovada", "spread_home": -3.0, "total": 50.0} for i in range(800)]
    rows += [{"game_id": f"G{i}", "book": "consensus", "spread_home": -3.5, "total": None} for i in range(36)]
    storage.write_parquet(tmp_path / "t" / "market" / "closing_lines" / "CFB" / "2023.parquet", pd.DataFrame(rows))
    c = BT.closing("CFB", 2023).set_index("game_id")
    assert c.close_spread_home.notna().sum() == 800                      # the old rule kept 36
    assert c.loc["G0"].close_spread_home == -3.5                          # the preferred book still wins where present
    assert c.loc["G500"].close_spread_home == -3.0                        # and another book fills in where it is absent
    assert c.loc["G0"].close_total == 50.0                                # a missing total is filled independently


def _cohort_rows(tmp_path, wins, losses, cid="cfb_totals_edge4"):
    import config
    from pipeline import cohorts as C
    d = tmp_path / "t" / "model" / "cohorts"; d.mkdir(parents=True, exist_ok=True)
    rows = [{"cohort_id": cid, "game_id": f"W{i}", "result": "WIN", "line_moved_toward_model": True} for i in range(wins)]
    rows += [{"cohort_id": cid, "game_id": f"L{i}", "result": "LOSS", "line_moved_toward_model": False} for i in range(losses)]
    pd.DataFrame(rows).to_csv(d / f"{cid}.csv", index=False)


def test_forward_test_verdict_is_withheld_until_the_registered_sample(tmp_path, monkeypatch):
    """Checking daily and stopping when the numbers look good 'confirms' noise. A hot start must not
    produce a verdict before the pre-registered sample size."""
    import config
    from pipeline import cohorts as C
    monkeypatch.setattr(config, "TABLES", tmp_path / "t")
    monkeypatch.setattr(C, "COHORT_DIR", tmp_path / "t" / "model" / "cohorts")
    cohort = next(c for c in config.COHORTS if c["id"] == "cfb_totals_edge4")
    _cohort_rows(tmp_path, 40, 10)                                   # 80% on 50 games: looks spectacular
    s = C.summary(cohort)
    assert s["verdict"] == "collecting" and s["rate"] == 0.8 and "withheld" in s["verdict_text"]


def test_forward_test_confirms_or_rejects_only_at_the_decision_point(tmp_path, monkeypatch):
    import config
    from pipeline import cohorts as C
    monkeypatch.setattr(config, "TABLES", tmp_path / "t")
    monkeypatch.setattr(C, "COHORT_DIR", tmp_path / "t" / "model" / "cohorts")
    cohort = next(c for c in config.COHORTS if c["id"] == "cfb_totals_edge4")
    _cohort_rows(tmp_path, 130, 70)                                  # 65% on 200: well clear of break-even
    assert C.summary(cohort)["verdict"] == "confirmed"
    _cohort_rows(tmp_path, 106, 94)                                  # 53% on 200: above break-even, but within luck
    s = C.summary(cohort)
    assert s["verdict"] == "not confirmed" and s["p_value"] > s["alpha"]


def test_forward_test_membership_matches_how_the_backtest_measured_it(tmp_path, monkeypatch):
    """Final pregame projection against the closing total, no market filter, and nothing predicted after
    kickoff may count."""
    import config
    from pipeline import storage, cohorts as C
    monkeypatch.setattr(config, "TABLES", tmp_path / "t")
    monkeypatch.setattr(C, "COHORT_DIR", tmp_path / "t" / "model" / "cohorts")
    K = "2026-09-26T17:00:00Z"                     # after the cohorts' 2026-09-21 registration
    storage.write_parquet(storage.games_path("CFB", 2026), pd.DataFrame(
        [{"game_id": g, "week": 3, "season": 2026, "season_type": "REG", "kickoff_utc": K} for g in ("A", "B", "F")]
        + [{"game_id": "P", "week": 3, "season": 2026, "season_type": "REG", "kickoff_utc": "2026-09-13T17:00:00Z"}]))
    (tmp_path / "t" / "model" / "predictions" / "CFB").mkdir(parents=True)
    pd.DataFrame([{"prediction_id": "a", "game_id": "A", "predicted_at": "2026-09-25T12:00:00Z", "proj_total": 58.0, "proj_margin_home": 0},
                  {"prediction_id": "b", "game_id": "B", "predicted_at": "2026-09-25T12:00:00Z", "proj_total": 52.0, "proj_margin_home": 0},
                  {"prediction_id": "f", "game_id": "F", "predicted_at": "2026-09-26T19:00:00Z", "proj_total": 70.0, "proj_margin_home": 0},
                  {"prediction_id": "p", "game_id": "P", "predicted_at": "2026-09-12T12:00:00Z", "proj_total": 60.0, "proj_margin_home": 0}]
                 ).to_csv(tmp_path / "t" / "model" / "predictions" / "CFB" / "2026.csv", index=False)
    (tmp_path / "t" / "market" / "snapshots" / "CFB" / "2026").mkdir(parents=True)
    pd.DataFrame([{"game_id": g, "retrieved_at": "2026-09-26T16:00:00Z", "book": "draftkings", "total": 50.0, "spread_home": -3}
                  for g in ("A", "B", "F")] + [{"game_id": "P", "retrieved_at": "2026-09-13T16:00:00Z", "book": "draftkings",
                  "total": 50.0, "spread_home": -3}]).to_csv(tmp_path / "t" / "market" / "snapshots" / "CFB" / "2026" / "W03.csv", index=False)
    (tmp_path / "t" / "results" / "CFB").mkdir(parents=True)
    pd.DataFrame([{"game_id": g, "total": 60, "margin_home": 1} for g in ("A", "B", "F", "P")]
                 ).to_csv(tmp_path / "t" / "results" / "CFB" / "2026.csv", index=False)
    C.update("CFB", 2026)
    g = storage.read_table(tmp_path / "t" / "model" / "cohorts" / "cfb_totals_edge4.csv").set_index("game_id")
    assert list(g.index) == ["A"]   # B's edge is 2; F's only projection came after kickoff;
                                    # P qualified (edge 10) but kicked off before the hypothesis was registered
    assert g.loc["A"].side == "OVER" and g.loc["A"].result == "WIN"
    assert C.update("CFB", 2026)["cfb_totals_edge4"]["newly_graded"] == 0      # graded once, never rewritten


def test_empty_dict_columns_do_not_break_the_parquet_write(tmp_path):
    """Parquet cannot store a struct with no fields, so a dict column that is empty on every row failed
    the whole write and took the picks job down. Dict and list columns are serialised instead."""
    from pipeline import storage, picks_engine as pe
    storage.write_parquet(tmp_path / "empty.parquet",
                          pd.DataFrame([{"pick_id": "a", "signal_ages": {}}, {"pick_id": "b", "signal_ages": {}}]))
    assert (tmp_path / "empty.parquet").exists()
    storage.write_parquet(tmp_path / "mixed.parquet",
                          pd.DataFrame([{"pick_id": "a", "signal_ages": {"steam": 2.0}, "signals": "steam"},
                                        {"pick_id": "b", "signal_ages": {}, "signals": ""}]))
    back = pd.read_parquet(tmp_path / "mixed.parquet")
    assert back.signal_ages.iloc[0] == '{"steam": 2.0}'
    # and the scorer accepts either the JSON text or a live dict
    scored = pe.score(back.assign(market="SPREAD", edge_points=4.0, data_quality=1.0))
    assert scored.market_component.iloc[0] > 0 and scored.market_component.iloc[1] == 0
    storage.write_parquet(tmp_path / "lists.parquet", pd.DataFrame([{"a": []}, {"a": [1, 2]}]))
    assert pd.read_parquet(tmp_path / "lists.parquet").a.tolist() == ["[]", "[1, 2]"]
