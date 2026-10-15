"""
Writing linetracker prices as market snapshots.

The whole risk here is a price filed against the WRONG game. The linetracker shows plain display
names with no team link, and the existing slug matcher already proved how that goes wrong: it stored
texas-san-antonio-roadrunners as CFB_TEX by trimming tokens until something matched. So every test
below is about refusing rather than guessing.
"""
from __future__ import annotations

import pandas as pd
import pytest

import config
from pipeline import linetracker as lt
from providers import vsin


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(lt, "NAMES_CSV", tmp_path / "data" / "manual" / "vsin_names.csv")
    monkeypatch.setattr(lt, "UNMAPPED_CSV", str(tmp_path / "data" / "manual" / "unmapped_{league}.csv"))


def _teams():
    return pd.DataFrame([
        {"team_id": "CFB_UTSA", "league": "CFB", "abbr": "UTSA", "school_or_city": "UTSA",
         "mascot": "Roadrunners", "display_name": "UTSA Roadrunners"},
        {"team_id": "CFB_TEX", "league": "CFB", "abbr": "TEX", "school_or_city": "Texas",
         "mascot": "Longhorns", "display_name": "Texas Longhorns"},
        {"team_id": "CFB_NAVY", "league": "CFB", "abbr": "NAVY", "school_or_city": "Navy",
         "mascot": "Midshipmen", "display_name": "Navy Midshipmen"},
        {"team_id": "CFB_SJSU", "league": "CFB", "abbr": "SJSU", "school_or_city": "San José State",
         "mascot": "Spartans", "display_name": "San José State Spartans"},
        {"team_id": "CFB_UTEP", "league": "CFB", "abbr": "UTEP", "school_or_city": "UTEP",
         "mascot": "Miners", "display_name": "UTEP Miners"},
    ])


def _sched():
    return pd.DataFrame([
        {"game_id": "2026_CFB_W08_NAVY_UTSA", "away_team_id": "CFB_NAVY", "home_team_id": "CFB_UTSA",
         "week": 8, "status": "SCHEDULED", "kickoff_utc": "2026-10-17T19:00:00+00:00"},
        {"game_id": "2026_CFB_W08_SJSU_UTEP", "away_team_id": "CFB_SJSU", "home_team_id": "CFB_UTEP",
         "week": 8, "status": "SCHEDULED", "kickoff_utc": "2026-10-18T01:00:00+00:00"},
    ])


def _game(date, away, home, spread, total, books=("circa", "betmgm")):
    return {"date": date, "n_teams": 2, "open_label": "▶ 3:00 PM ET ET OPEN",
            "open": {b: {"spread": spread - 0.5, "total": total - 1} for b in books},
            "teams": [
                (away, {b: {"spread": -spread, "spread_price": -110, "ml_price": 250,
                            "total": total, "over_price": -110} for b in books}),
                (home, {b: {"spread": spread, "spread_price": -110, "ml_price": -300,
                            "total": total, "under_price": -110} for b in books})]}


# ---- the bug that was live in the repo --------------------------------------------------------
def test_the_token_trim_no_longer_turns_utsa_into_texas():
    """
    data/tables/ref/team_aliases.csv held `vsin,texas-san-antonio-roadrunners,,CFB_TEX` -- UTSA's
    slug mapped to Texas, because the trim ran down to a single token. A three-or-more token name
    must never reduce to its first token.
    """
    assert "texas" not in vsin._slug_variants("texas-san-antonio-roadrunners")
    # two-token names still shorten, which is where the trim is actually wanted
    assert "navy" in vsin._slug_variants("navy-midshipmen")
    assert "floridaatlantic" in vsin._slug_variants("florida-atlantic-owls")
    assert "florida" not in vsin._slug_variants("florida-atlantic-owls")


def test_accents_are_folded_not_deleted():
    """'San Jose State' and 'San José State' have to land on the same key, or SJSU never matches."""
    assert vsin.slug_key("San José State") == vsin.slug_key("San Jose State")
    assert lt.name_candidates("San Jose ST Spartans", "CFB", _teams()) == {"CFB_SJSU"}


def test_an_unbridgeable_name_is_refused_not_guessed():
    """No spelling of 'Texas-San Antonio' reaches 'UTSA', so it must come back empty."""
    assert lt.name_candidates("Texas-San Antonio Roadrunners", "CFB", _teams()) == set()


def test_a_recorded_name_resolves(tmp_path):
    lt.NAMES_CSV.parent.mkdir(parents=True, exist_ok=True)
    lt.NAMES_CSV.write_text("league,label,team_id,note\nCFB,Texas-San Antonio Roadrunners,CFB_UTSA,x\n")
    assert lt.name_candidates("Texas-San Antonio Roadrunners", "CFB", _teams()) == {"CFB_UTSA"}


# ---- resolving by the pair --------------------------------------------------------------------
def test_a_mis_read_name_cannot_reach_a_real_game():
    """
    Even if a label resolved to the wrong team, the PAIR must still exist in the schedule. There is
    no Texas-vs-Navy game on Oct 17, so the price is refused rather than filed against Texas.
    """
    g = _game("Sat, Oct 17", "Navy Midshipmen", "Texas Longhorns", -7.0, 45.5)
    res, probs = lt.resolve([g], "CFB", 2026, _sched(), _teams())
    assert res == []
    assert any(p["kind"] == "no_scheduled_game" for p in probs)


def test_a_resolvable_pair_lands_on_the_right_game_and_the_right_side():
    lt.NAMES_CSV.parent.mkdir(parents=True, exist_ok=True)
    lt.NAMES_CSV.write_text("league,label,team_id,note\nCFB,Texas-San Antonio Roadrunners,CFB_UTSA,x\n")
    g = _game("Sat, Oct 17", "Navy Midshipmen", "Texas-San Antonio Roadrunners", -7.0, 45.5)
    res, probs = lt.resolve([g], "CFB", 2026, _sched(), _teams())
    assert len(res) == 1, probs
    r = res[0]
    assert r["game_id"] == "2026_CFB_W08_NAVY_UTSA"
    assert r["home_label"] == "Texas-San Antonio Roadrunners"
    assert r["away_label"] == "Navy Midshipmen"


def test_the_eastern_date_is_used_not_the_utc_date():
    """
    A 9pm ET Saturday kickoff is 01:00 UTC Sunday. Comparing UTC dates would miss the game that the
    page files under Saturday.
    """
    g = _game("Sat, Oct 17", "San Jose ST Spartans", "UTEP Miners", -3.0, 52.0)
    res, _ = lt.resolve([g], "CFB", 2026, _sched(), _teams())
    assert len(res) == 1 and res[0]["game_id"] == "2026_CFB_W08_SJSU_UTEP"


def test_an_unpaired_group_is_never_written():
    res, probs = lt.resolve([{"date": "Sat, Oct 17", "n_teams": 1, "teams": [("Navy Midshipmen", {})],
                              "open": {}, "open_label": "OPEN"}], "CFB", 2026, _sched(), _teams())
    assert res == [] and any(p["kind"] == "unpaired" for p in probs)


def test_unmapped_names_are_written_out_for_filling_in():
    g = _game("Sat, Oct 17", "Navy Midshipmen", "Texas-San Antonio Roadrunners", -7.0, 45.5)
    res, probs = lt.resolve([g], "CFB", 2026, _sched(), _teams())
    assert res == []
    note = next(p for p in probs if p["kind"] == "unmapped_names_written")
    assert "Texas-San Antonio Roadrunners" in note["why"]
    out = __import__("pathlib").Path(str(lt.UNMAPPED_CSV).format(league="CFB"))
    assert out.exists() and "Texas-San Antonio Roadrunners" in out.read_text()


# ---- snapshot rows ----------------------------------------------------------------------------
def _resolved_one():
    lt.NAMES_CSV.parent.mkdir(parents=True, exist_ok=True)
    lt.NAMES_CSV.write_text("league,label,team_id,note\nCFB,Texas-San Antonio Roadrunners,CFB_UTSA,x\n")
    g = _game("Sat, Oct 17", "Navy Midshipmen", "Texas-San Antonio Roadrunners", -7.0, 45.5)
    res, _ = lt.resolve([g], "CFB", 2026, _sched(), _teams())
    return res


def test_one_row_per_book_with_the_home_spread_convention():
    snaps = lt.snapshots(_resolved_one(), pd.Timestamp("2026-10-16T18:30:00Z"))
    assert {s["book"] for s in snaps} == {"circa", "betmgm"}
    circa = next(s for s in snaps if s["book"] == "circa")
    assert circa["game_id"] == "2026_CFB_W08_NAVY_UTSA"
    assert circa["spread_home"] == -7.0            # negative = home favoured, as everywhere else
    assert circa["ml_home"] == -300 and circa["ml_away"] == 250
    assert circa["total"] == 45.5
    assert circa["over_price"] == -110 and circa["under_price"] == -110
    assert circa["source"] == "vsin_linetracker"
    assert circa["snapshot_id"] == "2026_CFB_W08_NAVY_UTSA_circa_20261016T183000Z"


def test_the_opening_number_travels_on_the_same_row():
    """Opening and current on one row is what closing-line value needs; separate rows lose the pair."""
    circa = next(s for s in lt.snapshots(_resolved_one()) if s["book"] == "circa")
    assert circa["provider_open_spread_home"] == -7.5
    assert circa["provider_open_total"] == 44.5


def test_a_book_quoting_nothing_produces_no_row():
    res = _resolved_one()
    res[0]["home_books"]["westgate"] = {}
    res[0]["away_books"]["westgate"] = {}
    assert "westgate" not in {s["book"] for s in lt.snapshots(res)}


def test_movement_rows_carry_the_books_own_arrows():
    """
    The arrows are the books' own flag that a number just moved -- better than diffing our snapshots,
    which cannot see a move that happened between two pulls.
    """
    res = _resolved_one()
    res[0]["home_books"]["circa"]["spread_moved"] = "down"
    res[0]["away_books"]["betmgm"]["total_moved"] = "up"
    moves = lt.movement(res)
    assert {"circa", "betmgm"} == {m["book"] for m in moves}
    sp = next(m for m in moves if m["market"] == "SPREAD")
    assert sp["side"] == "home" and sp["direction"] == "down" and sp["value"] == -7.0


def test_write_appends_per_week_and_is_idempotent():
    snaps = lt.snapshots(_resolved_one(), pd.Timestamp("2026-10-16T18:30:00Z"))
    n1 = lt.write(snaps, "CFB", 2026, _sched())
    n2 = lt.write(snaps, "CFB", 2026, _sched())
    assert n1 == 2 and n2 == 0, "re-writing the same snapshot must not duplicate it"
    path = config.TABLES / "market" / "snapshots" / "CFB" / "2026" / "W08.csv"
    df = pd.read_csv(path)
    assert len(df) == 2 and set(df.book) == {"circa", "betmgm"}
    assert list(df.columns)[:5] == ["snapshot_id", "game_id", "retrieved_at",
                                    "provider_updated_at", "book"]


# ---- collision is the right test for a wrong alias --------------------------------------------
class _Resolver:
    """Minimal stand-in: collisions() only reads .aliases."""
    def __init__(self, rows):
        self.aliases = pd.DataFrame(rows)
        self.saved = False

    def save(self):
        self.saved = True


def _cfb_teams():
    return pd.DataFrame([
        {"team_id": "CFB_TEX", "league": "CFB", "abbr": "TEX", "school_or_city": "Texas",
         "mascot": "Longhorns", "display_name": "Texas Longhorns"},
        {"team_id": "CFB_TAM", "league": "CFB", "abbr": "TAM", "school_or_city": "Texas A&M",
         "mascot": "Aggies", "display_name": "Texas A&M Aggies"},
        {"team_id": "CFB_UTSA", "league": "CFB", "abbr": "UTSA", "school_or_city": "UTSA",
         "mascot": "Roadrunners", "display_name": "UTSA Roadrunners"},
        {"team_id": "CFB_TENN", "league": "CFB", "abbr": "TENN", "school_or_city": "Tennessee",
         "mascot": "Volunteers", "display_name": "Tennessee Volunteers"},
        {"team_id": "CFB_APP", "league": "CFB", "abbr": "APP", "school_or_city": "App State",
         "mascot": "Mountaineers", "display_name": "App State Mountaineers"},
    ])


def _alias(alias, team_id):
    return {"provider": "vsin", "alias": alias, "provider_id": None, "team_id": team_id,
            "season_from": None, "season_to": None}


def test_several_slugs_claiming_one_team_is_the_wrong_mapping():
    """
    Measured on the live table: texas-a-and-m-aggies AND texas-san-antonio-roadrunners were both
    filed as CFB_TEX. A team has exactly one VSiN slug, so a second one is another school that the
    token trim collapsed onto it -- provable with no spelling rule involved.
    """
    r = _Resolver([_alias("texas-longhorns", "CFB_TEX"),
                   _alias("texas-a-and-m-aggies", "CFB_TEX"),
                   _alias("texas-san-antonio-roadrunners", "CFB_TEX")])
    coll = vsin.collisions(r, "CFB", _cfb_teams())
    assert len(coll) == 1
    assert coll[0]["keep"] == "texas-longhorns"
    assert set(coll[0]["drop"]) == {"texas-a-and-m-aggies", "texas-san-antonio-roadrunners"}


def test_a_lone_underivable_alias_is_left_alone():
    """
    THE MISTAKE THIS GUARDS AGAINST. An earlier detector condemned every alias no spelling could
    derive, and told the user to delete appalachian-st-mountaineers, army-black-nights (VSiN's own
    typo for Black Knights) and miami-fl-hurricanes -- three CORRECT mappings, each the only thing
    making its team resolve at all. An alias exists to bridge what spelling cannot reach, so being
    underivable is the normal state of a useful one. Only a collision proves an alias wrong.
    """
    r = _Resolver([_alias("appalachian-st-mountaineers", "CFB_APP"),
                   _alias("texas-longhorns", "CFB_TEX")])
    assert vsin.collisions(r, "CFB", _cfb_teams()) == []


def test_depth_separates_a_real_match_from_a_trimmed_one():
    """
    tennessee-volunteers reaches Tennessee at depth 0 on its full name; tennessee-martin only gets
    there at depth 1, after its own identity is cut away. Both "match", so depth decides.
    """
    r = _Resolver([_alias("tennessee-volunteers", "CFB_TENN"),
                   _alias("tennessee-martin", "CFB_TENN")])
    coll = vsin.collisions(r, "CFB", _cfb_teams())
    assert coll[0]["keep"] == "tennessee-volunteers"
    assert coll[0]["drop"] == ["tennessee-martin"]


def test_a_collision_with_no_clear_winner_is_reported_not_guessed():
    r = _Resolver([_alias("mystery-one", "CFB_TEX"), _alias("mystery-two", "CFB_TEX")])
    coll = vsin.collisions(r, "CFB", _cfb_teams())
    assert coll[0]["keep"] is None and coll[0]["drop"] == []
    assert "for a human to pick" in coll[0]["why"]


def test_the_ampersand_is_read_as_a_word():
    """VSiN writes texas-a-and-m-aggies; our table writes Texas A&M Aggies. Dropping & broke both."""
    assert vsin.slug_key("Texas A&M Aggies") == vsin.slug_key("texas a and m aggies")
    assert lt.name_candidates("Texas A&M Aggies", "CFB", _cfb_teams()) == {"CFB_TAM"}
