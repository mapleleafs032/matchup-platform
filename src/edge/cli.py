"""Command-line interface.

    edge config check            validate configuration; show which secrets are set
    edge sources list            provider catalog with honest integration status
    edge sources list-sports     verify sport keys against The Odds API (free call)
    edge db upgrade              apply database migrations
    edge db seed                 load reference data (idempotent)
    edge ingest odds --sport nfl fetch and store current odds for one sport

Manual runs go through the same validation as scheduled runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from alembic.util.exc import CommandError as AlembicError
from sqlalchemy.exc import SQLAlchemyError

from edge.core.config import get_app_config, get_secrets
from edge.core.errors import EdgeError
from edge.core.logging import configure_logging

if TYPE_CHECKING:
    from edge.sources.base import FetchResult
    from edge.sources.odds_api import OddsApiClient


def _cmd_config_check(_: argparse.Namespace) -> int:
    cfg = get_app_config()
    sec = get_secrets()
    print(
        json.dumps(
            {
                "config_fingerprint": cfg.fingerprint(),
                "rule_version": cfg.app.rule_version,
                "enabled_sports": [k for k, v in cfg.sports.items() if v.enabled],
                "environment": sec.environment,
                "database_url_set": sec.database_url is not None,
                "odds_api_key_set": sec.odds_api_key is not None,
                "market_informed_weight_fitted": cfg.decision.market_informed.model_weight
                is not None,
            },
            indent=2,
        )
    )
    return 0


def _cmd_sources_list(_: argparse.Namespace) -> int:
    from edge.sources.registry import PROVIDERS

    for key, info in PROVIDERS.items():
        print(f"{key:14} {info.status.value:12} verified={info.verified_on or 'no'}  {info.name}")
    return 0


def _odds_client() -> OddsApiClient:
    from edge.sources.odds_api import OddsApiClient

    key = get_secrets().odds_api_key
    return OddsApiClient(
        get_app_config().providers.odds_api, key.get_secret_value() if key else None
    )


def _cmd_list_sports(_: argparse.Namespace) -> int:
    client = _odds_client()
    try:
        result = client.list_sports()
    finally:
        client.close()
    for s in json.loads(result.body):
        print(f"{s.get('key'):40} active={s.get('active')}  {s.get('title')}")
    return 0


def _cmd_db_upgrade(_: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")
    print("database at head")
    return 0


def _cmd_db_seed(_: argparse.Namespace) -> int:
    from edge.ingest.seed import seed_reference
    from edge.store.db import make_engine, session_scope

    with session_scope(make_engine()) as s:
        print(json.dumps(seed_reference(s, get_app_config())))
    return 0


def _cmd_ingest_odds(args: argparse.Namespace) -> int:
    from sqlalchemy.orm import sessionmaker

    from edge.ingest.archive import LocalArchive
    from edge.ingest.odds import ingest_odds
    from edge.store.db import make_engine

    cfg = get_app_config()
    if args.sport not in cfg.sports or not cfg.sports[args.sport].enabled:
        print(f"sport {args.sport!r} is not enabled in config", file=sys.stderr)
        return 2
    client = _odds_client()
    session = sessionmaker(make_engine(), expire_on_commit=False)()
    try:
        summary = ingest_odds(
            session, client, LocalArchive(get_secrets().raw_archive_dir), cfg, args.sport
        )
    finally:
        session.close()
        client.close()
    print(json.dumps(asdict(summary), indent=2, default=str))
    return 0


def _nfl_fetch(args: argparse.Namespace) -> FetchResult:
    from edge.core.timeutil import utcnow
    from edge.sources.base import FetchResult
    from edge.sources.nflverse import PROVIDER, fetch_schedules

    if args.csv:
        path = Path(args.csv)
        return FetchResult(PROVIDER, str(path), {}, 200, path.read_bytes(), utcnow())
    return fetch_schedules()


def _cmd_ingest_nfl_schedules(args: argparse.Namespace) -> int:
    from sqlalchemy.orm import sessionmaker

    from edge.ingest.archive import LocalArchive
    from edge.ingest.nfl_schedules import ingest_nfl_schedules
    from edge.store.db import make_engine

    session = sessionmaker(make_engine(), expire_on_commit=False)()
    try:
        summary = ingest_nfl_schedules(
            session, _nfl_fetch(args), LocalArchive(get_secrets().raw_archive_dir)
        )
    finally:
        session.close()
    print(json.dumps(asdict(summary), indent=2, default=str))
    return 0


def _cmd_backtest_nfl(args: argparse.Namespace) -> int:
    from edge.backtest.nfl_walkforward import run
    from edge.sources.nflverse import normalize_schedules

    games = normalize_schedules(_nfl_fetch(args)).games
    report = run(games)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str))
    test = report["test"]
    print(
        json.dumps(
            {
                "model_version": report["model_version"],
                "report": str(out),
                "test_margin_mae_model_vs_market": [
                    test["margin"]["model_mae"],
                    test["margin"]["market_mae"],
                ],
                "test_moneyline_log_loss_model_vs_market": [
                    test["moneyline_vs_market_same_games"]["model_log_loss"],
                    test["moneyline_vs_market_same_games"]["market_log_loss"],
                ],
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="edge")
    sub = p.add_subparsers(dest="group", required=True)

    cfg = sub.add_parser("config").add_subparsers(dest="cmd", required=True)
    cfg.add_parser("check").set_defaults(func=_cmd_config_check)

    src = sub.add_parser("sources").add_subparsers(dest="cmd", required=True)
    src.add_parser("list").set_defaults(func=_cmd_sources_list)
    src.add_parser("list-sports").set_defaults(func=_cmd_list_sports)

    db = sub.add_parser("db").add_subparsers(dest="cmd", required=True)
    db.add_parser("upgrade").set_defaults(func=_cmd_db_upgrade)
    db.add_parser("seed").set_defaults(func=_cmd_db_seed)

    ing = sub.add_parser("ingest").add_subparsers(dest="cmd", required=True)
    odds = ing.add_parser("odds")
    odds.add_argument("--sport", required=True)
    odds.set_defaults(func=_cmd_ingest_odds)
    nfl = ing.add_parser("nfl-schedules", help="nflverse schedules and results")
    nfl.add_argument("--csv", help="use a local games.csv instead of downloading")
    nfl.set_defaults(func=_cmd_ingest_nfl_schedules)

    bt = sub.add_parser("backtest").add_subparsers(dest="cmd", required=True)
    btn = bt.add_parser("nfl", help="walk-forward backtest of the NFL baseline")
    btn.add_argument("--csv", help="use a local games.csv instead of downloading")
    btn.add_argument("--out", default="var/reports/nfl_baseline_backtest.json")
    btn.set_defaults(func=_cmd_backtest_nfl)
    return p


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except EdgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        # Short, secret-free message instead of a traceback. Full detail goes to the log.
        hint = ""
        if "does not exist" in str(exc.orig if hasattr(exc, "orig") else exc):
            hint = " (has `edge db upgrade` been run?)"
        print(f"database error: {type(exc).__name__}{hint}", file=sys.stderr)
        logging.getLogger("edge.cli").error("database error", exc_info=True)
        return 1
    except AlembicError as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
