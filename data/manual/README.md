# Manual tables

These CSVs are the only data you edit by hand (GitHub: open the file -> pencil -> edit -> Commit).
They are loaded by the daily context job and every row is badged "manual" in the app.

| file | what | required columns |
|---|---|---|
| coaches_manual.csv | offensive / defensive coordinators (no free source exists) | team_id, season, role (OC/DC), coach_name, effective_from (YYYY-MM-DD) |
| rivalries.csv | rivalry pairs used by the scheduling-context engine | league, team_a, team_b |
| injuries_cfb.csv | CFB injuries (no official report exists) | season, week, team_id, player_name, position, status, report_date |
| kickoff_overrides.csv | kickoff time corrections the provider missed | game_id, kickoff_utc (ISO, UTC) |
| nfl_venues.csv | NFL stadium coordinates for weather (nflverse has none) | maintained by the pipeline; edit only to fix a coordinate |

Rules: team_id must exist in data/tables/ref/teams.parquet (CFB_NEB, NFL_KC, ...). Rows that fail
validation are logged to validation_log and skipped; they never break the site. Delete the example rows.
