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
    added, unmatched, _ = vsin.seed_aliases(rows, "NFL", r, _teams())
    assert added == 6 and not unmatched
    assert r.resolve("vsin", alias="seattle-seahawks") == "NFL_SEA"
    added2, unmatched2, _ = vsin.seed_aliases(rows, "NFL", r, _teams())
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


def test_vsin_abbreviations_expand_to_real_school_names():
    """VSiN writes 'boise-st-broncos' and 'e-michigan-eagles'; these must reach the CFBD school names."""
    for slug, school in (("boise-st-broncos", "Boise State"), ("mississippi-st-bulldogs", "Mississippi State"),
                         ("e-michigan-eagles", "Eastern Michigan"), ("fl-atlantic-owls", "Florida Atlantic"),
                         ("la-monroe-warhawks", "Louisiana Monroe"), ("s-alabama-jaguars", "South Alabama"),
                         ("middle-tenn-st-blue-raiders", "Middle Tennessee State"), ("connecticut-huskies", "Connecticut")):
        assert vsin.slug_key(school) in set(vsin._slug_variants(slug)), slug


def test_row_without_a_moneyline_is_still_read():
    """College favourites often have no moneyline posted; the row must still yield spread and total."""
    html = """<table>
    <tr><th>CFB</th><th></th><th>SpreadSPR</th><th>HandleHND</th><th>BetsBET</th><th>TotalTOT</th><th>HandleHND</th><th>BetsBET</th><th>MoneyML</th><th>HandleHND</th><th>BetsBET</th></tr>
    <tr><td>101</td><td><a href="https://data.vsin.com/college-football/teams/alabama-crimson-tide">Alabama</a></td>
        <td>-24.5</td><td>61%</td><td>58%</td><td>52.5</td><td>44%</td><td>47%</td><td></td><td></td><td></td></tr>
    </table>"""
    rows, problems = vsin.parse(html)
    assert len(rows) == 1 and not problems
    r = rows[0]
    assert r.spread == -24.5 and r.spread_handle == 0.61 and r.total == 52.5 and r.moneyline is None and r.ml_bets is None


def test_unmatched_pair_reports_the_slugs_behind_it():
    """A wrong alias mapping looks like 'no scheduled game'; the slugs must be visible to diagnose it."""
    r = ids.AliasResolver.load()
    rows, _ = vsin.parse(FIX.read_text())
    vsin.seed_aliases(rows, "NFL", r, _teams())
    only_one = _games().head(1)
    recs, problems = vsin.to_records(rows, "NFL", only_one, r, "2026-09-09T18:00:00+00:00")
    unmatched = [p for p in problems if p["kind"] == "no_scheduled_game"]
    assert unmatched and "VSiN slugs:" in unmatched[0]["why"]
    flagged = [p for p in problems if p["kind"] == "mapped_but_never_matched"]
    assert flagged and "->" in flagged[0]["why"]


def test_market_with_zero_on_both_sides_is_treated_as_not_posted():
    """College favourites often have no moneyline; 0% / 0% is 'not posted', not a validation failure."""
    html = FIX.read_text().replace('<td>+140</td><td>36%</td><td>29%</td>', '<td></td><td>0%</td><td>0%</td>') \
                          .replace('<td>-166</td><td>64%</td><td>71%</td>', '<td></td><td>0%</td><td>0%</td>')
    r = ids.AliasResolver.load()
    rows, _ = vsin.parse(html)
    vsin.seed_aliases(rows, "NFL", r, _teams())
    recs, problems = vsin.to_records(rows, "NFL", _games(), r, "2026-09-09T18:00:00+00:00")
    assert not [p for p in problems if p["kind"] == "sum_not_100"]
    sea = [x for x in recs if x["game_id"] == "2026_NFL_W01_NE_SEA"][0]
    assert "moneyline_ticket_pct_home" not in sea and sea["spread_ticket_pct_home"] == 0.65


def test_qualifier_words_are_never_dropped_when_matching():
    """The real failure this guards: florida-atlantic-owls resolved to Florida, miami-oh to Miami."""
    assert "florida" not in vsin._slug_variants("florida-atlantic-owls")
    assert vsin.slug_key("Florida Atlantic") in vsin._slug_variants("florida-atlantic-owls")
    assert "miami" not in vsin._slug_variants("miami-oh-redhawks")
    assert vsin.slug_key("Miami (OH)") in vsin._slug_variants("miami-oh-redhawks")
    # schools whose name really is the short form still resolve
    assert "miami" in vsin._slug_variants("miami-hurricanes")
    assert "florida" in vsin._slug_variants("florida-gators")


def test_stored_alias_written_by_an_older_matcher_is_detected_and_corrected(tmp_path):
    r = ids.AliasResolver.load()
    teams = pd.DataFrame([
        {"team_id": "CFB_FLA", "league": "CFB", "abbr": "FLA", "school_or_city": "Florida", "mascot": "Gators", "display_name": "Florida Gators"},
        {"team_id": "CFB_FAU", "league": "CFB", "abbr": "FAU", "school_or_city": "Florida Atlantic", "mascot": "Owls", "display_name": "Florida Atlantic Owls"},
    ])
    r.add([{"provider": "vsin", "alias": "florida-atlantic-owls", "provider_id": None, "team_id": "CFB_FLA", "season_from": None, "season_to": None}])
    rows = [vsin.TeamRow(slug="florida-atlantic-owls", name="Florida Atlantic", spread=-3.0, spread_handle=.5, spread_bets=.5,
                         total=50.0, total_handle=.5, total_bets=.5, moneyline=-150, ml_handle=.5, ml_bets=.5)]
    added, unmatched, conflicts = vsin.seed_aliases(rows, "CFB", r, teams)
    assert conflicts and conflicts[0]["stored"] == "CFB_FLA" and conflicts[0]["expected"] == "CFB_FAU"
    assert r.resolve("vsin", alias="florida-atlantic-owls") == "CFB_FAU"      # corrected in place


def test_single_published_side_is_recovered_as_its_complement():
    """VSiN sometimes leaves one cell of a pair blank. Over% and Under% are complements, so one value
    determines both -- discarding the metric (the '—' on the card) threw away real information."""
    html = """<table>
    <tr><th>NFL</th><th></th><th>SpreadSPR</th><th>HandleHND</th><th>BetsBET</th><th>TotalTOT</th><th>HandleHND</th><th>BetsBET</th><th>MoneyML</th><th>HandleHND</th><th>BetsBET</th></tr>
    <tr><td></td><td><a href="https://data.vsin.com/nfl/teams/new-england-patriots">New England</a></td>
        <td>+3</td><td>22%</td><td>35%</td><td>44.5</td><td></td><td>74%</td><td>+140</td><td>36%</td><td>29%</td></tr>
    <tr><td>45</td><td><a href="https://data.vsin.com/nfl/teams/seattle-seahawks">Seattle</a></td>
        <td>-3</td><td>78%</td><td>65%</td><td>44.5</td><td>41%</td><td></td><td>-166</td><td>64%</td><td>71%</td></tr>
    </table>"""
    r = ids.AliasResolver.load()
    rows, _ = vsin.parse(html)
    vsin.seed_aliases(rows, "NFL", r, _teams())
    recs, problems = vsin.to_records(rows, "NFL", _games(), r, "2026-09-09T18:00:00+00:00")
    rec = recs[0]
    assert rec["total_ticket_pct_home"] == 0.74          # over bets published on the away row
    assert rec["total_money_pct_home"] == 0.59           # under handle 41% published -> over handle is 59%
    assert any(p["kind"] == "single_sided_pct" for p in problems)    # recovery is recorded, not silent


def test_vsin_lines_become_market_snapshots(tmp_path, monkeypatch):
    """With VSiN as the single market source, its line must land in market_snapshots so the market
    engine, predictions and picks keep working unchanged."""
    import config
    from pipeline.jobs import ingest_splits as isp
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    games = pd.DataFrame([{"game_id": "2026_NFL_W01_NE_SEA", "week": 1}])
    recs = [{"game_id": "2026_NFL_W01_NE_SEA", "retrieved_at": "2026-09-09T18:00:00+00:00", "source": "vsin_dk",
             "line_spread_home": -3.0, "line_total": 44.5, "line_ml_home": -166, "line_ml_away": 140,
             "spread_ticket_pct_home": 0.65, "spread_money_pct_home": 0.78,
             "total_ticket_pct_home": 0.47, "total_money_pct_home": 0.41,
             "moneyline_ticket_pct_home": 0.71, "moneyline_money_pct_home": 0.64}]
    n = isp.write_market_snapshots(recs, "NFL", 2026, games)
    assert n == 1
    out = pd.read_csv(tmp_path / "tables" / "market" / "snapshots" / "NFL" / "2026" / "W01.csv")
    r = out.iloc[0]
    assert r.book == "draftkings" and r.source == "vsin_dk"
    assert r.spread_home == -3.0 and r.total == 44.5 and r.ml_home == -166
    assert r.spread_ticket_pct_home == 0.65 and r.total_ticket_pct_over == 0.47
    assert bool(r.is_first_snapshot) is True
    assert isp.write_market_snapshots(recs, "NFL", 2026, games) == 0      # append-only, no duplicates
