# CFB + NFL Matchup Analysis Platform

Data-accuracy-first matchup research platform. Architecture: Python jobs in GitHub Actions → Parquet/CSV
tables in this repo (DuckDB for queries) → prebuilt JSON → static site on GitHub Pages.

Phase status: **Phase 4 in progress** — schedules, results, closing lines (NFL 2021–2026 loaded), odds snapshots.

## Layout
```
config.py                  every tunable + budgets; secrets via env only
providers/                 one adapter per source (nflverse, cfbd, odds_api); all HTTP via providers/base.py
pipeline/                  ids, storage (append-only guard), validate, log, ci_immutability
pipeline/jobs/             ingest_schedules, ingest_odds, report_status
data/tables/               the database (see schema.sql / phase3_data_model.md)
data/raw/                  archived keyed-API payloads (sha256-named, gzip)
.github/workflows/         ci, schedules (daily), odds (2/day + dispatch), first_run
apps_script/OddsTimer.gs   hourly game-day dispatcher
```

## Local use
```
pip install -r requirements.txt
pytest -q                                   # includes one live nflverse test (no key)
python -m pipeline.jobs.ingest_schedules --league NFL
CFBD_API_KEY=... python -m pipeline.jobs.ingest_schedules --league CFB --weeks 1 2 --verify
python -m pipeline.jobs.report_status
duckdb -c "select * from read_parquet('data/tables/games/NFL/2026.parquet') limit 5"
```

## Rules enforced by code
* NULL means unavailable; never 0, never a guess.
* Every row: `source`, `retrieved_at`, `effective_at`.
* Unknown team names halt the job (`ALIAS_UNMATCHED` in validation_log) — nothing is fuzzy-matched.
* Append-only paths (`config.APPEND_ONLY_PATHS`) are checked by CI; existing lines cannot change.
* Spread convention: `spread_home` negative = home favored. nflverse's `spread_line` is sign-flipped on ingest;
  CFBD's sign is cross-checked against `formattedSpread` and mismatches are rejected.
* Seasons before 2021 are refused at the ID layer.
