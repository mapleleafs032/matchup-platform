# Data-source inventory

Source of truth for status is `src/edge/sources/registry.py`
(`edge sources list`). Only IMPLEMENTED sources may be used by jobs.

| Key | Provider | Status | Verified | Notes |
| --- | --- | --- | --- | --- |
| `odds_api` | The Odds API v4 | Implemented | 2026-10-03 against the v4 guide | Featured markets only. Decimal odds requested (American can carry rounding errors). Market-level `last_update` used; bookmaker-level is deprecated. Up to 10 bookmakers by key = 1 region of cost. Historical snapshots (from 2020-06-06) not yet implemented. |
| `nflverse` | nflverse nfldata `games.csv` | Implemented (schedules, results, closing lines) | 2026-10-05 against the schedules dictionary and live file | `spread_line` positive = home favored (converted to book convention). Kickoff times are Eastern. Line source undocumented. QB/temp/wind are post-game facts and excluded. No license file: personal use only. Play-by-play not yet integrated. |
| `vsin_splits` | VSiN public splits page | Blocked | No | Terms of use and robots.txt must be reviewed before any automated access. DraftKings customers only; never described as market-wide. |
| `nws_weather` | National Weather Service | Incomplete | No | US only; verify terms, User-Agent rules, limits. |

## Critical fields (initial)

| Field | Preferred source | Fallback | Validation | Missing behavior | Refresh |
| --- | --- | --- | --- | --- | --- |
| Moneyline / spread / total prices | The Odds API | None yet | decimal in (1, 1000); two outcomes; spreads mirrored; totals equal | Book-market dropped and logged; game priced from remaining books | Adaptive: 60/15/5 min |
| Team identity | `external_ids` mapping | None | Exact provider name or id match | Run stops (`UnmappedEntityError`) | On seed |
| Starting QB (NFL) | Planned | - | - | Confirmation gate fails -> not official | Pre-game checkpoints |
