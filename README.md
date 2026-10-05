# sports-edge

A personal, non-commercial sports prediction and betting-market analysis
platform. It estimates outcome probabilities independently of the market,
compares them with sportsbook prices, and publishes zero to three official
plays a day, plus leans, with a full record of why every game did or did not
qualify.

> **No guarantees.** Nothing here has been validated on historical data yet.
> Every betting threshold in `config/settings.yaml` is provisional. Model
> probabilities are estimates, not promises, and past results are not evidence
> of future performance.

## Status (2026-10-05)

| Area | Status |
| --- | --- |
| Configuration, logging, error types | Implemented, tested |
| Odds math, market view, decision engine, daily card | Implemented, tested |
| The Odds API adapter (current odds, featured markets) | Implemented, tested with mocked responses; not yet run against the live API |
| Postgres schema, append-only protection, point-in-time reads | Implemented, tested on Postgres 16 |
| Odds ingestion job, CLI | Implemented, tested |
| nflverse schedules/results adapter and DB ingestion | Implemented, tested on the live file |
| NFL baseline ratings model + walk-forward backtest | Implemented; **no edge vs the market** (see `docs/results/`) |
| Play-level features, QB availability, grading/CLV | Planned |
| Weather, splits sources | Planned / blocked (see `edge sources list`) |
| Odds recorder Worker, read API, frontend | Planned |

## Quick start

Requires Python 3.12 and Docker (for local Postgres).

```bash
git clone <your-repo-url> sports-edge && cd sports-edge
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.lock && pip install -e . --no-deps
cp .env.example .env            # then fill in EDGE_ODDS_API_KEY when you have one
docker compose up -d            # local Postgres 16

edge config check               # validates config; shows which secrets are set
edge db upgrade                 # create the schema
edge db seed                    # sports, leagues, books, NFL teams (safe to re-run)
edge sources list               # provider catalog with honest status
edge sources list-sports        # free call: verifies sport keys (needs API key)
edge ingest odds --sport nfl    # one odds snapshot (costs credits)
edge ingest nfl-schedules       # nflverse games and results (free)
edge backtest nfl               # walk-forward backtest of the NFL baseline (~20 s)
```

## Tests

```bash
pytest -q                       # unit tests always; Postgres tests need TEST_DATABASE_URL
ruff check src tests migrations && mypy
```

Integration tests create and drop their own databases on the server in
`TEST_DATABASE_URL`, so point it at a local or CI server, never production.

## Layout

```text
config/settings.yaml   all non-secret settings (sports, books, thresholds, refresh)
src/edge/core          config, logging, time, errors
src/edge/sources       provider adapters + registry (only IMPLEMENTED ones are usable)
src/edge/markets       odds math, canonical quote types, market view
src/edge/quality       per-game data-quality status
src/edge/predict       prediction contract (models plug in here)
src/edge/decision      decision engine and daily card
src/edge/ingest        jobs: archive, seed, odds ingestion
src/edge/store         database models, sessions, point-in-time reader
migrations/            Alembic migrations (0001 adds append-only triggers)
data/seeds             reference data
docs/                  architecture, data sources, decisions, session reports
```

## Documentation

- `docs/architecture.md` - module contracts (purpose, inputs, outputs, failure behavior, tests)
- `docs/data-sources.md` - provider inventory and verification status
- `docs/decisions/` - architecture decision records
- `docs/troubleshooting.md` - common problems
- `docs/session-reports/` - what each development session did, with real test results
