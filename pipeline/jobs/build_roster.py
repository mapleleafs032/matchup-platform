"""
python -m pipeline.jobs.build_roster --league NFL --season 2026 --what fetch derive
python -m pipeline.jobs.build_roster --league CFB --season 2026 --what fetch derive     # 8 CFBD calls (+1 usage in-season)
python -m pipeline.jobs.build_roster --league CFB --season 2025 --what fetch            # backfill inputs for 2026 returning production

fetch  -> player_season_usage (both), transfers/recruits/recruiting_classes/team_talent/draft_picks/returning (CFB)
derive -> returning_production (derived), departures, transfers (matched), talent_scores, qb_status, depth_charts (CFB projected), continuity
"""
from __future__ import annotations
import argparse
import glob
import sys

import pandas as pd

import config
from pipeline import ids, storage, roster_engine as eng
from pipeline.log import JobRun, ValidationLog
from providers import cfbd_roster, nflverse_roster
from providers.base import RequestManager, ProviderError

ROSTER = config.TABLES / "roster"
REF = config.TABLES / "ref"


def _latest_week_file(d, season) -> pd.DataFrame:
    files = sorted(glob.glob(str(d / str(season) / "*.parquet")))
    return pd.read_parquet(files[-1]) if files else pd.DataFrame()


def _merge(path, new, keys):
    cur = storage.read_table(path)
    if not new.empty:
        if not cur.empty:
            k = lambda df: df[keys].astype(str).agg("|".join, axis=1)
            cur = cur[~k(cur).isin(set(k(new)))]
            new = pd.concat([cur, new], ignore_index=True)
        storage.write_parquet(path, new)
    return len(new)


# ---- fetch ---------------------------------------------------------------------------------
def fetch_nfl(season: int, job: JobRun):
    rm = RequestManager("nflverse", job.job_run_id)
    resolver = ids.AliasResolver.load()
    pa = storage.read_table(REF / "player_aliases.parquet")
    snaps = nflverse_roster.fetch(rm, "snaps", season)
    pstats = nflverse_roster.fetch(rm, "pstats", season)
    pfr = nflverse_roster.fetch(rm, "pfr_def", season)
    usage = nflverse_roster.player_season_usage(snaps, pstats, pfr, season, resolver, pa)
    if not usage.empty:
        storage.write_parquet(ROSTER / "player_season_usage" / "NFL" / f"{season}.parquet", usage)
        print(f"NFL {season} player_season_usage: {len(usage)} rows")
    draft = nflverse_roster.fetch(rm, "draft")
    if draft is not None:
        d = nflverse_roster.draft_rows(draft, season, resolver)
        storage.write_parquet(ROSTER / "draft_picks" / "NFL" / f"{season}.parquet", d)
        print(f"NFL {season} draft picks: {len(d)}")
    job.api_calls += rm.calls_this_run


def fetch_cfb(season: int, week: int, job: JobRun, vlog: ValidationLog, usage_only: bool = False):
    rm = RequestManager("cfbd", job.job_run_id, enforce_daily=(job.trigger == "backfill"))
    resolver = ids.AliasResolver.load()
    unmatched: set[str] = set()
    res = cfbd_roster.fetch(rm, "/player/usage", season)
    usage = cfbd_roster.normalize_usage(res.payload, season, resolver, res.retrieved_at, unmatched)
    if usage_only:
        _merge(ROSTER / "player_season_usage" / "CFB" / f"{season}_usage_W{week:02d}.parquet", usage, ["player_id", "team_id"])
        print(f"CFB {season} usage as of W{week}: {len(usage)} players"); job.api_calls += rm.calls_this_run; return
    res = cfbd_roster.fetch(rm, "/stats/player/season", season)
    stats = cfbd_roster.normalize_player_season_stats(res.payload, season, resolver, res.retrieved_at, unmatched)
    merged = stats.merge(usage.drop(columns=["player_name", "position_raw", "position"], errors="ignore"), on=["player_id", "team_id", "season"], how="outer")
    if not merged.empty:
        storage.write_parquet(ROSTER / "player_season_usage" / "CFB" / f"{season}.parquet", merged)
        print(f"CFB {season} player_season_usage: {len(merged)} rows ({len(stats)} with stats, {len(usage)} with usage)")
    for endpoint, fn, path, keys in (
        ("/player/returning", cfbd_roster.normalize_returning, ROSTER / "returning_production" / "CFB" / f"{season}.parquet", ["team_id", "season", "as_of_week", "method"]),
        ("/player/portal", cfbd_roster.normalize_portal, ROSTER / "transfers" / f"{season}.parquet", ["transfer_id"]),
        ("/talent", cfbd_roster.normalize_talent, ROSTER / "team_talent.parquet", ["team_id", "season"]),
        ("/draft/picks", cfbd_roster.normalize_draft_picks, ROSTER / "draft_picks" / "CFB" / f"{season}.parquet", ["season", "overall"]),
    ):
        try:
            r = cfbd_roster.fetch(rm, endpoint, season)
        except ProviderError as e:
            vlog.warn("PROVIDER_FAIL", endpoint, "", str(e)[:100], "200"); continue
        df = fn(r.payload, season, resolver, r.retrieved_at, unmatched)
        n = _merge(path, df, keys)
        print(f"CFB {season} {endpoint}: {len(df)} rows")
    r1 = cfbd_roster.fetch(rm, "/recruiting/teams", season)
    teams_class = cfbd_roster.normalize_recruiting_teams(r1.payload, season, resolver, r1.retrieved_at, unmatched)
    r2 = cfbd_roster.fetch(rm, "/recruiting/players", season, classification="HighSchool")
    recruits = cfbd_roster.normalize_recruits(r2.payload, season, resolver, r2.retrieved_at, unmatched)
    storage.write_parquet(REF / "recruits" / f"{season}.parquet", recruits)
    classes = cfbd_roster.class_summary(recruits, teams_class, season)
    _merge(ROSTER / "recruiting_classes.parquet", classes, ["team_id", "season"])
    print(f"CFB {season} recruiting: {len(classes)} classes, {len(recruits)} recruits")
    for u in sorted(unmatched):
        vlog.warn("ALIAS_UNMATCHED", u, "team", u, "team_aliases row (non-FBS expected)")
    job.api_calls += rm.calls_this_run


# ---- derive ---------------------------------------------------------------------------------
def derive(league: str, season: int, week: int, job: JobRun):
    games = storage.read_table(storage.games_path(league, season))
    ros_dir = ROSTER / "roster_snapshots" / league
    roster_now = _latest_week_file(ros_dir, season)
    if roster_now.empty:
        job.status = "SKIPPED"; job.message = f"no roster snapshot for {league} {season}; run context rosters first"; return
    players = storage.read_table(REF / "players" / f"{league}.parquet")
    usage_prior = storage.read_table(ROSTER / "player_season_usage" / league / f"{season - 1}.parquet") if season - 1 >= config.MIN_ALLOWED_SEASON else pd.DataFrame()
    usage_cur = None
    cur_files = sorted(glob.glob(str(ROSTER / "player_season_usage" / league / f"{season}_usage_W*.parquet")))
    if cur_files:
        usage_cur = pd.read_parquet(cur_files[-1])
    draft = storage.read_table(ROSTER / "draft_picks" / league / f"{season}.parquet")
    portal = storage.read_table(ROSTER / "transfers" / f"{season}.parquet") if league == "CFB" else None
    recruits = storage.read_table(REF / "recruits" / f"{season}.parquet") if league == "CFB" else None
    coaches = storage.read_table(ROSTER / "coaches.csv")
    inj_path = ROSTER / "injuries" / league / f"{season}.csv"
    injuries = storage.read_table(inj_path)
    depth_nfl = _latest_week_file(ROSTER / "depth_charts" / "NFL", season) if league == "NFL" else None
    pgs = pd.concat([pd.read_parquet(p).assign(season=int(p.split("/")[-1][:4])) for p in glob.glob(str(config.TABLES / "stats" / "player_game_stats" / league / "*.parquet"))], ignore_index=True) if glob.glob(str(config.TABLES / "stats" / "player_game_stats" / league / "*.parquet")) else pd.DataFrame()
    if usage_prior.empty:
        print(f"{league} {season}: no prior-season usage table -> returning production from provider only (or unavailable)")

    rp = eng.returning_production(league, season, week, roster_now, usage_prior)
    if not rp.empty:
        _merge(ROSTER / "returning_production" / league / f"{season}.parquet", rp, ["team_id", "season", "as_of_week", "method"])
        print(f"{league} {season} returning production (derived): {len(rp)} teams, median rp_total={rp.rp_total.median():.2f}")
    dep = eng.departures(league, season, roster_now, usage_prior, draft, portal)
    if not dep.empty:
        storage.write_parquet(ROSTER / "departures" / league / f"{season}.parquet", dep)
        print(f"{league} {season} departures: {len(dep)} ({dep.category.value_counts().to_dict()})")
    tr = None
    if league == "CFB" and portal is not None and not portal.empty:
        tr = eng.evaluate_transfers(season, portal, roster_now, players, usage_prior)
        storage.write_parquet(ROSTER / "transfers" / f"{season}.parquet", tr)
        print(f"CFB {season} transfers matched to roster: {int(tr.player_id.notna().sum())} of {len(tr)}; roles {tr.projected_role.value_counts().to_dict()}")
        classes = storage.read_table(ROSTER / "recruiting_classes.parquet"); talent = storage.read_table(ROSTER / "team_talent.parquet")
        ts = eng.talent_scores(season, classes, talent, tr)
        if not ts.empty:
            _merge(ROSTER / "talent_scores.parquet", ts, ["team_id", "season"])
            print(f"CFB {season} talent scores: {len(ts)} teams")
    # QB status as of the earliest kickoff of `week`
    wk = games[(games.week == week) & (games.season_type == "REG") & games.kickoff_utc.notna()]
    cutoff = pd.to_datetime(wk.kickoff_utc, utc=True).min() if not wk.empty else pd.Timestamp.now(tz="UTC")
    team_ids = sorted(set(wk.home_team_id) | set(wk.away_team_id)) if not wk.empty else sorted(roster_now.team_id.unique())
    team_ids = [t for t in team_ids if not t.startswith("CFB_FCS")]
    qb = eng.qb_status(league, season, week, cutoff, team_ids, roster_now, players, pgs, usage_prior, injuries, depth_nfl, tr, recruits)
    if not qb.empty:
        storage.write_parquet(ROSTER / "qb_status" / league / str(season) / f"W{week:02d}.parquet", qb)
        print(f"{league} {season} W{week} QB status: {len(qb)} teams; basis {qb.projection_basis.value_counts().to_dict()}")
    if league == "CFB":
        dc = eng.project_depth_chart_cfb(season, week, roster_now, usage_prior, usage_cur, tr, recruits)
        if not dc.empty:
            storage.write_parquet(ROSTER / "depth_charts" / "CFB" / str(season) / f"W{week:02d}.parquet", dc)
            print(f"CFB {season} W{week} projected depth charts: {len(dc)} slots; basis {dc.projection_basis.value_counts().to_dict()}")
    cont = eng.continuity(league, season, rp, qb, coaches, tr)
    if not cont.empty:
        storage.write_parquet(ROSTER / "continuity" / league / f"{season}.parquet", cont)
        print(f"{league} {season} continuity index: median {cont.continuity_index.median():.2f}")
    job.rows_written += sum(len(x) for x in (rp, dep, qb, cont) if x is not None and not x.empty)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--week", type=int)
    p.add_argument("--what", nargs="+", default=["fetch", "derive"])
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    if a.season < config.MIN_ALLOWED_SEASON:
        sys.exit("season refused")
    games = storage.read_table(storage.games_path(a.league, a.season))
    week = a.week or (int(games[games.status == "SCHEDULED"].week.min()) if not games.empty and (games.status == "SCHEDULED").any() else 1)
    with JobRun(f"{a.league}_ROSTER", a.league, a.trigger) as job:
        vlog = ValidationLog(job.job_run_id, "roster")
        if "fetch" in a.what:
            (fetch_nfl if a.league == "NFL" else lambda s, j: fetch_cfb(s, week, j, vlog))(a.season, job)
        if "usage" in a.what and a.league == "CFB":
            fetch_cfb(a.season, week, job, vlog, usage_only=True)
        if "derive" in a.what:
            derive(a.league, a.season, week, job)
        vlog.flush()


if __name__ == "__main__":
    main()
