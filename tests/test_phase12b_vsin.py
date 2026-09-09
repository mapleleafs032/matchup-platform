from __future__ import annotations
import pathlib

import pandas as pd
import pytest

import config
from pipeline import ids
from providers import vsin

FIX = pathlib.Path(__file__).parent / "fixtures" / "vsin_nfl.html"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(ids, "ALIASES_PATH", tmp_path / "tables" / "ref" / "team_aliases.csv")


def _teams():
    return pd.DataFrame([
        {"team_id": "NFL_NE", "league": "NFL", "abbr": "NE", "school_or_city": "New England", "mascot": "Patriots", "display_name": "New England Patriots"},
        {"team_id": "NFL_SEA", "league": "NFL", "abbr": "SEA", "school_or_city": "Seattle", "mascot": "Seahawks", "display_name": "Seattle Seahawks"},
        {"team_id": "NFL_SF", "league": "NFL", "abbr": "SF", "school_or_city": "San Francisco", "mascot": "49ers", "display_name": "San Francisco 49ers"},
        {"team_id": "NFL_LAR", "league": "NFL", "abbr": "LAR", "school_or_city": "Los Angeles", "mascot": "Rams", "display_name": "Los Angeles Rams"},
        {"team_id": "NFL_NO", "league": "NFL", "abbr": "NO", "school_or_city": "New Orleans", "mascot": "Saints", "display_name": "New Orleans Saints"},
        {"team_id": "NFL_DET", "league": "NFL", "abbr": "DET", "school_or_city": "Detroit", "mascot": "Lions", "display_name": "Detroit Lions"},
    ])


def _games():
    return pd.DataFrame([
        {"game_id": "2026_NFL_W01_NE_SEA", "away_team_id": "NFL_NE", "home_team_id": "NFL_SEA", "week": 1, "status": "SCHEDULED", "kickoff_utc": "2026-09-10T00:20:00Z", "season_type": "REG"},
        {"game_id": "2026_NFL_W01_SF_LAR", "away_team_id": "NFL_SF", "home_team_id": "NFL_LAR", "week": 1, "status": "SCHEDULED", "kickoff_utc": "2026-09-11T00:15:00Z", "season_type": "REG"},
        {"game_id": "2026_NFL_W01_NO_DET", "away_team_id": "NFL_NO", "home_team_id": "NFL_DET", "week": 1, "status": "SCHEDULED", "kickoff_utc": "2026-09-13T17:00:00Z", "season_type": "REG"},
    ])


def test_parse_reads_every_team_row_and_skips_date_headers():
    rows, problems = vsin.parse(FIX.read_text())
    assert not problems and len(rows) == 6
    ne = rows[0]
    assert ne.slug == "new-england-patriots" and ne.spread == 3.0 and ne.spread_handle == 0.22 and ne.spread_bets == 0.35
    assert ne.total == 44.5 and ne.total_handle == 0.41 and ne.total_bets == 0.47 and ne.moneyline == 140.0
    sea = rows[1]
    assert sea.spread == -3.0 and sea.spread_handle == 0.78 and sea.spread_bets == 0.65      # rotation number ignored


def test_parse_aborts_when_the_header_layout_changes():
    bad = FIX.read_text().replace("HandleHND", "TicketsTKT")
    rows, problems = vsin.parse(bad)
    assert rows == [] and "no longer matches" in problems[0]["why"]


def test_seed_aliases_maps_slugs_and_reports_unknowns():
    r = ids.AliasResolver.load()
    rows, _ = vsin.parse(FIX.read_text())
    added, unmatched = vsin.seed_aliases(rows, "NFL", r, _teams())
    assert added == 6 and not unmatched
    assert r.resolve("vsin", alias="seattle-seahawks") == "NFL_SEA"
    added2, unmatched2 = vsin.seed_aliases(rows, "NFL", r, _teams())
    assert added2 == 0                                                     # already known, not re-added
    fake = [type(rows[0])(slug="not-a-real-team", name="X", spread=1, spread_handle=.5, spread_bets=.5, total=44,
                          total_handle=.5, total_bets=.5, moneyline=100, ml_handle=.5, ml_bets=.5)]
    assert vsin.seed_aliases(fake, "NFL", r, _teams())[1] == ["not-a-real-team"]


def test_to_records_uses_home_side_and_over_side_correctly():
    r = ids.AliasResolver.load()
    rows, _ = vsin.parse(FIX.read_text())
    vsin.seed_aliases(rows, "NFL", r, _teams())
    recs, problems = vsin.to_records(rows, "NFL", _games(), r, "2026-09-09T18:00:00+00:00")
    assert len(recs) == 3 and not problems
    sea = [x for x in recs if x["game_id"] == "2026_NFL_W01_NE_SEA"][0]
    # Seattle is home: its spread bets 65% and handle 78% are the home-side numbers
    assert sea["spread_ticket_pct_home"] == 0.65 and sea["spread_money_pct_home"] == 0.78
    assert sea["moneyline_ticket_pct_home"] == 0.71 and sea["moneyline_money_pct_home"] == 0.64
    # the total's over share comes from the AWAY row (NE: 47% bets, 41% handle)
    assert sea["total_ticket_pct_home"] == 0.47 and sea["total_money_pct_home"] == 0.41
    assert sea["line_spread_home"] == -3.0 and sea["line_total"] == 44.5 and sea["source"] == "vsin_dk"
    det = [x for x in recs if x["game_id"] == "2026_NFL_W01_NO_DET"][0]
    assert det["spread_ticket_pct_home"] == 0.74 and det["moneyline_money_pct_home"] == 0.95


def test_unmapped_slug_is_reported_not_guessed():
    r = ids.AliasResolver.load()
    rows, _ = vsin.parse(FIX.read_text())
    vsin.seed_aliases(rows[:2], "NFL", r, _teams())          # only the first game is mapped
    recs, problems = vsin.to_records(rows, "NFL", _games(), r, "2026-09-09T18:00:00+00:00")
    assert len(recs) == 1 and any("unmapped VSiN slug" in p["why"] for p in problems)


def test_unmapped_slug_does_not_desynchronize_later_pairs():
    """The bug this guards: advancing one row on an unmapped team married rows from different games."""
    r = ids.AliasResolver.load()
    rows, _ = vsin.parse(FIX.read_text())
    teams = _teams()[~_teams().team_id.isin(["NFL_SF", "NFL_LAR"])]      # middle game's teams unknown
    vsin.seed_aliases(rows, "NFL", r, teams)
    recs, problems = vsin.to_records(rows, "NFL", _games(), r, "2026-09-09T18:00:00+00:00")
    ids_out = {x["game_id"] for x in recs}
    assert ids_out == {"2026_NFL_W01_NE_SEA", "2026_NFL_W01_NO_DET"}      # first and third still correct
    det = [x for x in recs if x["game_id"] == "2026_NFL_W01_NO_DET"][0]
    assert det["spread_ticket_pct_home"] == 0.74                          # Detroit's own number, not a neighbour's


def test_mispaired_rows_are_rejected_by_structural_checks():
    r = ids.AliasResolver.load()
    rows, _ = vsin.parse(FIX.read_text())
    vsin.seed_aliases(rows, "NFL", r, _teams())
    scrambled = [rows[0], rows[3]]                                        # NE row + LAR row: not a real pair
    games = _games().copy()
    games.loc[len(games)] = {"game_id": "2026_NFL_W01_NE_LAR", "away_team_id": "NFL_NE", "home_team_id": "NFL_LAR",
                             "week": 1, "status": "SCHEDULED", "kickoff_utc": "2026-09-13T17:00:00Z", "season_type": "REG"}
    recs, problems = vsin.to_records(scrambled, "NFL", games, r, "2026-09-09T18:00:00+00:00")
    assert not recs and any("mis-paired" in p["why"] for p in problems)
