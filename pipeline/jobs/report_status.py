"""
python -m pipeline.jobs.report_status
Prints a plain-text status summary: table row counts, last job runs, validation rejects, API budget,
unmatched aliases. This is what you paste back for review after the first run.
"""
from __future__ import annotations
import glob

import pandas as pd

import config
from pipeline import storage


def _count(pattern: str) -> int:
    n = 0
    for p in glob.glob(str(config.TABLES / pattern), recursive=True):
        df = storage.read_table(__import__("pathlib").Path(p))
        n += len(df)
    return n


def main():
    print("=" * 70)
    print("TABLE COUNTS")
    for name, pat in [("teams", "ref/teams.parquet"), ("team_aliases", "ref/team_aliases.csv"),
                      ("games NFL", "games/NFL/*.parquet"), ("games CFB", "games/CFB/*.parquet"),
                      ("results NFL", "results/NFL/*.csv"), ("results CFB", "results/CFB/*.csv"),
                      ("closing_lines NFL", "market/closing_lines/NFL/*.parquet"),
                      ("market_snapshots NFL", "market/snapshots/NFL/**/*.csv"),
                      ("market_snapshots CFB", "market/snapshots/CFB/**/*.csv"),
                      ("plays NFL", "stats/plays/NFL/**/*.parquet"), ("plays CFB", "stats/plays/CFB/**/*.parquet"),
                      ("team_game_advanced NFL", "stats/team_game_advanced/NFL/*.parquet"),
                      ("team_game_advanced CFB", "stats/team_game_advanced/CFB/*.parquet"),
                      ("player_game_stats CFB", "stats/player_game_stats/CFB/*.parquet"),
                      ("players NFL", "ref/players/NFL.parquet"), ("players CFB", "ref/players/CFB.parquet"),
                      ("venues", "ref/venues.parquet"),
                      ("roster_snapshots NFL", "roster/roster_snapshots/NFL/**/*.parquet"), ("roster_snapshots CFB", "roster/roster_snapshots/CFB/**/*.parquet"),
                      ("depth_charts NFL", "roster/depth_charts/NFL/**/*.parquet"),
                      ("injuries NFL", "roster/injuries/NFL/*.csv"), ("injuries CFB", "roster/injuries/CFB/*.csv"),
                      ("coaches", "roster/coaches.csv"), ("rankings CFB", "context/rankings/*.parquet"),
                      ("weather_snapshots", "context/weather_snapshots/**/*.csv"),
                      ("team_metrics_asof NFL", "analytics/team_metrics_asof/NFL/**/*.parquet"),
                      ("team_metrics_asof CFB", "analytics/team_metrics_asof/CFB/**/*.parquet"),
                      ("team_ratings", "analytics/team_ratings/**/*.parquet"),
                      ("player_season_usage NFL", "roster/player_season_usage/NFL/*.parquet"), ("player_season_usage CFB", "roster/player_season_usage/CFB/*.parquet"),
                      ("returning_production", "roster/returning_production/**/*.parquet"), ("transfers CFB", "roster/transfers/*.parquet"),
                      ("qb_status", "roster/qb_status/**/*.parquet"), ("continuity", "roster/continuity/**/*.parquet"),
                      ("matchup_edges", "analytics/matchup_edges/**/*.parquet")]:
        print(f"  {name:24s} {_count(pat):>7d}")
    teams = storage.read_table(config.TABLES / "ref" / "teams.parquet")
    if not teams.empty:
        print(f"  CFB FBS teams seeded     {int((teams.league == 'CFB').sum()):>7d}")
    print("\nLAST JOB RUNS")
    jl = storage.read_table(config.TABLES / "ops" / "job_log.csv")
    if not jl.empty:
        print(jl.sort_values("started_at").tail(8)[["job_name", "status", "rows_written", "api_calls", "message"]].to_string(index=False))
    print("\nVALIDATION (last 20)")
    vl = storage.read_table(config.TABLES / "ops" / "validation_log.csv")
    if vl.empty:
        print("  none")
    else:
        print(vl.tail(20)[["table_name", "rule", "severity", "record_key", "field", "observed"]].to_string(index=False))
        print(f"  totals: {(vl.severity == 'REJECT').sum()} REJECT, {(vl.severity == 'WARN').sum()} WARN")
    print("\nAPI BUDGET (this month)")
    b = storage.read_table(config.TABLES / "ops" / "api_budget.csv")
    if not b.empty:
        month = pd.Timestamp.now('UTC').strftime("%Y-%m")
        m = b[b.day.astype(str).str.startswith(month)].groupby("provider").agg(requests=("requests", "sum"), credits=("credits", "sum"), failures=("failures", "sum"), remaining_reported=("remaining_reported", "last"))
        for prov, r in m.iterrows():
            lim = config.API_BUDGET.get(prov, {}).get("monthly")
            print(f"  {prov:12s} calls={int(r.requests):4d} credits={int(r.credits):5d}/{lim}  failures={int(r.failures)}  provider_reports_remaining={r.remaining_reported}")
    print("\nMATCHUP EDGES (latest week per league; prelim weighted advantage, home perspective, points)")
    for lg in ("NFL", "CFB"):
        files = sorted(glob.glob(str(config.TABLES / f"analytics/matchup_edges/{lg}/*/W*.parquet")))
        if files:
            e = pd.read_parquet(files[-1]); g = e.drop_duplicates("game_id")[["game_id", "prelim_margin_home"]].sort_values("prelim_margin_home")
            un = e[e.is_unavailable].category.value_counts().to_dict()
            print(f"  {lg} {files[-1].split('/')[-2]} {files[-1].split('/')[-1][:-8]}: {len(g)} games | unavailable categories: {un}")
            print("   " + " | ".join(f"{r.game_id.split('_',3)[3]} {r.prelim_margin_home:+.1f}" for _, r in pd.concat([g.head(3), g.tail(3)]).iterrows()))
    print("\nROSTER ENGINE (latest season per league)")
    for lg in ("NFL", "CFB"):
        for p in sorted(glob.glob(str(config.TABLES / f"roster/continuity/{lg}/*.parquet")))[-1:]:
            c = pd.read_parquet(p)
            print(f"  {lg} {p.split('/')[-1][:-8]}: continuity median={c.continuity_index.median():.2f}  lowest: " + ", ".join(f"{t}={v:.2f}" for t, v in c.sort_values('continuity_index').head(4)[['team_id','continuity_index']].values))
        qbs = sorted(glob.glob(str(config.TABLES / f"roster/qb_status/{lg}/*/*.parquet")))
        if qbs:
            q = pd.read_parquet(qbs[-1])
            print(f"  {lg} QB status {qbs[-1].split('/')[-2]} {qbs[-1].split('/')[-1][:-8]}: basis {q.projection_basis.value_counts().to_dict()} | flags {q['flags'].value_counts().head(4).to_dict()}")
    print("\nTEAM RATINGS (latest week per league, top 8 by overall)")
    for lg in ("NFL", "CFB"):
        for p in sorted(glob.glob(str(config.TABLES / f"analytics/team_ratings/{lg}/*.parquet")))[-1:]:
            r = pd.read_parquet(p); r = r[r.as_of_week == r.as_of_week.max()]
            print(f"  {lg} season {p.split('/')[-1][:-8]} as of week {int(r.as_of_week.iloc[0])}  HFA={r.hfa_league.iloc[0]:.2f}")
            print(r.sort_values("rating_overall", ascending=False)[["team_id", "rating_overall", "rating_off", "rating_def", "sos", "sos_rank", "games_in_fit"]].head(8).round(2).to_string(index=False))
    print("\nWEATHER sample (latest rows)")
    for p in sorted(glob.glob(str(config.TABLES / "context/weather_snapshots/**/*.csv"), recursive=True))[-1:]:
        w = pd.read_csv(p)
        print(w[["game_id", "hours_to_kickoff", "is_indoor", "temp_f", "wind_mph", "wind_gust_mph", "precip_prob"]].tail(6).to_string(index=False))
    print("\nCFB advanced sample (garbage-filtered rows; overlay_source = cfbd_advanced when native values applied)")
    for p in sorted(glob.glob(str(config.TABLES / "stats/team_game_advanced/CFB/*.parquet")))[-1:]:
        a = pd.read_parquet(p)
        a = a[a.is_garbage_filtered == True]
        cols = [c for c in ["game_id", "team_id", "off_plays", "off_ppa_play", "off_success_rate", "off_explosiveness", "off_line_yards", "def_havoc", "off_pts_per_scoring_opp", "off_sec_per_play", "overlay_source"] if c in a.columns]
        print(a[cols].tail(6).round(3).to_string(index=False))
    print("\nCFB market snapshot sample (sign check: negative spread_home = home favored)")
    for p in sorted(glob.glob(str(config.TABLES / "market/snapshots/CFB/**/*.csv"), recursive=True))[-1:]:
        s = pd.read_csv(p)
        print(s[["game_id", "book", "spread_home", "provider_open_spread_home", "total", "ml_home", "ml_away", "is_first_snapshot"]].head(8).to_string(index=False))
    print("=" * 70)


if __name__ == "__main__":
    main()
