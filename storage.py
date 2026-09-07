"""
python -m pipeline.jobs.ingest_schedules --league NFL --season 2026
python -m pipeline.jobs.ingest_schedules --league CFB --season 2026 --weeks 1 2 3 4 5
python -m pipeline.jobs.ingest_schedules --league NFL --season 2021   (backfill; also writes results + closing lines)

Steps per provider (Phase 4 contract): Connect -> Fetch -> Normalize -> Validate -> Store -> Log -> Test
"""
from __future__ import annotations
import argparse
import sys

import pandas as pd

import config
from pipeline import ids, storage, validate
from pipeline.log import JobRun, ValidationLog
from providers import cfbd, nflverse
from providers.base import RequestManager, BudgetExceeded, ProviderError

TEAMS_PATH = config.TABLES / "ref" / "teams.parquet"
RESULTS_DIR = config.TABLES / "results"
CLOSING_DIR = config.TABLES / "market" / "closing_lines"


def _merge_teams(new: pd.DataFrame) -> None:
    cur = storage.read_table(TEAMS_PATH)
    if not cur.empty:
        cur = cur[~cur.team_id.isin(new.team_id)]
    storage.write_parquet(TEAMS_PATH, pd.concat([cur, new], ignore_index=True).sort_values("team_id"))


def seed_nfl(rm: RequestManager, resolver: ids.AliasResolver) -> int:
    raw = nflverse.fetch_teams(rm)
    teams, aliases = nflverse.normalize_teams(raw)
    _merge_teams(teams)
    resolver.add(aliases)
    resolver.save()
    return len(teams)


def seed_cfb(rm: RequestManager, resolver: ids.AliasResolver, season: int, job: JobRun) -> int:
    res = cfbd.fetch_teams_fbs(rm, season)
    teams, aliases, warnings = cfbd.normalize_teams(res.payload, res.retrieved_at)
    for w in warnings:
        print("WARN", w)
    job.message += " ".join(warnings)[:300]
    _merge_teams(teams)
    resolver.add(aliases)
    resolver.save()
    return len(teams)


def run_nfl(season: int, job: JobRun) -> None:
    rm = RequestManager("nflverse", job.job_run_id)
    resolver = ids.AliasResolver.load()
    if resolver.aliases[resolver.aliases.provider == "nflverse"].empty:
        n = seed_nfl(rm, resolver)
        print(f"seeded {n} NFL teams")
    raw = nflverse.fetch_schedules(rm)
    games, closes = nflverse.normalize_schedules(raw, season, resolver)
    vlog = ValidationLog(job.job_run_id, "games")
    games = validate.validate_games(games, "NFL", season, vlog)
    vlog.flush()
    if vlog.rejects:
        job.status = "PARTIAL"
        job.message += f" {vlog.rejects} game rows rejected;"
    r = storage.upsert_games("NFL", season, games)
    for w in r["warnings"]:
        print("WARN", w)
    job.rows_written += r["inserted"] + r["updated"]
    if not closes.empty:
        storage.write_parquet(CLOSING_DIR / "NFL" / f"{season}.parquet", closes)
        job.rows_written += len(closes)
    results = nflverse.normalize_results(raw, season, resolver)
    if not results.empty:
        n = storage.append_csv(RESULTS_DIR / "NFL" / f"{season}.csv", results, key_cols=["game_id"], on_duplicate="skip")
        job.rows_written += n
    job.api_calls = rm.calls_this_run
    print(f"NFL {season}: games inserted={r['inserted']} updated={r['updated']} closes={len(closes)} results={len(results)}")


def run_cfb(season: int, weeks: list[int], season_type: str, job: JobRun, force: bool = False) -> None:
    rm = RequestManager("cfbd", job.job_run_id, enforce_daily=(job.trigger == "backfill"))
    resolver = ids.AliasResolver.load()
    if resolver.aliases[resolver.aliases.provider == "cfbd"].empty:
        n = seed_cfb(rm, resolver, season, job)
        print(f"seeded {n} FBS teams")
    vlog = ValidationLog(job.job_run_id, "games")
    total_ins = total_upd = 0
    existing = storage.read_table(storage.games_path("CFB", season))
    st_code = "REG" if season_type == "regular" else "POST"
    for wk in weeks:
        if not force and not existing.empty and "week" in existing.columns:
            have = existing[(existing.week == wk) & (existing.season_type == st_code)]
            if len(have) and (have.status == "FINAL").all() and season < config.SEASON:
                print(f"CFB {season} W{wk}: already loaded and final; skipping (use --force to refetch)")
                continue
        try:
            res = cfbd.fetch_games(rm, season, wk, season_type)
        except BudgetExceeded as e:
            job.status = "PARTIAL"; job.message += f" budget stop at week {wk}: {e};"
            break
        if wk == weeks[0] and "--verify" in sys.argv:
            for line in cfbd.verify_first_pull(res.payload, []):
                print("VERIFY", line)
        try:
            games = cfbd.normalize_games(res.payload, season, resolver, res.retrieved_at)
        except ids.UnmatchedAlias as e:
            for u in resolver.unmatched:
                vlog.reject("ALIAS_UNMATCHED", f"{season}_W{wk}", "team", u["alias"], "team_aliases row")
            vlog.flush()
            raise
        games = validate.validate_games(games, "CFB", season, vlog)
        if games.empty:
            print(f"CFB {season} W{wk} ({season_type}): provider returned no games"); continue
        r = storage.upsert_games("CFB", season, games)
        for w in r["warnings"]:
            print("WARN", w)
        total_ins += r["inserted"]; total_upd += r["updated"]
        # results for completed games
        done = [g for g in res.payload if g.get("completed") and g.get("homePoints") is not None]
        if done:
            gid_map = {int(x["provider_game_ids"].split(":")[1].strip("}")): x["game_id"] for x in games.to_dict("records")}
            rows = []
            for g in done:
                gid = gid_map.get(g["id"])
                if gid:
                    rows.append({"game_id": gid, "away_score": int(g["awayPoints"]), "home_score": int(g["homePoints"]),
                                 "margin_home": int(g["homePoints"] - g["awayPoints"]), "total": int(g["homePoints"] + g["awayPoints"]),
                                 "went_overtime": None, "q_scores": None, "attendance": g.get("attendance"),
                                 "source": "cfbd", "retrieved_at": res.retrieved_at.isoformat(), "effective_at": g.get("startDate")})
            if rows:
                job.rows_written += storage.append_csv(RESULTS_DIR / "CFB" / f"{season}.csv", pd.DataFrame(rows), ["game_id"], "skip")
    vlog.flush()
    if vlog.rejects:
        job.status = "PARTIAL"; job.message += f" {vlog.rejects} rows rejected;"
    job.rows_written += total_ins + total_upd
    job.api_calls = rm.calls_this_run
    print(f"CFB {season} weeks {weeks}: inserted={total_ins} updated={total_upd} calls={rm.calls_this_run} remaining_month={rm.remaining_monthly()}")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--weeks", type=int, nargs="*", help="CFB only; default = current week +/- 1")
    p.add_argument("--season-type", default="regular", choices=["regular", "postseason"])
    p.add_argument("--trigger", default="manual")
    p.add_argument("--verify", action="store_true", help="print first-pull field checklist (CFB)")
    p.add_argument("--force", action="store_true", help="refetch weeks that are already loaded and final")
    a = p.parse_args(argv)
    if a.season < config.MIN_ALLOWED_SEASON:
        sys.exit(f"season {a.season} is before MIN_ALLOWED_SEASON {config.MIN_ALLOWED_SEASON}; refusing")
    with JobRun(f"{a.league}_SCHEDULES", a.league, a.trigger) as job:
        if a.league == "NFL":
            run_nfl(a.season, job)
        else:
            weeks = a.weeks or list(range(1, 16))   # CFBD has no week 0 (late-August games are its week 1)
            run_cfb(a.season, weeks, a.season_type, job, a.force)


if __name__ == "__main__":
    main()
