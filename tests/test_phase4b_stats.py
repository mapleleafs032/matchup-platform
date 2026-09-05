"""
Phase 4B tests. CFBD payloads here are SYNTHETIC fixtures shaped per the v2 docs, used only to exercise
normalization + the metric engine. The NFL test is live (public data) and cross-checks against the raw file.
"""
from __future__ import annotations
from datetime import datetime, timezone

import pandas as pd
import pytest

import config
from pipeline import ids, metrics_game
from providers import cfbd, cfbd_stats

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
GAME = pd.DataFrame([{"game_id": "2026_CFB_W01_NEB_PSU", "league": "CFB", "season": 2026, "season_type": "REG", "week": 1,
                      "away_team_id": "CFB_NEB", "home_team_id": "CFB_PSU", "kickoff_utc": "2026-09-05T19:30:00Z",
                      "status": "FINAL", "provider_game_ids": '{"cfbd":900000001}', "retrieved_at": NOW.isoformat()}])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(ids, "ALIASES_PATH", tmp_path / "tables" / "ref" / "team_aliases.csv")


def _resolver():
    r = ids.AliasResolver.load()
    r.add([{"provider": "cfbd", "alias": "Nebraska", "provider_id": "158", "team_id": "CFB_NEB", "season_from": None, "season_to": None},
           {"provider": "cfbd", "alias": "Penn State", "provider_id": "213", "team_id": "CFB_PSU", "season_from": None, "season_to": None}])
    return r


def _plays_payload():
    # PSU drive 1: rush 6, pass 22 (into RZ), rush -2 (TFL), pass TD; NEB drive: sack, pass INT.
    base = {"gameId": 900000001, "period": 1, "scoring": False}
    return [
        {**base, "id": 1, "driveId": 11, "offense": "Penn State", "defense": "Nebraska", "clock": {"minutes": 14, "seconds": 50}, "yardsToGoal": 75, "down": 1, "distance": 10, "yardsGained": 6, "playType": "Rush", "ppa": 0.3, "offenseScore": 0, "defenseScore": 0},
        {**base, "id": 2, "driveId": 11, "offense": "Penn State", "defense": "Nebraska", "clock": {"minutes": 14, "seconds": 20}, "yardsToGoal": 69, "down": 2, "distance": 4, "yardsGained": 22, "playType": "Pass Reception", "ppa": 1.4, "offenseScore": 0, "defenseScore": 0},
        {**base, "id": 3, "driveId": 11, "offense": "Penn State", "defense": "Nebraska", "clock": {"minutes": 13, "seconds": 45}, "yardsToGoal": 18, "down": 1, "distance": 10, "yardsGained": -2, "playType": "Rush", "ppa": -0.6, "offenseScore": 0, "defenseScore": 0},
        {**base, "id": 4, "driveId": 11, "offense": "Penn State", "defense": "Nebraska", "clock": {"minutes": 13, "seconds": 10}, "yardsToGoal": 20, "down": 2, "distance": 12, "yardsGained": 20, "playType": "Passing Touchdown", "ppa": 4.1, "offenseScore": 0, "defenseScore": 0, "scoring": True},
        {**base, "id": 5, "driveId": 12, "offense": "Nebraska", "defense": "Penn State", "clock": {"minutes": 12, "seconds": 30}, "yardsToGoal": 75, "down": 1, "distance": 10, "yardsGained": -7, "playType": "Sack", "ppa": -1.2, "offenseScore": 0, "defenseScore": 7},
        {**base, "id": 6, "driveId": 12, "offense": "Nebraska", "defense": "Penn State", "clock": {"minutes": 11, "seconds": 55}, "yardsToGoal": 82, "down": 2, "distance": 17, "yardsGained": 0, "playType": "Pass Interception Return", "ppa": -3.0, "offenseScore": 0, "defenseScore": 7},
        {"gameId": 900000001, "id": 7, "driveId": 11, "offense": "Penn State", "defense": "Nebraska", "period": 1, "clock": {"minutes": 13, "seconds": 5}, "yardsToGoal": 3, "down": 1, "distance": 3, "yardsGained": 0, "playType": "Extra Point Good", "ppa": None, "offenseScore": 6, "defenseScore": 0, "scoring": True},
    ]


def _drives_payload():
    return [
        {"gameId": 900000001, "id": 11, "driveNumber": 1, "offense": "Penn State", "defense": "Nebraska", "startPeriod": 1, "startYardsToGoal": 75, "endYardsToGoal": 0, "plays": 4, "yards": 75, "driveResult": "TD", "startOffenseScore": 0, "endOffenseScore": 7, "startDefenseScore": 0, "elapsed": {"minutes": 1, "seconds": 45}},
        {"gameId": 900000001, "id": 12, "driveNumber": 2, "offense": "Nebraska", "defense": "Penn State", "startPeriod": 1, "startYardsToGoal": 75, "endYardsToGoal": 82, "plays": 2, "yards": -7, "driveResult": "INT", "startOffenseScore": 0, "endOffenseScore": 0, "startDefenseScore": 7, "elapsed": {"minutes": 0, "seconds": 40}},
    ]


def test_cfbd_plays_and_drives_normalize():
    r = _resolver(); missing = set()
    plays = cfbd_stats.normalize_plays(_plays_payload(), GAME, r, NOW, missing)
    drives = cfbd_stats.normalize_drives(_drives_payload(), GAME, r, NOW, missing)
    assert not missing
    assert len(plays) == 7 and plays.play_type.tolist() == ["RUSH", "PASS", "RUSH", "PASS", "SACK", "PASS", "XP"]
    sack = plays[plays.play_id.str.endswith("_5")].iloc[0]
    assert sack.is_dropback and sack.is_sack and not sack.is_turnover
    intc = plays[plays.play_id.str.endswith("_6")].iloc[0]
    assert intc.is_turnover and intc.turnover_type == "INT" and not intc.is_complete
    assert plays.game_sec_remaining.iloc[0] == 3 * 900 + 890
    assert not plays.is_garbage_time.any()
    d1 = drives[drives.drive_id.str.endswith("_11")].iloc[0]
    assert d1.result == "TD" and d1.points == 7 and d1.reached_opp_40 and d1.reached_rz
    d2 = drives[drives.drive_id.str.endswith("_12")].iloc[0]
    assert d2.result == "TURNOVER" and d2.points == 0 and not d2.reached_opp_40


def test_metric_engine_cfb_definitions():
    r = _resolver(); missing = set()
    plays = cfbd_stats.normalize_plays(_plays_payload(), GAME, r, NOW, missing)
    drives = cfbd_stats.normalize_drives(_drives_payload(), GAME, r, NOW, missing)
    row = metrics_game.team_game_advanced(plays, drives, "2026_CFB_W01_NEB_PSU", "CFB_PSU", "CFB_NEB", "CFB", False, "cfbd", "t", "t")
    assert row["metric_system"] == "PPA_CFBD"
    assert row["off_plays"] == 4 and row["def_plays"] == 2
    # yardage-rule success: 6/10 on 1st (>=5 yes), 22/4 yes, -2 no, 20/12 yes -> 3/4
    assert abs(row["off_success_rate"] - 0.75) < 1e-9
    assert abs(row["off_ppa_play"] - (0.3 + 1.4 - 0.6 + 4.1) / 4) < 1e-9
    assert abs(row["off_explosive_play_rate"] - 0.5) < 1e-9          # 22-yd and 20-yd passes
    assert abs(row["off_pass_rate"] - 0.5) < 1e-9
    assert row["off_pts_per_scoring_opp"] == 7 and row["off_rz_td_rate"] == 1.0 and row["off_avg_start_yardline"] == 25
    # defense = NEB's offense: sack + INT on 2 plays -> havoc 1.0, front 0.5, db 0.5
    assert row["def_havoc"] == 1.0 and row["def_havoc_front"] == 0.5 and row["def_havoc_db"] == 0.5
    assert row["def_pts_per_scoring_opp_allowed"] is None            # NEB never reached the 40 -> no opps -> NULL, not 0


def test_cfbd_team_box_ambiguous_fields_left_null():
    r = _resolver(); missing = set()
    payload = [{"id": 900000001, "teams": [
        {"teamId": 213, "team": "Penn State", "homeAway": "home", "points": 31, "stats": [
            {"category": "rushingYards", "stat": "185"}, {"category": "netPassingYards", "stat": "240"}, {"category": "totalYards", "stat": "425"},
            {"category": "rushingAttempts", "stat": "38"}, {"category": "completionAttempts", "stat": "22-30"}, {"category": "thirdDownEff", "stat": "7-13"},
            {"category": "fourthDownEff", "stat": "1-1"}, {"category": "turnovers", "stat": "1"}, {"category": "fumblesLost", "stat": "1"},
            {"category": "sacks", "stat": "4"}, {"category": "tacklesForLoss", "stat": "8"}, {"category": "possessionTime", "stat": "33:12"},
            {"category": "firstDowns", "stat": "23"}, {"category": "passingTDs", "stat": "2"}, {"category": "rushingTDs", "stat": "2"},
            {"category": "passesDeflected", "stat": "5"}, {"category": "interceptions", "stat": "2"}, {"category": "totalPenaltiesYards", "stat": "6-55"}]},
        {"teamId": 158, "team": "Nebraska", "homeAway": "away", "points": 17, "stats": [
            {"category": "rushingYards", "stat": "90"}, {"category": "netPassingYards", "stat": "210"}, {"category": "totalYards", "stat": "300"},
            {"category": "rushingAttempts", "stat": "25"}, {"category": "completionAttempts", "stat": "18-35"}, {"category": "thirdDownEff", "stat": "4-14"},
            {"category": "fourthDownEff", "stat": "0-2"}, {"category": "turnovers", "stat": "3"}, {"category": "fumblesLost", "stat": "1"},
            {"category": "sacks", "stat": "1"}, {"category": "tacklesForLoss", "stat": "3"}, {"category": "possessionTime", "stat": "26:48"},
            {"category": "firstDowns", "stat": "15"}, {"category": "passingTDs", "stat": "1"}, {"category": "rushingTDs", "stat": "1"},
            {"category": "passesDeflected", "stat": "2"}, {"category": "interceptions", "stat": "0"}, {"category": "totalPenaltiesYards", "stat": "4-30"}]}]}]
    box = cfbd_stats.normalize_team_box(payload, GAME, r, NOW, missing)
    assert not missing and len(box) == 2
    psu = box[box.team_id == "CFB_PSU"].iloc[0]
    assert psu.points == 31 and psu.points_allowed == 17 and psu.pass_cmp == 22 and psu.pass_att == 30
    assert psu.third_down_conv == 7 and psu.third_down_att == 13 and psu.possession_sec == 33 * 60 + 12
    assert psu.sacks_made == 4 and psu.int_made == 2 and psu.penalties == 6 and psu.penalty_yds == 55
    assert pd.isna(psu.pass_int) and pd.isna(psu.sacks_taken)     # ambiguous in box -> filled from plays, never guessed


def test_cfbd_advanced_overlay_columns():
    r = _resolver(); missing = set()
    payload = [{"gameId": 900000001, "week": 1, "team": "Penn State", "opponent": "Nebraska",
                "offense": {"plays": 70, "ppa": 0.31, "successRate": 0.49, "explosiveness": 1.32, "powerSuccess": 0.8, "stuffRate": 0.14,
                            "lineYards": 3.1, "secondLevelYards": 1.1, "openFieldYards": 1.9,
                            "standardDowns": {"ppa": 0.3, "successRate": 0.55}, "passingDowns": {"ppa": 0.2, "successRate": 0.35},
                            "rushingPlays": {"ppa": 0.2, "successRate": 0.5}, "passingPlays": {"ppa": 0.45, "successRate": 0.48},
                            "havoc": {"total": 0.12, "frontSeven": 0.08, "db": 0.04}},
                "defense": {"plays": 60, "ppa": -0.05, "successRate": 0.38, "explosiveness": 1.05, "powerSuccess": 0.6, "stuffRate": 0.22,
                            "lineYards": 2.4, "secondLevelYards": 0.8, "openFieldYards": 0.7,
                            "standardDowns": {"ppa": 0.0, "successRate": 0.42}, "passingDowns": {"ppa": -0.1, "successRate": 0.3},
                            "rushingPlays": {"ppa": -0.1, "successRate": 0.36}, "passingPlays": {"ppa": 0.0, "successRate": 0.4},
                            "havoc": {"total": 0.2, "frontSeven": 0.13, "db": 0.07}}}]
    adv = cfbd_stats.normalize_advanced(payload, GAME, r, NOW, True, missing)
    assert not missing and len(adv) == 1
    a = adv.iloc[0]
    assert a.off_ppa_play == 0.31 and a.off_success_std_downs == 0.55 and a.off_stuff_rate_allowed == 0.14 and a.off_havoc_allowed == 0.12
    assert a.def_havoc == 0.2 and a.def_havoc_front == 0.13 and a.def_stuff_rate == 0.22 and a.def_line_yards_allowed == 2.4
    assert "off_stuff_rate" not in adv.columns and "def_line_yards" not in adv.columns


@pytest.mark.live
def test_nfl_2025_week1_matches_raw():
    """Requires a prior run of ingest_stats for NFL 2025 W1 in the repo data dir; verifies EPA/play against raw."""
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "data/tables/stats/team_game_advanced/NFL/2025.parquet"
    if not path.exists():
        pytest.skip("run ingest_stats NFL 2025 first")
    adv = pd.read_parquet(path)
    row = adv[(adv.game_id == "2025_NFL_W01_DAL_PHI") & (adv.team_id == "NFL_PHI") & (~adv.is_garbage_filtered)].iloc[0]
    assert abs(row.off_ppa_play - 0.160) < 0.002 and abs(row.off_success_rate - 0.508) < 0.002
