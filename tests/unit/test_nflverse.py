from datetime import UTC, datetime

import pytest

from edge.core.errors import ValidationFailure
from edge.sources.base import FetchResult
from edge.sources.nflverse import NflGameRecord, franchise, normalize_schedules
from tests.conftest import FIXTURES, NOW

BODY = (FIXTURES / "nflverse_games_sample.csv").read_bytes()


def _games(body: bytes = BODY) -> dict[str, NflGameRecord]:
    r = normalize_schedules(FetchResult("nflverse", "x", {}, 200, body, NOW))
    return {g.game_id: g for g in r.games}


def test_spread_sign_converted_to_book_convention() -> None:
    g = _games()["2024_01_BAL_KC"]
    # nflverse spread_line +3.0 (home KC favored) -> book handicap -3.0
    assert g.home == "KC" and g.home_handicap == -3.0
    assert (g.home_score, g.away_score, g.margin) == (27, 20, 7)
    assert (g.home_ml, g.away_ml, g.total_line) == (-148, 124, 46.0)


def test_eastern_kickoff_converted_to_utc_across_dst() -> None:
    gs = _games()
    assert gs["2024_01_BAL_KC"].kickoff == datetime(2024, 9, 6, 0, 20, tzinfo=UTC)  # EDT
    late = gs["2024_13_PIT_CIN"]  # December: EST (UTC-5)
    assert late.kickoff == datetime(2024, 12, 1, 18, 0, tzinfo=UTC)  # 13:00 EST = 18:00 UTC
    assert late.kickoff_time_known


def test_relocated_franchises_map_to_one_key() -> None:
    gs = _games()
    assert gs["2015_01_SEA_STL"].home == "LAR"
    assert gs["2016_01_LA_SF"].away == "LAR"
    assert franchise("SD") == "LAC" and franchise("OAK") == "LV"
    with pytest.raises(ValidationFailure):
        franchise("XYZ")


def test_unknown_kickoff_time_gets_conservative_timestamps() -> None:
    g = _games()["1999_01_MIN_ATL"]
    assert not g.kickoff_time_known
    assert g.kickoff.astimezone().date() is not None
    assert (g.result_available_at - g.kickoff).total_seconds() == 30 * 3600


def test_upcoming_game_has_no_scores() -> None:
    up = [g for g in _games().values() if g.season == 2026]
    assert up and not up[0].completed and up[0].margin is None


def test_neutral_site_flag() -> None:
    assert any(g.neutral for g in _games().values())


def test_post_game_facts_are_not_exported() -> None:
    fields = set(NflGameRecord.__dataclass_fields__)
    assert not fields & {"home_qb_name", "home_qb_id", "temp", "wind", "referee"}


def _mutate(col: str, value: str, game_id: str = "2024_01_BAL_KC") -> bytes:
    lines = BODY.decode().splitlines()
    header = lines[0].split(",")
    i = header.index(col)
    out = [lines[0]]
    for ln in lines[1:]:
        parts = ln.split(",")
        if parts[0] == game_id:
            parts[i] = value
        out.append(",".join(parts))
    return ("\n".join(out) + "\n").encode()


def test_inconsistent_result_rejected() -> None:
    with pytest.raises(ValidationFailure, match="result"):
        _games(_mutate("result", "8"))


def test_unknown_team_rejected() -> None:
    with pytest.raises(ValidationFailure, match="unknown nflverse team"):
        _games(_mutate("home_team", "ZZZ"))


def test_duplicate_game_id_rejected() -> None:
    lines = BODY.decode().splitlines()
    with pytest.raises(ValidationFailure, match="duplicate"):
        _games(("\n".join([*lines, lines[1]]) + "\n").encode())


def test_missing_column_rejected() -> None:
    lines = BODY.decode().splitlines()
    lines[0] = lines[0].replace("spread_line", "spread")
    with pytest.raises(ValidationFailure, match="missing columns"):
        _games(("\n".join(lines) + "\n").encode())


def test_implausible_line_dropped_and_reported() -> None:
    r = normalize_schedules(
        FetchResult("nflverse", "x", {}, 200, _mutate("spread_line", "45"), NOW)
    )
    g = next(x for x in r.games if x.game_id == "2024_01_BAL_KC")
    assert g.home_handicap is None and any("implausible spread" in i for i in r.issues)
