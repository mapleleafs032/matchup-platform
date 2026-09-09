from __future__ import annotations

import pandas as pd
import pytest

import config
from pipeline import ids, splits_engine
from providers import splits_manual


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(ids, "ALIASES_PATH", tmp_path / "tables" / "ref" / "team_aliases.csv")


def _resolver():
    r = ids.AliasResolver.load()
    r.add([{"provider": "vsin", "alias": a, "provider_id": None, "team_id": t, "season_from": None, "season_to": None}
           for a, t in (("Seattle Seahawks", "NFL_SEA"), ("New England Patriots", "NFL_NE"), ("Chicago Bears", "NFL_CHI"), ("Carolina Panthers", "NFL_CAR"))])
    return r


def _games():
    return pd.DataFrame([
        {"game_id": "2026_NFL_W01_NE_SEA", "away_team_id": "NFL_NE", "home_team_id": "NFL_SEA", "week": 1, "status": "SCHEDULED", "kickoff_utc": "2026-09-14T00:20:00Z", "season_type": "REG"},
        {"game_id": "2026_NFL_W01_CHI_CAR", "away_team_id": "NFL_CHI", "home_team_id": "NFL_CAR", "week": 1, "status": "SCHEDULED", "kickoff_utc": "2026-09-13T17:00:00Z", "season_type": "REG"},
    ])


PASTE = """2026-09-11T14:30
New England Patriots   38%  31%   44%  40%   35%  28%
Seattle Seahawks       62%  69%   56%  60%   65%  72%
Chicago Bears          71%  66%   48%  45%   74%  70%
Carolina Panthers      29%  34%   52%  55%   26%  30%
"""


def test_parse_paste_reads_teams_and_percentages():
    r = _resolver()
    rows, problems = splits_manual.parse_paste(PASTE, "NFL", r)
    assert len(rows) == 4 and not problems
    assert rows[0].team_id == "NFL_NE" and rows[0].percents[:2] == [38.0, 31.0]


def test_parse_paste_reports_unknown_team_instead_of_guessing():
    r = _resolver()
    rows, problems = splits_manual.parse_paste("Some Unknown Team  55%  45%  50%  50%  60%  40%\n", "NFL", r)
    assert not rows and len(problems) == 1 and problems[0]["why"] == "team not recognized"


def test_pair_rows_builds_home_side_percentages():
    r = _resolver()
    rows, _ = splits_manual.parse_paste(PASTE, "NFL", r)
    recs, problems = splits_manual.pair_rows(rows, _games(), "FULL", "draftkings", "2026-09-11T14:30:00+00:00", "manual_paste")
    assert len(recs) == 2 and not problems
    sea = [x for x in recs if x["game_id"] == "2026_NFL_W01_NE_SEA"][0]
    assert sea["spread_ticket_pct_home"] == 0.62 and sea["spread_money_pct_home"] == 0.69      # home row is Seattle
    assert sea["moneyline_ticket_pct_home"] == 0.65 and sea["total_money_pct_home"] == 0.60
    car = [x for x in recs if x["game_id"] == "2026_NFL_W01_CHI_CAR"][0]
    assert car["spread_ticket_pct_home"] == 0.29                                               # home row is Carolina


def test_pair_rows_rejects_percentages_that_do_not_sum_to_100():
    r = _resolver()
    bad = "New England Patriots  38%  31%\nSeattle Seahawks  40%  69%\n"      # 38+40 = 78
    rows, _ = splits_manual.parse_paste(bad, "NFL", r)
    recs, problems = splits_manual.pair_rows(rows, _games(), "FULL", "draftkings", "2026-09-11T14:30:00+00:00", "manual_paste")
    assert any("does not sum to 100" in p["why"] for p in problems)
    assert not recs or "spread_ticket_pct_home" not in recs[0]


def _hist(rows):
    d = pd.DataFrame(rows)
    d["retrieved_at"] = pd.to_datetime(d.retrieved_at, utc=True)
    return d.sort_values("retrieved_at")


def test_divergence_flags_tickets_against_money():
    h = _hist([{"game_id": "G", "period": "FULL", "book": "draftkings", "retrieved_at": "2026-09-11T12:00:00Z",
                "spread_ticket_pct_home": 0.74, "spread_money_pct_home": 0.48, "line_spread_home": -3.0, "line_total": 44.0}])
    a = splits_engine.analyze_game(h, "FULL", "SEA", "NE", pd.Timestamp("2026-09-14T00:20:00Z"))
    assert a["available"] and a["divergence"]["spread"]["notable"]
    assert a["latest"]["spread"]["ticket_side"] == "SEA" and a["latest"]["spread"]["money_side"] == "NE"
    assert any("only 48% of the money" in n for n in a["notes"])


def test_reverse_line_movement_detected_only_against_the_ticket_majority():
    # 72% of tickets on the home team, yet the home number moved from -3.5 toward +? (less negative) -> toward away
    h = _hist([{"game_id": "G", "period": "FULL", "book": "dk", "retrieved_at": "2026-09-11T12:00:00Z", "spread_ticket_pct_home": 0.70, "spread_money_pct_home": 0.55, "line_spread_home": -3.5, "line_total": 44.0},
               {"game_id": "G", "period": "FULL", "book": "dk", "retrieved_at": "2026-09-12T12:00:00Z", "spread_ticket_pct_home": 0.72, "spread_money_pct_home": 0.52, "line_spread_home": -2.5, "line_total": 44.0}])
    a = splits_engine.analyze_game(h, "FULL", "SEA", "NE", pd.Timestamp("2026-09-14T00:20:00Z"))
    assert "spread" in a["rlm"] and a["rlm"]["spread"]["crowd_side"] == "SEA" and a["rlm"]["spread"]["line_moved_toward"] == "NE"
    assert any("against the ticket majority" in n for n in a["notes"])
    # the same movement WITH the ticket majority is not flagged
    h2 = _hist([{"game_id": "G", "period": "FULL", "book": "dk", "retrieved_at": "2026-09-11T12:00:00Z", "spread_ticket_pct_home": 0.30, "spread_money_pct_home": 0.35, "line_spread_home": -3.5, "line_total": 44.0},
                {"game_id": "G", "period": "FULL", "book": "dk", "retrieved_at": "2026-09-12T12:00:00Z", "spread_ticket_pct_home": 0.28, "spread_money_pct_home": 0.33, "line_spread_home": -2.5, "line_total": 44.0}])
    assert "spread" not in splits_engine.analyze_game(h2, "FULL", "SEA", "NE", None)["rlm"]


def test_snapshots_after_kickoff_are_excluded():
    h = _hist([{"game_id": "G", "period": "FULL", "book": "dk", "retrieved_at": "2026-09-11T12:00:00Z", "spread_ticket_pct_home": 0.60, "spread_money_pct_home": 0.58, "line_spread_home": -3.0, "line_total": 44.0},
               {"game_id": "G", "period": "FULL", "book": "dk", "retrieved_at": "2026-09-15T12:00:00Z", "spread_ticket_pct_home": 0.90, "spread_money_pct_home": 0.90, "line_spread_home": -9.0, "line_total": 44.0}])
    a = splits_engine.analyze_game(h, "FULL", "SEA", "NE", pd.Timestamp("2026-09-14T00:20:00Z"))
    assert a["n_snapshots"] == 1 and a["latest"]["spread"]["ticket_pct_home"] == 0.60


def test_no_splits_reports_unavailable_rather_than_zero():
    a = splits_engine.analyze_game(pd.DataFrame(columns=["period", "retrieved_at"]), "1H", "SEA", "NE", None)
    assert a["available"] is False and a["latest"] == {} and a["series"] == []


def test_pair_resolves_to_the_upcoming_meeting_not_a_past_one():
    """Division rivals meet twice: a paste must attach to the game that has not kicked off yet."""
    r = _resolver()
    games = pd.DataFrame([
        {"game_id": "2026_NFL_W02_CHI_CAR", "away_team_id": "NFL_CHI", "home_team_id": "NFL_CAR", "week": 2, "status": "FINAL", "kickoff_utc": "2026-09-20T17:00:00Z", "season_type": "REG"},
        {"game_id": "2026_NFL_W14_CHI_CAR", "away_team_id": "NFL_CHI", "home_team_id": "NFL_CAR", "week": 14, "status": "SCHEDULED", "kickoff_utc": "2026-12-06T18:00:00Z", "season_type": "REG"},
    ])
    rows, _ = splits_manual.parse_paste("Chicago Bears 71% 66% 48% 45% 74% 70%\nCarolina Panthers 29% 34% 52% 55% 26% 30%\n", "NFL", r)
    recs, problems = splits_manual.pair_rows(rows, games, "FULL", "dk", "2026-12-03T15:00:00+00:00", "manual_paste")
    assert len(recs) == 1 and recs[0]["game_id"] == "2026_NFL_W14_CHI_CAR" and not problems
