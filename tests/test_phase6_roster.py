from __future__ import annotations
from datetime import datetime, timezone

import pandas as pd
import pytest

import config
from pipeline import ids, roster_engine as eng
from providers import cfbd_roster

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(ids, "ALIASES_PATH", tmp_path / "tables" / "ref" / "team_aliases.csv")


def _resolver():
    r = ids.AliasResolver.load()
    r.add([{"provider": "cfbd", "alias": "Nebraska", "provider_id": "158", "team_id": "CFB_NEB", "season_from": None, "season_to": None},
           {"provider": "cfbd", "alias": "Penn State", "provider_id": "213", "team_id": "CFB_PSU", "season_from": None, "season_to": None}])
    return r


def test_cfbd_player_season_stats_pivot_and_usage():
    r = _resolver(); un = set()
    payload = [{"season": 2025, "playerId": 1001, "player": "Dylan Raiola", "position": "QB", "team": "Nebraska", "category": "passing", "statType": "ATT", "stat": "410"},
               {"season": 2025, "playerId": 1001, "player": "Dylan Raiola", "position": "QB", "team": "Nebraska", "category": "passing", "statType": "YDS", "stat": "3200"},
               {"season": 2025, "playerId": 1001, "player": "Dylan Raiola", "position": "QB", "team": "Nebraska", "category": "passing", "statType": "PCT", "stat": "64.1"},
               {"season": 2025, "playerId": 2002, "player": "Big Guy", "position": "DL", "team": "Nebraska", "category": "defensive", "statType": "SACKS", "stat": "7.5"},
               {"season": 2025, "playerId": 2002, "player": "Big Guy", "position": "DL", "team": "Nebraska", "category": "defensive", "statType": "TOT", "stat": "40"},
               {"season": 2025, "playerId": 3003, "player": "FCS Guy", "position": "WR", "team": "Montana", "category": "receiving", "statType": "REC", "stat": "50"}]
    st = cfbd_roster.normalize_player_season_stats(payload, 2025, r, NOW, un)
    assert un == {"Montana"} and len(st) == 2
    qb = st[st.player_id == "CFB_P_1001"].iloc[0]
    assert qb.pass_att == 410 and qb.pass_yds == 3200 and "PCT" not in st.columns
    dl = st[st.player_id == "CFB_P_2002"].iloc[0]
    assert dl.sacks == 7.5 and dl.tackles == 40 and dl.position == "DL"
    us = cfbd_roster.normalize_usage([{"season": 2025, "id": 1001, "name": "Dylan Raiola", "position": "QB", "team": "Nebraska",
                                       "usage": {"overall": 0.41, "pass": 0.98, "rush": 0.05}}], 2025, r, NOW, set())
    assert us.iloc[0].usage_overall == 0.41 and us.iloc[0].usage_pass == 0.98


def test_cfbd_portal_returning_talent_classes():
    r = _resolver(); un = set()
    portal = cfbd_roster.normalize_portal([{"season": 2026, "firstName": "New", "lastName": "Quarterback", "position": "QB", "origin": "Penn State",
                                            "destination": "Nebraska", "transferDate": "2026-01-10T00:00:00Z", "rating": 0.93, "stars": 4, "eligibility": "Immediate"}], 2026, r, NOW, un)
    assert portal.iloc[0].from_team_id == "CFB_PSU" and portal.iloc[0].to_team_id == "CFB_NEB" and portal.iloc[0].transfer_rank == 1
    ret = cfbd_roster.normalize_returning([{"season": 2026, "team": "Nebraska", "percentPPA": 0.62, "percentPassingPPA": 0.9, "usage": 0.55}], 2026, r, NOW, un)
    assert ret.iloc[0].rp_total == 0.62 and ret.iloc[0].rp_passing == 0.9 and pd.isna(ret.iloc[0].rp_defense)     # CFBD returning is offense only
    tal = cfbd_roster.normalize_talent([{"year": 2026, "team": "Nebraska", "talent": 812.5}, {"year": 2026, "team": "Penn State", "talent": 905.1}], 2026, r, NOW, un)
    assert tal[tal.team_id == "CFB_PSU"].talent_rank.iloc[0] == 1
    rec = cfbd_roster.normalize_recruits([{"id": 1, "athleteId": 5001, "name": "A", "position": "WR", "committedTo": "Nebraska", "stars": 4, "rating": 0.92, "ranking": 80, "recruitType": "HighSchool"},
                                          {"id": 2, "athleteId": 5002, "name": "B", "position": "OT", "committedTo": "Nebraska", "stars": 3, "rating": 0.86, "ranking": 400, "recruitType": "HighSchool"},
                                          {"id": 3, "athleteId": 5003, "name": "C", "position": "QB", "committedTo": "Nebraska", "stars": 4, "rating": 0.90, "ranking": 120, "recruitType": "JUCO"}], 2026, r, NOW, un)
    classes = cfbd_roster.class_summary(rec, pd.DataFrame([{"team_id": "CFB_NEB", "season": 2026, "class_rank": 30, "class_points": 210.5}]), 2026)
    c = classes.iloc[0]
    assert c.commits == 2 and c.four_stars == 1 and abs(c.blue_chip_ratio - 0.5) < 1e-9 and c.class_rank == 30    # JUCO excluded from HS class


def _prior_usage():
    return pd.DataFrame([
        {"player_id": "P1", "team_id": "T", "season": 2025, "player_name": "Star QB", "position": "QB", "usage_overall": 0.40, "pass_att": 400, "rush_att": 50, "receptions": 0, "tackles": 0, "tfl": 0, "sacks": 0, "ints": 0, "pbu": 0, "qb_hurries": 0},
        {"player_id": "P2", "team_id": "T", "season": 2025, "player_name": "Lead RB", "position": "RB", "usage_overall": 0.20, "pass_att": 0, "rush_att": 200, "receptions": 20, "tackles": 0, "tfl": 0, "sacks": 0, "ints": 0, "pbu": 0, "qb_hurries": 0},
        {"player_id": "P3", "team_id": "T", "season": 2025, "player_name": "WR One", "position": "WR", "usage_overall": 0.15, "pass_att": 0, "rush_att": 0, "receptions": 80, "tackles": 0, "tfl": 0, "sacks": 0, "ints": 0, "pbu": 0, "qb_hurries": 0},
        {"player_id": "P4", "team_id": "T", "season": 2025, "player_name": "Edge Guy", "position": "EDGE", "usage_overall": 0.0, "pass_att": 0, "rush_att": 0, "receptions": 0, "tackles": 40, "tfl": 12, "sacks": 8, "ints": 0, "pbu": 1, "qb_hurries": 10},
        {"player_id": "P5", "team_id": "T", "season": 2025, "player_name": "Corner", "position": "CB", "usage_overall": 0.0, "pass_att": 0, "rush_att": 0, "receptions": 0, "tackles": 50, "tfl": 2, "sacks": 0, "ints": 3, "pbu": 8, "qb_hurries": 0},
        {"player_id": "P6", "team_id": "T", "season": 2025, "player_name": "Left Tackle", "position": "OL", "usage_overall": 0.0, "pass_att": 0, "rush_att": 0, "receptions": 0, "tackles": 0, "tfl": 0, "sacks": 0, "ints": 0, "pbu": 0, "qb_hurries": 0},
    ])


def test_returning_production_cfb_rules():
    prior = _prior_usage()
    # QB left (transfer), RB/WR/CB/OL return, EDGE drafted
    roster_now = pd.DataFrame([{"team_id": "T", "player_id": p, "position": pos, "years_exp": 3} for p, pos in (("P2", "RB"), ("P3", "WR"), ("P5", "CB"), ("P6", "OL"), ("NEWQB", "QB"))])
    rp = eng.returning_production("CFB", 2026, 1, roster_now, prior).iloc[0]
    assert rp.rp_passing == 0.0 and rp.rp_rushing == 200 / 250 and rp.rp_receiving == 1.0
    assert rp.ol_starts_returning == 1 and bool(rp.ol_starts_returning_is_proxy)
    assert 0 < rp.rp_defense < 1 and rp.def_pressure_returning == 0.0 and rp.secondary_snaps_returning == 1.0
    assert 0 < rp.rp_total < 1
    dep = eng.departures("CFB", 2026, roster_now, prior, pd.DataFrame([{"player_id": "P4"}]),
                         pd.DataFrame([{"from_team_id": "T", "player_name": "Star QB"}]))
    assert dict(zip(dep.player_id, dep.category)) == {"P1": "TRANSFER", "P4": "DRAFT"}


def test_qb_status_never_inherits_and_flags_injury():
    prior = _prior_usage()
    players = pd.DataFrame([{"player_id": "P1", "full_name": "Star QB"}, {"player_id": "NEWQB", "full_name": "New Guy"}, {"player_id": "BACKUP", "full_name": "Backup"}])
    roster_now = pd.DataFrame([{"team_id": "T", "player_id": "NEWQB", "position": "QB", "years_exp": 1}, {"team_id": "T", "player_id": "BACKUP", "position": "QB", "years_exp": 2}])
    cutoff = pd.Timestamp("2026-09-12T00:00:00Z")
    empty_pgs = pd.DataFrame(columns=["game_id", "team_id", "player_id", "pass_att", "pass_cmp", "pass_yds", "pass_td", "pass_int", "dropbacks", "ppa_dropback", "effective_at", "season"])
    q = eng.qb_status("CFB", 2026, 2, cutoff, ["T"], roster_now, players, empty_pgs, prior, pd.DataFrame(), None, None, None).iloc[0]
    assert q.player_id is None and "UNKNOWN_STARTER" in q["flags"] and q.confidence <= 0.35 and q.prior_season_starter_id == "P1" and not q.prior_starter_returning
    # in-season: last game's passer becomes the projection; then an OUT injury flips to the backup
    pgs = pd.DataFrame([{"game_id": "G1", "team_id": "T", "player_id": "NEWQB", "pass_att": 30, "pass_cmp": 20, "pass_yds": 250, "pass_td": 2, "pass_int": 1, "dropbacks": 33, "ppa_dropback": 0.1, "effective_at": "2026-09-06T00:00:00Z", "season": 2026},
                        {"game_id": "G1", "team_id": "T", "player_id": "BACKUP", "pass_att": 3, "pass_cmp": 2, "pass_yds": 10, "pass_td": 0, "pass_int": 0, "dropbacks": 3, "ppa_dropback": -0.2, "effective_at": "2026-09-06T00:00:00Z", "season": 2026}])
    q = eng.qb_status("CFB", 2026, 2, cutoff, ["T"], roster_now, players, pgs, prior, pd.DataFrame(), None, None, None).iloc[0]
    assert q.player_id == "NEWQB" and q.projection_basis == "last_game_starter" and q.confidence == 0.9 and q.career_games_10att == 1
    inj = pd.DataFrame([{"season": 2026, "week": 2, "player_id": "NEWQB", "status": "OUT"}])
    q = eng.qb_status("CFB", 2026, 2, cutoff, ["T"], roster_now, players, pgs, prior, inj, None, None, None).iloc[0]
    assert q.player_id == "BACKUP" and "INJURY_REPLACEMENT" in q["flags"] and q.confidence == 0.5


def test_projected_depth_chart_ordering_and_slots():
    prior = _prior_usage()
    roster_now = pd.DataFrame([{"team_id": "T", "player_id": p, "position": pos, "years_exp": y} for p, pos, y in
                               (("P2", "RB", 3), ("RB2", "RB", 1), ("P3", "WR", 3), ("WR2", "WR", 2), ("WR3", "WR", 1), ("WR4", "WR", 1),
                                ("NEWQB", "QB", 2), ("BACKUP", "QB", 1), ("P6", "OL", 4), ("OL2", "OL", 2), ("OL3", "OL", 1), ("OL4", "OL", 1), ("OL5", "OL", 1), ("OL6", "OL", 1))])
    transfers = pd.DataFrame([{"transfer_id": "x", "player_id": "NEWQB", "rating": 0.93, "to_team_id": "T"}])
    recruits = pd.DataFrame([{"athlete_id": 999, "rating": 0.95}])   # -> CFB_P_999 not on roster; ignored
    dc = eng.project_depth_chart_cfb(2026, 1, roster_now, prior, None, transfers, recruits)
    qb1 = dc[(dc.slot == "QB1") & (dc.rank_in_slot == 1)].iloc[0]
    assert qb1.player_id == "NEWQB" and qb1.projection_basis == "portal_rank" and qb1.is_projected
    assert dc[(dc.slot == "RB1") & (dc.rank_in_slot == 1)].player_id.iloc[0] == "P2"
    assert set(dc[dc.slot.str.startswith("WR") & (dc.rank_in_slot == 1)].player_id) == {"P3", "WR2", "WR3"}
    assert len(dc[dc.slot.isin(["LT", "LG", "C", "RG", "RT"]) & (dc.rank_in_slot == 1)]) == 5


def test_continuity_index_uses_components():
    rp = pd.DataFrame([{"team_id": "T", "rp_total": 0.8}])
    qb = pd.DataFrame([{"team_id": "T", "flags": "RETURNING_STARTER"}])
    coaches = pd.DataFrame([{"team_id": "T", "season": 2026, "role": "HC", "coach_id": "x"}, {"team_id": "T", "season": 2025, "role": "HC", "coach_id": "y"}])
    c = eng.continuity("CFB", 2026, rp, qb, coaches, pd.DataFrame([{"to_team_id": "T", "transfer_id": i} for i in range(5)])).iloc[0]
    assert c.c_qb == 1.0 and c.c_hc == 0.0 and c.hc_changed and abs(c.c_portal_churn - 0.8) < 1e-9
    assert abs(c.continuity_index - (0.45 * 0.8 + 0.25 * 1.0 + 0.15 * 0.0 + 0.15 * 0.8)) < 1e-9
