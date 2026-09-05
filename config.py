"""
Centralized configuration. Nothing tunable lives anywhere else.
Secrets are NOT here: they come from environment variables set by GitHub Actions secrets.
"""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TABLES = DATA / "tables"
RAW = DATA / "raw"
SNAPSHOTS = DATA / "snapshots"
SITE_JSON = ROOT / "site" / "json"

SEASON = 2026
LEAGUES = ("CFB", "NFL")
BACKTEST_SEASONS = [2021, 2022, 2023, 2024, 2025]   # post-COVID only; 2020 and earlier are hard-rejected
MIN_ALLOWED_SEASON = 2021

# ---- Provider keys (env only) -------------------------------------------------
CFBD_API_KEY = os.environ.get("CFBD_API_KEY", "").strip()
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# ---- Odds routing (decision #2: free now, paid is a config flip) --------------
ODDS_PLAN = os.environ.get("ODDS_PLAN", "free")          # "free" | "paid"
ODDS_ROUTING = {
    "free": {"NFL": "odds_api", "CFB": "cfbd"},
    "paid": {"NFL": "odds_api", "CFB": "odds_api"},
}[ODDS_PLAN]
ODDS_API_REGIONS = "us"
ODDS_API_MARKETS = "h2h,spreads,totals"                  # 3 credits per call on The Odds API
ODDS_API_SPORT_KEYS = {"NFL": "americanfootball_nfl", "CFB": "americanfootball_ncaaf"}
ODDS_BOOK_PRIORITY = ["consensus", "draftkings", "fanduel", "betmgm", "caesars", "espnbet", "bovada"]

# ---- API budgets (§50). Hard stops; jobs skip rather than exceed. ------------
API_BUDGET = {
    "cfbd":     {"monthly": 1000 if os.environ.get("CFBD_TIER", "free") == "free" else 5000, "daily_soft": 60},
    "odds_api": {"monthly": 500 if ODDS_PLAN == "free" else 20000, "daily_soft": 25},
    "open_meteo": {"monthly": 200000, "daily_soft": 5000},
    "nflverse": {"monthly": 10**9, "daily_soft": 10**9},
    "espn":     {"monthly": 5000, "daily_soft": 200},
}
BUDGET_DEGRADE_AT_PCT_REMAINING = 0.15   # below this, odds cadence halves automatically

# ---- Refresh cadences (hours). Game-day cadences handled by Apps Script timer. -
CADENCE = {
    "schedules_hours": 24,
    "odds_midweek_per_day": {"free": {"NFL": 2, "CFB": 2}, "paid": {"NFL": 6, "CFB": 6}}[ODDS_PLAN],
    "odds_gameday_hours_before_kick": {"free": 6, "paid": 8}[ODDS_PLAN],
}

# ---- Validation ranges (§44). Fractions 0..1 unless noted. -------------------
VALIDATION_RANGES = {
    "spread_home": (-60.0, 60.0),
    "total": (20.0, 110.0),
    "moneyline": (-10000, 10000),
    "points": (0, 120),
    "week_cfb": (0, 16),
    "week_nfl": (1, 22),
}

# ---- Season / week conventions -------------------------------------------------
NFL_GAME_TYPE_TO_SEASON_TYPE = {"REG": "REG", "WC": "POST", "DIV": "POST", "CON": "POST", "SB": "POST"}
CFBD_SEASON_TYPE = {"regular": "REG", "postseason": "POST", "both": None}
NFL_TIMEZONE = "America/New_York"   # nflverse gametime is Eastern

# ---- Game-level metric definitions (pipeline/metrics_game.py) ------------------
EXPLOSIVE_RUSH_YDS = 10
EXPLOSIVE_PASS_YDS = 20
# Garbage time. CFB: CFBD's published rule (lead >= threshold by period). NFL: pre-snap vegas win prob band.
CFB_GARBAGE_LEAD_BY_PERIOD = {2: 38, 3: 28, 4: 22}
NFL_GARBAGE_WP_BAND = (0.05, 0.95)
# Which upstream stats files to pull per league (nflverse release assets)
NFLVERSE_ASSETS = {
    "pbp": "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet",
    "ftn": "https://github.com/nflverse/nflverse-data/releases/download/ftn_charting/ftn_charting_{season}.parquet",
    "pfr_pass": "https://github.com/nflverse/nflverse-data/releases/download/pfr_advstats/advstats_week_pass_{season}.parquet",
    "pfr_def": "https://github.com/nflverse/nflverse-data/releases/download/pfr_advstats/advstats_week_def_{season}.parquet",
}
CFBD_STATS_CALLS_PER_WEEK = 5     # games/teams, stats/game/advanced, plays, drives, games/players

# ---- Storage --------------------------------------------------------------------
APPEND_ONLY_PATHS = [   # CI immutability check (Phase 3 §3) — relative to repo root
    "data/tables/market/snapshots",
    "data/tables/model/predictions",
    "data/tables/model/pregame_snapshots_index.csv",
    "data/tables/model/model_evaluation",
    "data/tables/results",
    "data/tables/roster/injuries",
    "data/tables/context/weather_snapshots",
    "data/tables/ops/job_log.csv",
    "data/tables/ops/validation_log.csv",
    "data/tables/ops/raw_responses.csv",
    "data/snapshots",
]

# Raw payload archiving: keyed/quota APIs are archived (small JSON, needed for rebuilds and disputes).
# nflverse bulk files are NOT archived (1 MB+ per pull, publicly versioned upstream); the index row still records the sha.
RAW_ARCHIVE_PROVIDERS = {"cfbd", "odds_api", "espn", "open_meteo"}

PIPELINE_VERSION = "ingest_v0.1"
