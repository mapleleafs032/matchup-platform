from __future__ import annotations
from datetime import datetime, timezone

import pandas as pd
import pytest

import config
from pipeline import ids
from providers import cfbd_context, open_meteo

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(ids, "ALIASES_PATH", tmp_path / "tables" / "ref" / "team_aliases.csv")


def _resolver():
    r = ids.AliasResolver.load()
    r.add([{"provider": "cfbd", "alias": "Nebraska", "provider_id": "158", "team_id": "CFB_NEB", "season_from": None, "season_to": None},
           {"provider": "cfbd", "alias": "Penn State", "provider_id": "213", "team_id": "CFB_PSU", "season_from": None, "season_to": None}])
    return r


def test_cfbd_roster_and_players():
    r = _resolver(); unmatched = set()
    payload = [{"id": 1001, "firstName": "Dylan", "lastName": "Raiola", "team": "Nebraska", "position": "QB", "jersey": 15, "year": 3, "height": 75, "weight": 220},
               {"id": 1002, "firstName": "Some", "lastName": "Lineman", "team": "Nebraska", "position": "OT", "jersey": 71, "year": 1, "height": 78, "weight": 305},
               {"id": 1003, "firstName": "FCS", "lastName": "Guy", "team": "Montana", "position": "WR", "jersey": 1, "year": 2}]
    prior = pd.DataFrame([{"team_id": "CFB_PSU", "season": 2025, "week": 14, "player_id": "CFB_P_1001"}])
    ros, players = cfbd_context.normalize_roster(payload, 2026, 2, r, NOW, prior, unmatched)
    assert unmatched == {"Montana"} and len(ros) == 2 and len(players) == 2
    qb = ros[ros.player_id == "CFB_P_1001"].iloc[0]
    assert qb.position == "QB" and qb.class_year == "JR" and qb.arrival_type == "TRANSFER" and qb.prior_team_id == "CFB_PSU"
    ol = ros[ros.player_id == "CFB_P_1002"].iloc[0]
    assert ol.position == "OL" and ol.arrival_type == "FRESHMAN"


def test_cfbd_rankings_polls():
    r = _resolver(); unmatched = set()
    payload = [{"season": 2026, "seasonType": "regular", "week": 2, "polls": [
        {"poll": "AP Top 25", "ranks": [{"rank": 1, "school": "Penn State", "conference": "Big Ten", "firstPlaceVotes": 40, "points": 1500},
                                        {"rank": 25, "school": "Nebraska", "conference": "Big Ten", "firstPlaceVotes": 0, "points": 100},
                                        {"rank": 3, "school": "Not A Team", "conference": "X", "firstPlaceVotes": 0, "points": 1300}]},
        {"poll": "Some Other Poll", "ranks": [{"rank": 1, "school": "Nebraska"}]}]}]
    rk = cfbd_context.normalize_rankings(payload, r, NOW, unmatched)
    assert len(rk) == 2 and set(rk.poll) == {"AP"} and unmatched == {"Not A Team"}
    assert rk[rk.team_id == "CFB_PSU"].iloc[0]["rank"] == 1


def test_cfbd_coaches_flags_midseason_change():
    r = _resolver(); unmatched = set()
    payload = [{"firstName": "Matt", "lastName": "Rhule", "seasons": [{"school": "Nebraska", "year": 2026, "games": 3, "wins": 2, "losses": 1}, {"school": "Nebraska", "year": 2025}]},
               {"firstName": "Interim", "lastName": "Coach", "seasons": [{"school": "Nebraska", "year": 2026, "games": 1, "wins": 0, "losses": 1}]},
               {"firstName": "James", "lastName": "Franklin", "seasons": [{"school": "Penn State", "year": 2026}]}]
    co = cfbd_context.normalize_coaches(payload, 2026, r, NOW, unmatched)
    assert len(co) == 3 and co[co.team_id == "CFB_NEB"].needs_manual_dates.all() and not co[co.team_id == "CFB_PSU"].needs_manual_dates.any()


def test_cfbd_venues():
    v = cfbd_context.normalize_venues([{"id": 3862, "name": "Memorial Stadium", "city": "Lincoln", "state": "NE", "latitude": 40.82, "longitude": -96.7,
                                        "elevation": 360.5, "timezone": "America/Chicago", "capacity": 85458, "grass": False, "dome": False}], NOW)
    x = v.iloc[0]
    assert x.venue_id == "V_CFBD_3862" and x.roof == "outdoors" and x.surface == "turf" and x.timezone == "America/Chicago"


def test_open_meteo_pick_hour_and_indoor_rule():
    payload = {"hourly": {"time": ["2026-09-12T18:00", "2026-09-12T19:00", "2026-09-12T20:00"],
                          "temperature_2m": [70.1, 68.5, 66.0], "apparent_temperature": [70, 68, 65], "relative_humidity_2m": [55, 60, 65],
                          "precipitation_probability": [10, 40, 60], "precipitation": [0.0, 0.02, 0.1],
                          "wind_speed_10m": [8.0, 12.5, 15.0], "wind_gusts_10m": [15.0, 22.0, 28.0], "wind_direction_10m": [200, 210, 220], "weather_code": [1, 3, 61]}}
    k = pd.Timestamp("2026-09-12T19:30:00Z")
    vals = open_meteo.pick_hour(payload, k)
    assert vals["temp_f"] == 68.5 and vals["wind_mph"] == 12.5 and vals["precip_prob"] == 0.4 and vals["wind_gust_mph"] == 22.0
    assert open_meteo.pick_hour(payload, pd.Timestamp("2026-09-13T01:00:00Z")) is None
    row = open_meteo.snapshot_row("G", k, "retractable_closed", vals, NOW)
    assert row["is_indoor"] and row["temp_f"] is None and row["wind_mph"] is None       # indoor -> weather fields NULL by rule
    row = open_meteo.snapshot_row("G", k, "retractable", vals, NOW)
    assert not row["is_indoor"] and row["roof_status_unknown"] and row["temp_f"] == 68.5
    row = open_meteo.snapshot_row("G", k, "outdoors", None, NOW)
    assert row["temp_f"] is None and not row["is_indoor"]                                    # not covered -> NULL, not 0


def test_nfl_venues_static_table_is_complete():
    import pathlib
    v = pd.read_csv(pathlib.Path(__file__).resolve().parents[1] / "data" / "manual" / "nfl_venues.csv")
    assert len(v) >= 41 and v.venue_id.is_unique and v.latitude.between(-90, 90).all() and v.longitude.between(-180, 180).all()
    assert set(v.roof) <= {"outdoors", "dome", "retractable"}
