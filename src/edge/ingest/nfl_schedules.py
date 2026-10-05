"""NFL schedule and results ingestion from nflverse.

Purpose: keep `games` (reference) and `game_results` (append-only) current.
Rules:
- A game already created by another source (e.g. The Odds API) is LINKED, not
  duplicated: same league, same home/away franchise, kickoff within 12 hours.
- Results are inserted once. A changed score becomes a new row that
  supersedes the old one; nothing is overwritten.
- Point-in-time: when the fetch happens more than 2 days after the result was
  first available, the row is a backfill (`is_backfill`), and `available_at`
  is the conservative `result_available_at` estimate from the adapter.
  Otherwise `available_at` is our observation time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from edge.core.errors import UnmappedEntityError
from edge.core.logging import get_logger, log_event
from edge.core.timeutil import utcnow
from edge.ingest.archive import RawArchive
from edge.sources.base import FetchResult
from edge.sources.nflverse import PROVIDER, NflGameRecord, normalize_schedules
from edge.store.models import ExternalId, Game, GameResult, IngestRun, League, RawObject, Team

log = get_logger(__name__)
_MATCH_WINDOW = timedelta(hours=12)
_BACKFILL_AFTER = timedelta(days=2)


@dataclass(slots=True)
class ScheduleIngestSummary:
    run_id: int
    games_seen: int = 0
    games_created: int = 0
    games_linked: int = 0
    results_written: int = 0
    results_superseded: int = 0
    issues: list[str] = field(default_factory=list)


def ingest_nfl_schedules(
    session: Session,
    result: FetchResult,
    archive: RawArchive,
    *,
    min_season: int = 2004,
) -> ScheduleIngestSummary:
    run = IngestRun(job="nfl_schedules", source=PROVIDER, started_at=utcnow())
    session.add(run)
    session.commit()
    summary = ScheduleIngestSummary(run_id=run.id)
    try:
        report = normalize_schedules(result)
        summary.issues = list(report.issues)
        league = session.scalar(select(League).where(League.key == "nfl"))
        if league is None:
            raise UnmappedEntityError("league 'nfl' not seeded; run `edge db seed`")
        teams = {
            t.key: t.id for t in session.scalars(select(Team).where(Team.league_id == league.id))
        }
        raw = session.scalar(select(RawObject).where(RawObject.sha256 == result.sha256))
        if raw is None:
            raw = RawObject(
                provider=PROVIDER,
                endpoint=result.endpoint,
                params=result.params,
                status_code=result.status_code,
                sha256=result.sha256,
                storage_key=archive.put(result),
                size_bytes=len(result.body),
                fetched_at=result.fetched_at,
            )
            session.add(raw)
            session.flush()
        for g in report.games:
            if g.season < min_season:
                continue
            summary.games_seen += 1
            game = _match_or_create(session, g, league.id, teams, summary)
            if g.completed:
                _write_result(session, game.id, g, result, summary)
        run.status = "ok_with_issues" if summary.issues else "ok"
        run.rows_written = summary.results_written
        run.issues = summary.issues
        run.finished_at = utcnow()
        session.commit()
        log_event(
            log,
            logging.INFO,
            "nfl schedules ingested",
            run_id=run.id,
            created=summary.games_created,
            linked=summary.games_linked,
            results=summary.results_written,
        )
        return summary
    except Exception as exc:
        session.rollback()
        failed = session.get(IngestRun, run.id)
        if failed is not None:
            failed.status, failed.error, failed.finished_at = "failed", repr(exc)[:2000], utcnow()
            session.commit()
        raise


def _team(teams: dict[str, int], key: str) -> int:
    if key not in teams:
        raise UnmappedEntityError(f"team {key!r} not seeded")
    return teams[key]


def _match_or_create(
    session: Session,
    g: NflGameRecord,
    league_id: int,
    teams: dict[str, int],
    s: ScheduleIngestSummary,
) -> Game:
    gid = session.scalar(
        select(ExternalId.entity_id).where(
            ExternalId.entity_type == "game",
            ExternalId.source == PROVIDER,
            ExternalId.external_id == g.game_id,
        )
    )
    if gid is not None:
        game = session.get(Game, gid)
        assert game is not None
        if g.kickoff_time_known and game.event_time != g.kickoff:
            game.event_time = g.kickoff  # rescheduled / flexed
        return game
    home, away = _team(teams, g.home), _team(teams, g.away)
    candidates = list(
        session.scalars(
            select(Game).where(
                Game.league_id == league_id,
                Game.home_team_id == home,
                Game.away_team_id == away,
                Game.event_time >= g.kickoff - _MATCH_WINDOW,
                Game.event_time <= g.kickoff + _MATCH_WINDOW,
            )
        )
    )
    if len(candidates) > 1:
        raise UnmappedEntityError(f"{g.game_id}: {len(candidates)} existing games match")
    if candidates:
        game = candidates[0]
        game.neutral_site = g.neutral
        s.games_linked += 1
    else:
        game = Game(
            league_id=league_id,
            event_time=g.kickoff,
            home_team_id=home,
            away_team_id=away,
            neutral_site=g.neutral,
        )
        session.add(game)
        session.flush()
        s.games_created += 1
    session.add(
        ExternalId(entity_type="game", entity_id=game.id, source=PROVIDER, external_id=g.game_id)
    )
    return game


def _write_result(
    session: Session, game_id: int, g: NflGameRecord, fetch: FetchResult, s: ScheduleIngestSummary
) -> None:
    assert g.home_score is not None and g.away_score is not None
    latest = session.scalar(
        select(GameResult)
        .where(GameResult.game_id == game_id)
        .order_by(GameResult.id.desc())
        .limit(1)
    )
    if latest is not None and (latest.home_score, latest.away_score) == (
        g.home_score,
        g.away_score,
    ):
        return
    backfill = fetch.fetched_at - g.result_available_at > _BACKFILL_AFTER
    session.add(
        GameResult(
            game_id=game_id,
            home_score=g.home_score,
            away_score=g.away_score,
            ended_in="overtime" if g.overtime else "regulation",
            supersedes_id=latest.id if latest is not None else None,
            published_at=None,
            observed_at=fetch.fetched_at,
            available_at=g.result_available_at if backfill else fetch.fetched_at,
            is_backfill=backfill,
            source=PROVIDER,
        )
    )
    s.results_written += 1
    if latest is not None:
        s.results_superseded += 1
        s.issues.append(
            f"{g.game_id}: score changed {latest.home_score}-{latest.away_score}"
            f" -> {g.home_score}-{g.away_score}; superseding row written"
        )
