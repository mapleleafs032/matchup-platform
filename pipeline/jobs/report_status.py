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
                      ("market_snapshots CFB", "market/snapshots/CFB/**/*.csv")]:
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
    print("\nCFB market snapshot sample (sign check: negative spread_home = home favored)")
    for p in sorted(glob.glob(str(config.TABLES / "market/snapshots/CFB/**/*.csv"), recursive=True))[-1:]:
        s = pd.read_csv(p)
        print(s[["game_id", "book", "spread_home", "provider_open_spread_home", "total", "ml_home", "ml_away", "is_first_snapshot"]].head(8).to_string(index=False))
    print("=" * 70)


if __name__ == "__main__":
    main()
