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


def test_espn_fpi_parses_either_response_shape():
    """ESPN's endpoints are unofficial, so the reader must survive both shapes and name the SOS field
    however ESPN spells it."""
    from providers import espn_fpi
    r = ids.AliasResolver.load()
    r.add([{"provider": "espn", "alias": "Alabama Crimson Tide", "provider_id": None, "team_id": "CFB_ALA", "season_from": None, "season_to": None},
           {"provider": "espn", "alias": "Georgia Bulldogs", "provider_id": None, "team_id": "CFB_UGA", "season_from": None, "season_to": None}])
    ts = pd.Timestamp("2026-09-12T12:00:00Z")
    fitt = {"teams": [
        {"team": {"displayName": "Alabama Crimson Tide"},
         "categories": [{"name": "resume", "values": [], "ranks": []}],
         "stats": [{"name": "avgsosrank", "rank": 7, "value": 7}, {"name": "fpi", "value": 21.4}]},
        {"team": {"displayName": "Georgia Bulldogs"}, "stats": [{"name": "strengthOfSchedule", "rank": 3}]}]}
    df, notes = espn_fpi.normalize(fitt, 2026, r, ts, set())
    assert len(df) == 2
    assert int(df[df.team_id == "CFB_ALA"].sos_rank_espn.iloc[0]) == 7
    assert int(df[df.team_id == "CFB_UGA"].sos_rank_espn.iloc[0]) == 3      # different spelling, same field
    core = {"items": [{"team": {"name": "Alabama Crimson Tide"}, "sosRank": 11}]}
    df2, _ = espn_fpi.normalize(core, 2026, r, ts, set())
    assert int(df2.sos_rank_espn.iloc[0]) == 11
    unmatched = set()
    espn_fpi.normalize({"teams": [{"team": {"displayName": "Nowhere State"}, "stats": [{"name": "sos", "rank": 1}]}]}, 2026, r, ts, unmatched)
    assert unmatched == {"Nowhere State"}                                    # unknown teams reported, never guessed
    df3, notes3 = espn_fpi.normalize({"nothing": 1}, 2026, r, ts, set())
    assert df3.empty and "no team entries found" in notes3[0]


def test_espn_parallel_array_categories():
    """ESPN stores power-index numbers as parallel names/values/ranks arrays inside each category,
    with no inline labels. This is the shape the live endpoint actually returns."""
    from providers import espn_fpi
    r = ids.AliasResolver.load()
    r.add([{"provider": "espn", "alias": "Texas State Bobcats", "provider_id": None, "team_id": "CFB_TXST", "season_from": None, "season_to": None}])
    payload = {"items": [{"team": {"displayName": "Texas State Bobcats", "logos": [{"height": 500, "width": 500}]},
        "categories": [
            {"name": "general", "names": ["fpi"], "values": [3.2], "ranks": [55]},
            {"name": "resume", "names": ["strengthofrecord", "fpi", "avgwinprobability", "strengthofschedule",
                                          "remainingstrengthofschedule", "gamecontrol"],
             "values": [0.41, 3.2, 0.55, 0.62, 0.58, 0.5], "ranks": [70, 55, 61, 96, 88, 64]}]}]}
    df, notes = espn_fpi.normalize(payload, 2026, r, pd.Timestamp("2026-09-12T03:00:00Z"), set())
    assert len(df) == 1          # labelled fields resolve by name, independent of the positional check
    row = df.iloc[0]
    assert int(row.sos_rank_espn) == 96                 # the RANK, not the 0.62 value
    assert int(row.remaining_sos_rank_espn) == 88 and int(row.strength_of_record_rank) == 70
    assert abs(row.fpi - 3.2) < 1e-9                    # fpi takes the value, not its rank
    # logo width/height must never be mistaken for a stat
    assert row.sos_rank_espn != 500


def test_verified_indices_are_used_without_needing_a_pin():
    """The column is now verified against ESPN's published ranks, so SOS is read directly. The old
    pinning machinery remains only as a fallback for a shape the reference cannot identify."""
    from providers import espn_fpi
    r = ids.AliasResolver.load()
    r.add([{"provider": "espn", "alias": "Texas State Bobcats", "provider_id": None, "team_id": "CFB_TXST",
            "season_from": None, "season_to": None}])
    payload = {"items": [{"team": {"displayName": "Texas State Bobcats"}, "categories": [
        {"name": "resume", "ranks": ["-"] * 6, "values": [103.0, 97.0, 1.0, 93.0, 68.0, 132.0],
         "totals": ["103rd", "97th", "1st", "93rd", "68th", "132nd"]}]}]}
    df, _ = espn_fpi.normalize(payload, 2026, r, pd.Timestamp("2026-09-17T12:00:00Z"), set(), "CFB")
    row = df.iloc[0]
    assert int(row.sos_rank_espn) == 1        # index 2, verified at 20/20 against the published table
    assert int(row.power_rank_espn) == 103    # index 0 is the FPI rank, not strength of record


def _espn_payload(rows):
    return {"items": [{"team": {"displayName": n}, "categories": [
        {"name": "resume", "ranks": ["-"] * 6, "values": [float(x) for x in v]}]} for n, v in rows]}


def _espn_resolver(rows):
    r = ids.AliasResolver.load()
    r.add([{"provider": "espn", "alias": n, "provider_id": None, "team_id": "CFB_T%d" % i,
            "season_from": None, "season_to": None} for i, (n, _) in enumerate(rows)])
    return r


def test_positional_check_alone_cannot_distinguish_a_rank_from_a_row_index():
    """The mistake this guards against: a column counting 1,2,3 down a list sorted by that column looks
    identical to a row index, so agreeing with the sort order proves nothing. Only a cross-sort — or a
    comparison against published values — can tell them apart."""
    from providers import espn_fpi
    teams = [f"Team {i}" for i in range(1, 41)]
    sos = {t: i + 1 for i, t in enumerate(teams)}
    mk = lambda order, val: {"items": [{"team": {"displayName": t}, "categories": [
        {"name": "resume", "ranks": ["-"] * 6, "values": [0.0, 0.0, float(val(t, i)), 0.0, 0.0, 0.0]}]}
        for i, t in enumerate(order, start=1)]}
    asc = mk(teams, lambda t, i: sos[t])
    # sorted ascending, a real rank and a row index are indistinguishable
    assert espn_fpi.verify_sos_column(asc)[0] in (True, False)
    # reversing the sort separates them
    assert espn_fpi.verify_across_sorts(asc, mk(teams[::-1], lambda t, i: sos[t]), 2)[0] is True
    assert espn_fpi.verify_across_sorts(asc, mk(teams[::-1], lambda t, i: i), 2)[0] is False


def test_labelled_response_still_resolves_without_any_pinning():
    """When ESPN returns named fields there is no ambiguity, and the value is used regardless."""
    from providers import espn_fpi
    r = ids.AliasResolver.load()
    r.add([{"provider": "espn", "alias": "Alabama Crimson Tide", "provider_id": None, "team_id": "CFB_ALA",
            "season_from": None, "season_to": None}])
    payload = {"teams": [{"team": {"displayName": "Alabama Crimson Tide"},
                          "stats": [{"name": "avgsosrank", "rank": 7}]}]}
    df, _ = espn_fpi.normalize(payload, 2026, r, pd.Timestamp("2026-09-12T15:00:00Z"), set())
    assert int(df.sos_rank_espn.iloc[0]) == 7


def _payload_from(order):
    return {"items": [{"team": {"displayName": n}, "categories": [
        {"name": "resume", "ranks": ["-"] * 6, "values": [float(x) for x in v]}]} for n, v in order]}


def test_cross_sort_check_separates_a_real_rank_from_a_row_index():
    """The check that can actually fail. A statistic travels with its team when the sort changes;
    a row index follows the position instead."""
    from providers import espn_fpi
    teams = [f"Team {i}" for i in range(1, 41)]
    sos = {t: i + 1 for i, t in enumerate(teams)}                     # true SOS rank
    # pull A: sorted by SOS, so position == rank for both readings
    A = _payload_from([(t, [0, 0, sos[t], 0, 0, 0]) for t in teams])
    # pull B: a different order. A real statistic keeps each team's own value.
    shuffled = teams[::-1]
    B_real = _payload_from([(t, [0, 0, sos[t], 0, 0, 0]) for t in shuffled])
    ok, why = espn_fpi.verify_across_sorts(A, B_real, 2)
    assert ok and "stayed with the team" in why
    # ...whereas a row index renumbers 1..N down the new order
    B_index = _payload_from([(t, [0, 0, pos, 0, 0, 0]) for pos, t in enumerate(shuffled, start=1)])
    ok2, why2 = espn_fpi.verify_across_sorts(A, B_index, 2)
    assert not ok2 and "it is an index, not a rank" in why2
    # and an ignored sort parameter is caught rather than treated as agreement
    ok3, why3 = espn_fpi.verify_across_sorts(A, A, 2)
    assert not ok3 and "same order" in why3


def test_reversed_sort_is_enough_to_discriminate():
    """The second pull only needs a different ORDER, not a different column — which matters because
    ESPN rejects most sort keys with a 400. Reversing the one key it accepts works."""
    from providers import espn_fpi
    teams = [f"Team {i}" for i in range(1, 41)]
    sos = {t: i + 1 for i, t in enumerate(teams)}
    mk = lambda order, val: {"items": [{"team": {"displayName": t}, "categories": [
        {"name": "resume", "ranks": ["-"] * 6, "values": [0.0, 0.0, float(val(t, i)), 0.0, 0.0, 0.0]}]}
        for i, t in enumerate(order, start=1)]}
    asc = mk(teams, lambda t, i: sos[t])
    assert espn_fpi.verify_across_sorts(asc, mk(teams[::-1], lambda t, i: sos[t]), 2)[0] is True
    assert espn_fpi.verify_across_sorts(asc, mk(teams[::-1], lambda t, i: i), 2)[0] is False


def test_espn_team_aliases_are_seeded_so_sos_is_not_silently_dropped():
    """Without team aliases every FPI row fails to resolve and the SOS column comes back empty while
    the job still reports success — which is exactly what happened."""
    from providers import espn_fpi
    teams = pd.DataFrame([
        {"team_id": "CFB_UCLA", "league": "CFB", "school_or_city": "UCLA", "mascot": "Bruins", "display_name": "UCLA Bruins", "abbr": "UCLA"},
        {"team_id": "CFB_WIS", "league": "CFB", "school_or_city": "Wisconsin", "mascot": "Badgers", "display_name": "Wisconsin Badgers", "abbr": "WIS"},
    ])
    r = ids.AliasResolver.load()
    payload = {"items": [{"team": {"displayName": n}, "categories": []}
                         for n in ("UCLA Bruins", "Wisconsin Badgers", "Nowhere State Somethings")]}
    added, unmatched = espn_fpi.seed_aliases(payload, "CFB", r, teams)
    assert added == 2 and unmatched == ["Nowhere State Somethings"]
    assert r.resolve("espn", alias="UCLA Bruins") == "CFB_UCLA"
    assert espn_fpi.seed_aliases(payload, "CFB", r, teams)[0] == 0      # idempotent


def test_espn_ranks_are_identified_by_ordinal_suffix_not_by_position():
    """ESPN returns each category as alternating values and ranks with no labels. `totals` marks a rank
    with an ordinal suffix, which identifies it structurally instead of guessing at array positions."""
    from providers import espn_fpi
    entry = {"categories": [
        {"name": "fpi", "values": [-7.9, 103.0], "totals": ["-7.9", "103rd"]},
        {"name": "efficiencies", "values": [30.7, 120.0, 44.7, 80.0, 42.0, 117.0, 10.2, 131.0],
         "totals": ["30.7", "120th", "44.7", "80th", "42.0", "117th", "10.2", "131st"]}]}
    assert espn_fpi.fpi_rank(entry) == 103
    eff = espn_fpi.efficiency_ranks(entry)
    assert eff["offense_rank"] == 80 and eff["defense_rank"] == 117 and eff["special_teams_rank"] == 131
    assert eff["overall_rank"] == 120
    assert eff["offense_value"] == 44.7          # the value is kept apart from its rank
    # a category with no ordinals yields values only, never a value mistaken for a rank
    plain = {"categories": [{"name": "fpi", "values": [1.5, 2.5], "totals": ["1.5", "2.5"]}]}
    assert espn_fpi.fpi_rank(plain) is None


def test_nfl_and_cfb_use_different_endpoints_and_sort_keys():
    from providers import espn_fpi
    assert espn_fpi.ENDPOINTS["NFL"]["sos_sort"] == "fpi.avgsosrank"
    assert espn_fpi.ENDPOINTS["CFB"]["sos_sort"] == "resume.avgsosrank"
    assert "/nfl/" in espn_fpi.ENDPOINTS["NFL"]["fitt"] and "/college-football/" in espn_fpi.ENDPOINTS["CFB"]["fitt"]


def test_espn_ranks_are_stored_per_league(tmp_path, monkeypatch):
    """One file per season meant NFL and CFB overwrote each other, so whichever ran second won and the
    other league fell back to our own ranks with no sign anything was wrong."""
    import config
    from pipeline import storage
    from pipeline.jobs import build_site as bs
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    for lg, tid, sos in (("CFB", "CFB_TXST", 1), ("NFL", "NFL_BUF", 7)):
        storage.write_parquet(config.TABLES / "context" / "espn_fpi" / lg / "2026.parquet",
                              pd.DataFrame([{"team_id": tid, "season": 2026, "espn_team": tid,
                                             "sos_rank_espn": sos, "power_rank_espn": 3,
                                             "offense_rank_espn": 4, "defense_rank_espn": 5,
                                             "special_teams_rank_espn": 6}]))

    class S:
        pass
    for lg, tid, sos in (("NFL", "NFL_BUF", 7), ("CFB", "CFB_TXST", 1)):
        s = S(); s.league = lg; s.season = 2026
        t = bs._espn_ranks(s)
        assert len(t) == 1 and t.team_id.iloc[0] == tid and int(t.sos_rank_espn.iloc[0]) == sos
        assert bs._espn_sos_table(s) == {tid: sos}


def test_column_identification_recovers_indices_from_published_ranks():
    """Every previous attempt at this guessed at array positions and got it wrong. Comparing a pull
    against ranks transcribed from ESPN's own pages identifies each column instead."""
    from providers import espn_fpi
    from providers.espn_fpi_reference import CFB_RESUME, CFB_EFFICIENCY
    items = []
    for name in list(CFB_RESUME)[:20]:
        r, e = CFB_RESUME[name], CFB_EFFICIENCY.get(name)
        resume_vals = [r["sor"], r["fpi"], r["sos"], r["rem_sos"], r["gc"], r["avgwp"]]
        cats = [{"name": "resume", "values": [float(x) for x in resume_vals],
                 "totals": [f"{x}th" for x in resume_vals]}]
        if e:
            ev = []
            for k in ("overall", "offense", "defense", "special_teams"):
                ev += [50.0, float(e[k])]
            cats.append({"name": "efficiencies", "values": ev,
                         "totals": [("50.0" if i % 2 == 0 else f"{int(v)}th") for i, v in enumerate(ev)]})
        items.append({"team": {"displayName": name}, "categories": cats})
    ident = espn_fpi.identify_columns({"items": items}, "CFB")
    res = ident["categories"]["resume"]["columns"]
    assert res["sos"]["index"] == 2 and res["sos"]["confident"]
    assert res["fpi"]["index"] == 1 and res["sor"]["index"] == 0
    assert not res["ap"]["confident"]          # AP is on the page but not in the array; flagged, not asserted
    eff = ident["categories"]["efficiencies"]["columns"]
    assert [eff[k]["index"] for k in ("overall", "offense", "defense", "special_teams")] == [1, 3, 5, 7]
    assert all(eff[k]["confident"] for k in ("overall", "offense", "defense", "special_teams"))


def _espn_live_payload():
    """A payload shaped like the live pull: 7-value resume, 8-value efficiencies, AP absent when unranked."""
    from providers.espn_fpi_reference import CFB_RESUME, CFB_EFFICIENCY
    items = []
    for name in CFB_RESUME:
        rr, ee = CFB_RESUME[name], CFB_EFFICIENCY.get(name)
        res = [rr["fpi"], rr["sor"], rr["sos"], rr["rem_sos"], rr["gc"], rr["avgwp"]]
        tot = [f"{v}th" for v in res]
        if rr["ap"] is not None:
            res.append(rr["ap"]); tot.append(f"{rr['ap']}th")
        cats = [{"name": "resume", "values": [float(x) for x in res], "totals": tot}]
        if ee:
            ev, et = [], []
            for k in ("overall", "offense", "defense", "special_teams"):
                ev += [50.0, float(ee[k])]; et += ["50.0", f"{ee[k]}th"]
            cats.append({"name": "efficiencies", "values": ev, "totals": et})
        items.append({"team": {"displayName": name}, "categories": cats})
    return {"items": items}


def test_every_espn_rank_matches_the_published_value():
    """The verified end state: SOS, Power, Offense, Defense and Special Teams all read correctly."""
    from providers import espn_fpi
    from providers.espn_fpi_reference import CFB_RESUME, CFB_EFFICIENCY
    payload = _espn_live_payload()
    teams = pd.DataFrame([{"team_id": f"CFB_T{i}", "league": "CFB", "school_or_city": n.rsplit(" ", 1)[0],
                           "mascot": n.rsplit(" ", 1)[-1], "display_name": n, "abbr": f"T{i}"}
                          for i, n in enumerate(CFB_RESUME)])
    r = ids.AliasResolver.load()
    espn_fpi.seed_aliases(payload, "CFB", r, teams)
    df, notes = espn_fpi.normalize(payload, 2026, r, pd.Timestamp("2026-09-17T12:00:00Z"), set(), "CFB")
    assert any("confirmed against published ranks" in n for n in notes)
    chk = df.set_index("espn_team")
    for name in ("Ohio State Buckeyes", "Notre Dame Fighting Irish", "LSU Tigers", "Texas A&M Aggies"):
        want_r, want_e = CFB_RESUME[name], CFB_EFFICIENCY[name]
        row = chk.loc[name]
        assert int(row.sos_rank_espn) == want_r["sos"]
        assert int(row.power_rank_espn) == want_r["fpi"]
        assert int(row.offense_rank_espn) == want_e["offense"]
        assert int(row.defense_rank_espn) == want_e["defense"]
        assert int(row.special_teams_rank_espn) == want_e["special_teams"]


def test_a_reordered_response_is_remapped_not_misread():
    """If ESPN moves a column, the pull re-identifies it from the published ranks instead of shifting
    every number by one."""
    from providers import espn_fpi
    from providers.espn_fpi_reference import CFB_RESUME
    payload = _espn_live_payload()
    for it in payload["items"]:                       # swap FPI and SOR
        for c in it["categories"]:
            if c["name"] == "resume":
                c["values"][0], c["values"][1] = c["values"][1], c["values"][0]
    resume, eff, note = espn_fpi.resolved_indices(payload, "CFB")
    assert resume["fpi"] == 1 and resume["sor"] == 0 and "re-mapped" in note
    assert resume["sos"] == 2                         # untouched columns stay put
