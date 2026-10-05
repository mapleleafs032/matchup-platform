# Architecture and module contracts

Batch-first modular monolith: scheduled Python jobs ingest data, build
point-in-time features, predict, decide and publish; a read-only frontend
consumes precomputed results. See the approved architecture proposal for the
full design. This file documents each module as implemented.

Status key: **Implemented** (code + passing tests) - **Planned** - **Blocked**.

| Module | Status | Purpose | Inputs | Outputs | Depends on | Failure behavior | Tests |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `core.config` | Implemented | One typed settings object | `config/settings.yaml`, `EDGE_*` env vars | `AppConfig`, `Secrets`, config fingerprint | pydantic | Invalid/missing config raises `ConfigError` at startup | `test_config_logging_quality.py` |
| `core.logging` | Implemented | JSON logs with secret redaction | log records | one JSON line per event | - | Redaction runs on every record | same |
| `core.timeutil` | Implemented | UTC handling | timestamps | aware UTC datetimes | - | Naive timestamps raise `ValueError` | `test_engine.py` |
| `markets.odds` | Implemented | All price math | decimal/American odds, probabilities | break-even, no-vig, EV, edge, acceptable price | - | Invalid inputs raise `ValueError`; nothing clamped | `test_odds.py` |
| `markets.consensus` | Implemented | Market view for one line | latest quotes, time, config | consensus no-vig, best eligible price, stale flags, movement | odds | < min books: consensus `None`; mixed lines raise | `test_consensus.py` |
| `quality.status` | Implemented | Per-game data status | input checks | `QualityReport` | - | Priority: conflicting > unavailable > partial > stale > complete | `test_config_logging_quality.py` |
| `predict.contracts` | Implemented | Standard prediction object | model output | `Prediction`, `SideProbability` | - | Invalid probabilities or cutoff after creation raise | same |
| `decision.engine` | Implemented | Prediction vs market -> decision | prediction, market view, quality, confirmations, validation status, opening consensus, splits, config | `DecisionRecord` with every gate | markets, quality, predict | Missing inputs fail a gate with a reason; never raise | `test_engine.py` |
| `decision.card` | Implemented | Daily card | decision records | officials (<= cap), leans, no-plays, summary | engine | Returns "No qualifying plays." when none qualify | `test_engine.py` |
| `sources.base` / `registry` | Implemented | Adapter interface, provider catalog | - | `ProviderInfo`, `FetchResult` | - | Non-IMPLEMENTED sources are unusable | `test_odds_api.py` |
| `sources.odds_api` | Implemented | The Odds API v4 current odds | sport key, markets | raw bytes; canonical events/quotes | httpx | Auth: no retry. 429/5xx/timeouts: bounded backoff. Low credits: stop. Bad body: `ValidationFailure`. Bad book-market: dropped + issue | `test_odds_api.py` |
| `ingest.archive` | Implemented (local) | Keep raw bytes | `FetchResult` | content-addressed gzip file | - | Atomic write; R2 version Planned | integration |
| `ingest.seed` | Implemented | Reference data | `data/seeds/*.yaml`, config | rows (idempotent) | store | Non-string seed values raise | integration |
| `ingest.odds` | Implemented | One odds snapshot into the DB | client, archive, config, sport | ingest run, raw object, games, markets, changed quotes | sources, store | Run marked failed (committed) and error re-raised; unmapped team stops run | integration |
| `store.models` + migration 0001 | Implemented | Canonical schema | - | 28 tables; 15 append-only via triggers | SQLAlchemy, Alembic | UPDATE/DELETE/TRUNCATE on append-only tables raise | integration |
| `store.pit` | Implemented | Point-in-time reads | select, cutoff | rows with `available_at <= cutoff` | store | Non-observation table raises `TypeError` | integration |
| `cli` | Implemented | Manual operations | arguments | JSON / text | all | Clean one-line errors; exit code 1 | smoke-tested |
| `sources.nflverse` | Implemented | NFL schedules, results, closing lines | games.csv | `NflGameRecord` (book-convention handicap, UTC kickoff, conservative availability) | httpx | Bad columns/ids/teams/scores raise `ValidationFailure`; implausible lines dropped + issue | `test_nflverse.py` |
| `ingest.nfl_schedules` | Implemented | Games + results into the DB | `FetchResult` | games (linked across sources), append-only results with supersession | store | Run audited; ambiguous match or unknown team stops run | integration |
| `models.nfl_ratings` | Implemented (baseline, shadow) | Margin/total projections and probabilities | past results, cutoff | `Prediction` with intervals | numpy | Abstains on thin history | `test_nfl_ratings.py` (incl. leakage replay) |
| `backtest.metrics`, `backtest.nfl_walkforward` | Implemented | Locked-split walk-forward evaluation | normalized games | JSON report | numpy | - | `test_metrics.py`; real-data run |
| Play-level features (EPA), key-number margins, QB availability | Planned | - | - | - | - | - | - |
| Grading of live decisions, CLV | Planned | - | - | - | - | - | - |
| Odds recorder Worker, read API, frontend | Planned | - | - | - | - | - | - |
| Monitoring / alerts | Planned | - | - | - | - | - | - |

## Timestamps on every observation

| Column | Meaning |
| --- | --- |
| `event_time` (games) | When the game starts |
| `published_at` | When the source says the data was published or last seen |
| `observed_at` | When our job received it |
| `available_at` | When a prediction may use it: `observed_at` live; `published_at` for backfills (flagged `is_backfill`, an approximation) |
| `ingested_at` | Database insert time |
| `modified_at` | Reference tables only |

## Decision classifications

- **Official play:** every gate passes, then the daily card caps apply.
- **Lean:** data quality and market sanity pass, priced, quote fresh, and the
  pure model shows >= `lean.min_edge` and >= `lean.min_ev`, but at least one
  official gate fails. Leans carry no stake and are graded separately.
- **No-play:** everything else, with every failing gate recorded.
