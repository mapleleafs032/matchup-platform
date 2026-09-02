"""
Phase 4 tests. `test_nflverse_live_*` hit GitHub (no key). CFBD / Odds API tests run on synthetic fixtures
in tests/fixtures (NOT real data) and only exercise normalization + validation logic.
Run: pytest -q
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import config
from pipeline import ids, storage, validate
from pipeline.log import ValidationLog
from providers import cfbd, nflverse, odds_api

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 2, 17, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Redirect all data paths to a temp dir so tests never touch the repo's data/."""
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(config, "RAW", tmp_path / "raw")
    monkeypatch.setattr(ids, "ALIASES_PATH", tmp_path / "tables" / "ref" / "team_aliases.csv")
    import providers.base as base
    monkeypatch.setattr(base.RequestManager, "BUDGET_PATH", tmp_path / "tables" / "ops" / "api_budget.csv")
    monkeypatch.setattr(base.RequestManager, "RAW_INDEX_PATH", tmp_path / "tables" / "ops" / "raw_responses.csv")
    yield tmp_path


# ---- ids -------------------------------------------------------------------------
def test_game_id_roundtrip():
    gid = ids.make_game_id(2026, "CFB", "REG", 5, "CFB_NEB", "CFB_PSU")
    assert gid == "2026_CFB_W05_NEB_PSU"
    assert ids.parse_game_id(gid)["home_team_id"] == "CFB_PSU"
    assert ids.make_game_id(2025, "NFL", "POST", 22, "NFL_BUF", "NFL_KC") == "2025_NFL_P22_BUF_KC"


def test_game_id_rejects_pre_2021_and_same_team():
    with pytest.raises(ValueError):
        ids.make_game_id(2020, "NFL", "REG", 1, "NFL_KC", "NFL_BUF")
    with pytest.raises(ValueError):
        ids.make_game_id(2026, "NFL", "REG", 1, "NFL_KC", "NFL_KC")


def test_alias_resolver_halts_on_unknown():
    r = ids.AliasResolver.load()
    r.add([{"provider": "nflverse", "alias": "KC", "provider_id": "2310", "team_id": "NFL_KC", "season_from": None, "season_to": None}])
    assert r.resolve("nflverse", alias="KC") == "NFL_KC"
    assert r.resolve("nflverse", provider_id=2310) == "NFL_KC"
    with pytest.raises(ids.UnmatchedAlias):
        r.resolve("nflverse", alias="Kansas City")
    assert r.unmatched[-1]["alias"] == "Kansas City"


# ---- nflverse (live) ----------------------------------------------------------------
class _Job:
    job_run_id = "test"


@pytest.mark.live
def test_nflverse_live_teams_and_schedules():
    from providers.base import RequestManager
    rm = RequestManager("nflverse", "test")
    raw_t = nflverse.fetch_teams(rm)
    teams, aliases = nflverse.normalize_teams(raw_t)
    assert len(teams) == 32
    assert "NFL_LAR" in set(teams.team_id) and "NFL_LA" not in set(teams.team_id)
    r = ids.AliasResolver.load(); r.add(aliases)
    raw = nflverse.fetch_schedules(rm)
    games, closes = nflverse.normalize_schedules(raw, 2026, r)
    assert len(games) >= 272
    g = games[games.game_id == "2026_NFL_W01_NE_SEA"].iloc[0]
    assert g.kickoff_utc == pd.Timestamp("2026-09-10T00:20:00Z")      # 20:20 ET -> 00:20 UTC next day
    assert g.venue_roof == "outdoors"
    hou = games[games.home_team_id == "NFL_HOU"].iloc[0]
    assert pd.isna(hou.venue_roof)                                          # missing roof stays NULL
    # 2025 backfill: closing-line sign flip check on a completed game
    g25, c25 = nflverse.normalize_schedules(raw, 2025, r)
    assert len(c25) > 250
    merged = c25.merge(raw[raw.season == 2025][["game_id", "spread_line"]].rename(columns={"game_id": "nv"}), how="inner",
                       left_on=c25.game_id.map(lambda x: None), right_on=raw.game_id.map(lambda x: None)) if False else None
    row = raw[(raw.season == 2025) & raw.home_score.notna()].iloc[0]
    gid = ids.make_game_id(2025, "NFL", config.NFL_GAME_TYPE_TO_SEASON_TYPE[row.game_type], int(row.week),
                           r.resolve("nflverse", alias=row.away_team), r.resolve("nflverse", alias=row.home_team))
    assert c25[c25.game_id == gid].spread_home.iloc[0] == -row.spread_line
    vlog = ValidationLog("test", "games")
    clean = validate.validate_games(games, "NFL", 2026, vlog)
    assert vlog.rejects == 0 and len(clean) == len(games)


# ---- CFBD (fixtures) -----------------------------------------------------------------
def _cfb_resolver():
    payload = json.loads((FIX / "cfbd_teams_fbs.json").read_text())
    teams, aliases, warnings = cfbd.normalize_teams(payload, NOW)
    assert not warnings and len(teams) == 3
    r = ids.AliasResolver.load(); r.add(aliases)
    return r


def test_cfbd_games_fcs_tagging_and_tba():
    r = _cfb_resolver()
    payload = json.loads((FIX / "cfbd_games_w1.json").read_text())
    games = cfbd.normalize_games(payload, 2026, r, NOW)
    assert len(games) == 2                                   # FCS-vs-FCS dropped
    nev = games[games.home_team_id == "CFB_NEV"].iloc[0]
    assert bool(nev.is_fcs_game) and nev.away_team_id.startswith("CFB_FCS")
    assert nev.status == "FINAL"
    psu = games[games.game_id == "2026_CFB_W01_NEB_PSU"].iloc[0]
    assert not bool(psu.is_fcs_game) and psu.kickoff_utc == pd.Timestamp("2026-09-05T19:30:00Z")
    vlog = ValidationLog("t", "games")
    assert len(validate.validate_games(games, "CFB", 2026, vlog)) == 2 and vlog.rejects == 0


def test_cfbd_lines_sign_check_rejects_mismatch():
    r = _cfb_resolver()
    games = cfbd.normalize_games(json.loads((FIX / "cfbd_games_w1.json").read_text()), 2026, r, NOW)
    gid_by = {900000001: "2026_CFB_W01_NEB_PSU", 900000002: games[games.home_team_id == "CFB_NEV"].game_id.iloc[0]}
    rejects: list[dict] = []
    snaps = cfbd.normalize_lines(json.loads((FIX / "cfbd_lines_w1.json").read_text()), 2026, gid_by, NOW, "free", rejects, set())
    # DraftKings row says "Nebraska -6.5" (away favored) but spread=-6.5 (home favored) -> rejected, not corrected
    assert len(rejects) == 1 and rejects[0]["rule"] == "SPREAD_SIGN_MISMATCH"
    assert set(snaps.book) == {"consensus", "bovada"}
    c = snaps[snaps.book == "consensus"].iloc[0]
    assert c.spread_home == -6.5 and c.provider_open_spread_home == -7.5 and c.total == 48.5 and c.is_first_snapshot
    b = snaps[snaps.book == "bovada"].iloc[0]
    assert pd.isna(b.spread_home) and b.total == 48.0            # missing spread stays NULL
    vlog = ValidationLog("t", "market")
    clean = validate.validate_market(snaps, set(games.game_id), vlog)
    assert vlog.rejects == 0 and len(clean) == 3


# ---- The Odds API (fixtures) --------------------------------------------------------
def test_odds_api_normalization_and_unmatched():
    r = ids.AliasResolver.load()
    r.add([
        {"provider": "odds_api", "alias": "Seattle Seahawks", "provider_id": None, "team_id": "NFL_SEA", "season_from": None, "season_to": None},
        {"provider": "odds_api", "alias": "New England Patriots", "provider_id": None, "team_id": "NFL_NE", "season_from": None, "season_to": None},
        {"provider": "odds_api", "alias": "Carolina Panthers", "provider_id": None, "team_id": "NFL_CAR", "season_from": None, "season_to": None},
        {"provider": "odds_api", "alias": "Chicago Bears", "provider_id": None, "team_id": "NFL_CHI", "season_from": None, "season_to": None},
    ])
    games = pd.DataFrame([
        {"game_id": "2026_NFL_W01_NE_SEA", "away_team_id": "NFL_NE", "home_team_id": "NFL_SEA", "kickoff_utc": "2026-09-14T00:20:00Z", "status": "SCHEDULED", "week": 1},
        {"game_id": "2026_NFL_W01_CHI_CAR", "away_team_id": "NFL_CHI", "home_team_id": "NFL_CAR", "kickoff_utc": "2026-09-13T17:00:00Z", "status": "SCHEDULED", "week": 1},
    ])
    unmatched: list[dict] = []
    snaps = odds_api.normalize_odds(json.loads((FIX / "odds_api_nfl.json").read_text()), "NFL", games, r, NOW, "free", set(), unmatched)
    assert len(unmatched) == 1 and "Washington Football Team" in unmatched[0]["reason"]
    assert len(snaps) == 3
    dk = snaps[(snaps.game_id == "2026_NFL_W01_NE_SEA") & (snaps.book == "draftkings")].iloc[0]
    assert dk.spread_home == -3.5 and dk.ml_home == -180 and dk.ml_away == 155 and dk.total == 44.5 and dk.over_price == -108
    car = snaps[snaps.game_id == "2026_NFL_W01_CHI_CAR"].iloc[0]
    assert car.spread_home == 2.5 and pd.isna(car.ml_home)
    vlog = ValidationLog("t", "market")
    assert len(validate.validate_market(snaps, set(games.game_id), vlog)) == 3 and vlog.rejects == 0


def test_validate_market_rejects_bad_rows():
    df = pd.DataFrame([
        {"snapshot_id": "a", "game_id": "G1", "spread_home": -3.25, "total": 45.0, "ml_home": -150, "ml_away": 130},
        {"snapshot_id": "b", "game_id": "G1", "spread_home": -3.0, "total": 145.0, "ml_home": -150, "ml_away": 130},
        {"snapshot_id": "c", "game_id": "G1", "spread_home": -3.0, "total": 45.0, "ml_home": -50, "ml_away": 130},
        {"snapshot_id": "d", "game_id": "G9", "spread_home": -3.0, "total": 45.0, "ml_home": -150, "ml_away": 130},
        {"snapshot_id": "e", "game_id": "G1", "spread_home": None, "total": None, "ml_home": None, "ml_away": None},
        {"snapshot_id": "f", "game_id": "G1", "spread_home": -3.0, "total": 45.0, "ml_home": -150, "ml_away": 130},
    ])
    vlog = ValidationLog("t", "market")
    clean = validate.validate_market(df, {"G1"}, vlog)
    assert list(clean.snapshot_id) == ["f"] and vlog.rejects == 5


# ---- storage -------------------------------------------------------------------------
def test_append_csv_is_insert_only(tmp_path):
    p = tmp_path / "x.csv"
    storage.append_csv(p, pd.DataFrame([{"k": 1, "v": "a"}]), ["k"])
    storage.append_csv(p, pd.DataFrame([{"k": 2, "v": "b"}]), ["k"])
    with pytest.raises(ValueError):
        storage.append_csv(p, pd.DataFrame([{"k": 1, "v": "changed"}]), ["k"], on_duplicate="reject")
    assert storage.append_csv(p, pd.DataFrame([{"k": 1, "v": "changed"}]), ["k"], on_duplicate="skip") == 0
    df = pd.read_csv(p)
    assert df.v.tolist() == ["a", "b"]


def test_upsert_games_protects_locked_fields():
    base = pd.DataFrame([{"game_id": "2026_NFL_W01_NE_SEA", "league": "NFL", "season": 2026, "season_type": "REG", "week": 1,
                          "away_team_id": "NFL_NE", "home_team_id": "NFL_SEA", "kickoff_utc": "2026-09-10T00:20:00Z",
                          "status": "SCHEDULED", "locked_at": None, "tv_network": None}])
    storage.upsert_games("NFL", 2026, base)
    # simulate lock
    g = storage.read_table(storage.games_path("NFL", 2026))
    g.loc[0, "status"] = "LOCKED"; g.loc[0, "locked_at"] = "2026-09-10T00:20:00Z"
    storage.write_parquet(storage.games_path("NFL", 2026), g)
    # provider now says a different kickoff and a TV network
    upd = base.copy(); upd.loc[0, "kickoff_utc"] = "2026-09-10T01:20:00Z"; upd.loc[0, "tv_network"] = "NBC"
    r = storage.upsert_games("NFL", 2026, upd)
    g = storage.read_table(storage.games_path("NFL", 2026)).iloc[0]
    assert g.kickoff_utc == "2026-09-10T00:20:00Z" and g.status == "LOCKED"     # protected
    assert g.tv_network == "NBC"                                                 # non-protected still updates
    assert len(r["warnings"]) == 1
