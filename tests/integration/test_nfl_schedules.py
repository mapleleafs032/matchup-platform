from datetime import timedelta

import pytest
from sqlalchemy import func, select

from edge.ingest.archive import LocalArchive
from edge.ingest.nfl_schedules import ingest_nfl_schedules
from edge.ingest.seed import seed_reference
from edge.sources.base import FetchResult
from edge.store.models import ExternalId, Game, GameResult, League, Team
from tests.conftest import FIXTURES, NOW

pytestmark = pytest.mark.postgres
BODY = (FIXTURES / "nflverse_games_sample.csv").read_bytes()


def _fetch(body: bytes = BODY) -> FetchResult:
    return FetchResult("nflverse", "games.csv", {}, 200, body, NOW)


def test_ingest_creates_games_results_and_is_idempotent(session, config, tmp_path) -> None:
    seed_reference(session, config)
    session.commit()
    s = ingest_nfl_schedules(session, _fetch(), LocalArchive(tmp_path), min_season=1999)
    assert s.games_seen == 7 and s.games_created == 7 and s.results_written == 6
    again = ingest_nfl_schedules(session, _fetch(), LocalArchive(tmp_path), min_season=1999)
    assert again.games_created == 0 and again.results_written == 0
    assert session.scalar(select(func.count()).select_from(GameResult)) == 6
    # historical results are backfills with conservative availability
    r = session.scalar(select(GameResult).where(GameResult.is_backfill.is_(True)).limit(1))
    assert r is not None and r.available_at < NOW


def test_score_correction_supersedes_without_overwrite(session, config, tmp_path) -> None:
    seed_reference(session, config)
    session.commit()
    ingest_nfl_schedules(session, _fetch(), LocalArchive(tmp_path), min_season=1999)
    fixed = BODY.decode().replace(",20,KC,27,", ",20,KC,28,").replace(",7,47,", ",8,48,", 1)
    s = ingest_nfl_schedules(
        session, _fetch(fixed.encode()), LocalArchive(tmp_path), min_season=1999
    )
    assert s.results_superseded == 1
    rows = list(session.scalars(select(GameResult).order_by(GameResult.id)))
    assert len(rows) == 7 and rows[-1].supersedes_id is not None and rows[-1].home_score == 28


def test_existing_odds_api_game_is_linked_not_duplicated(session, config, tmp_path) -> None:
    seed_reference(session, config)
    session.commit()
    league = session.scalar(select(League).where(League.key == "nfl"))
    kc = session.scalar(select(Team).where(Team.key == "KC"))
    bal = session.scalar(select(Team).where(Team.key == "BAL"))
    from datetime import UTC, datetime

    pre = Game(
        league_id=league.id,
        home_team_id=kc.id,
        away_team_id=bal.id,
        event_time=datetime(2024, 9, 6, 0, 20, tzinfo=UTC) + timedelta(minutes=5),
    )
    session.add(pre)
    session.commit()
    s = ingest_nfl_schedules(session, _fetch(), LocalArchive(tmp_path), min_season=1999)
    assert s.games_linked == 1 and s.games_created == 6
    ext = session.scalar(select(ExternalId).where(ExternalId.external_id == "2024_01_BAL_KC"))
    assert ext is not None and ext.entity_id == pre.id
