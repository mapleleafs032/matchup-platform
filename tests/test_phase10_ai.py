from __future__ import annotations
import json

from pipeline import ai_agent, ai_package


def _pkg():
    return {"game": {"game_id": "G", "league": "NFL", "season": 2026, "week": 3, "kickoff_utc": "2026-09-20T17:00:00Z"},
            "teamA": {"name": "Away Team", "metrics_blend_opp_adjusted": {"off_ppa_play": {"label": "Offensive EPA/Play", "value": 0.124, "rank": 8, "pct": 0.77, "games": 2}},
                      "quarterback": {"name": "Away QB", "career_yards_per_attempt": 7.4}},
            "teamB": {"name": "Home Team", "metrics_blend_opp_adjusted": {"def_ppa_play": {"label": "Defensive EPA/Play", "value": -0.05, "rank": 12, "pct": 0.63, "games": 2}},
                      "quarterback": None},
            "model": {"projected_score": {"away": 24.5, "home": 21.0}, "projected_margin_home": -3.5, "projected_total": 45.5, "win_prob_home": 0.41},
            "market": {"available": True, "current": {"spread_home": 2.5, "total": 44.0, "retrieved_at": "x"}, "notes": ["n"]},
            "unavailable": ["quarterback_home"]}


def _sections(**over):
    base = {k: "Insufficient reliable data." for k in ai_agent.SECTIONS}
    base.update({"key_advantages": ["a", "b"], "key_concerns": ["c"],
                 "model_projection": "The model projects Away Team 24.5, Home Team 21.0 (margin -3.5, total 45.5), giving the home side a 41% win probability.",
                 "offensive_matchup": "Away Team's offense (0.124 EPA/play, 8th) meets a defense allowing -0.05 EPA/play (12th).",
                 "quarterback_edge": "Insufficient reliable data on the home starter; the away QB averages 7.4 yards per attempt in his career.",
                 "market_movement": "The current spread is home +2.5 with a total of 44."})
    base.update(over)
    return base


def test_hash_ignores_timestamps_and_notes():
    p1 = _pkg(); p2 = _pkg(); p2["market"]["current"]["retrieved_at"] = "y"; p2["market"]["notes"] = ["different"]
    assert ai_package.package_hash(p1) == ai_package.package_hash(p2)
    p3 = _pkg(); p3["model"]["projected_margin_home"] = -4.0
    assert ai_package.package_hash(p1) != ai_package.package_hash(p3)


def test_validate_accepts_package_numbers_and_percent_forms():
    v = ai_agent.validate(_sections(), _pkg())
    assert v["ok"], v


def test_validate_rejects_invented_numbers_and_confident_unavailable():
    s = _sections(recent_form="Home Team has averaged 31.7 points over its last three games.")     # 31.7 is not in the package
    v = ai_agent.validate(s, _pkg())
    assert not v["ok"] and "31.7" in v["violations"]
    s = _sections(quarterback_edge="The home QB is a proven veteran who should dominate.")         # unavailable topic without the required phrase
    v = ai_agent.validate(s, _pkg())
    assert "quarterback_edge" in v["confident_about_unavailable"]


class _FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs); self.calls = 0; self.model = "fake"
    def complete(self, system, user):
        self.calls += 1
        return self.outputs.pop(0), {"model": "fake", "tokens_in": 100, "tokens_out": 50}


def test_generate_retries_once_then_flags_failure():
    bad = json.dumps(_sections(recent_form="They scored 99.9 points."))
    good = json.dumps(_sections())
    out = ai_agent.generate(_pkg(), _FakeClient([bad, good]))
    assert not out["validation_failed"] and out["attempts"] == 2
    out = ai_agent.generate(_pkg(), _FakeClient([bad, bad]))
    assert out["validation_failed"] and "99.9" in out["validation"]["violations"] and out["sections"] is not None
    out = ai_agent.generate(_pkg(), _FakeClient(["not json at all", "still not"]))
    assert out["validation_failed"] and out["sections"] is None


def test_parse_sections_requires_exact_keys():
    good = json.dumps(_sections())
    assert ai_agent.parse_sections("```json\n" + good + "\n```") is not None
    partial = json.dumps({k: "x" for k in ai_agent.SECTIONS[:5]})
    assert ai_agent.parse_sections(partial) is None
