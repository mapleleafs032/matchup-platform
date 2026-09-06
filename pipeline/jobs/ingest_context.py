"""
python -m pipeline.jobs.ingest_context --league NFL --what rosters injuries qbr
python -m pipeline.jobs.ingest_context --league CFB --what rosters rankings coaches venues
python -m pipeline.jobs.ingest_context --league BOTH --what weather manual        (weather needs venues first)
python -m pipeline.jobs.ingest_context --league BOTH --what all

Storage (Phase 3 layout):
  ref/players/{league}.parquet, ref/player_aliases.parquet, ref/venues.parquet         REBUILDABLE
  roster/roster_snapshots/{league}/{season}/W{ww}.parquet, roster/depth_charts/...     APPEND by week (rewritten per week)
  roster/injuries/{league}/{season}.csv                                                 APPEND-ONLY
  roster/coaches.csv (provider + manual rows)                                          APPEND-ONLY
  context/rankings/{season}.parquet                                                    REBUILDABLE per week
  context/weather_snapshots/{league}/{season}/W{ww}.csv                                APPEND-ONLY
  ops/manual_lists.csv, ops/kickoff_overrides.csv                                      from data/manual/
"""
from __future__ import annotations
import argparse
import glob
import sys
from datetime import datetime, timezone

import pandas as pd

import config
from pipeline import ids, storage
from pipeline.log import JobRun, ValidationLog
from providers import cfbd_context, nflverse_context, open_meteo
from providers.base import RequestManager, BudgetExceeded, ProviderError

REF = config.TABLES / "ref"
ROSTER = config.TABLES / "roster"
CONTEXT = config.TABLES / "context"
MANUAL = config.DATA / "manual"


def _now():
    return datetime.now(timezone.utc)


def _merge_by_key(path, new: pd.DataFrame, keys: list[str]):
    cur = storage.read_table(path)
    if not cur.empty and not new.empty:
        k_new = new[keys].astype(str).agg("|".join, axis=1)
        k_cur = cur[keys].astype(str).agg("|".join, axis=1)
        cur = cur[~k_cur.isin(set(k_new))]
        new = pd.concat([cur, new], ignore_index=True)
    if not new.empty:
        storage.write_parquet(path, new)
    return len(new)


def _current_week(games: pd.DataFrame) -> int:
    sched = games[(games.status == "SCHEDULED") & games.kickoff_utc.notna()]
    return int(sched.week.min()) if not sched.empty else int(games.week.max())


# ---- NFL --------------------------------------------------------------------------
def nfl(what: set[str], season: int, job: JobRun):
    rm = RequestManager("nflverse", job.job_run_id)
    resolver = ids.AliasResolver.load()
    games = storage.read_table(storage.games_path("NFL", season))
    if "players" in what or "rosters" in what or "qbr" in what:
        raw = nflverse_context.fetch_asset(rm, "players")
        players, aliases = nflverse_context.normalize_players(raw)
        storage.write_parquet(REF / "players" / "NFL.parquet", players)
        cur = storage.read_table(REF / "player_aliases.parquet")
        cur = cur[~cur.player_id.str.startswith("NFL_P_")] if not cur.empty else cur
        storage.write_parquet(REF / "player_aliases.parquet", pd.concat([cur, aliases], ignore_index=True))
        job.rows_written += len(players)
        print(f"NFL players: {len(players)}")
    if "rosters" in what:
        raw = nflverse_context.fetch_asset(rm, "rosters", season)
        if raw is not None:
            prior_files = sorted(glob.glob(str(ROSTER / "roster_snapshots" / "NFL" / str(season - 1) / "*.parquet")))
            prior = pd.read_parquet(prior_files[-1]) if prior_files else None
            ros = nflverse_context.normalize_rosters(raw, season, resolver, prior)
            for wk, part in ros.groupby("week"):
                storage.write_parquet(ROSTER / "roster_snapshots" / "NFL" / str(season) / f"W{int(wk):02d}.parquet", part)
            job.rows_written += len(ros)
            print(f"NFL rosters: {len(ros)} rows, weeks {sorted(ros.week.unique().tolist())}")
        raw = nflverse_context.fetch_asset(rm, "depth", season)
        if raw is not None and not games.empty:
            dc = nflverse_context.normalize_depth_charts(raw, season, games, resolver)
            for wk, part in dc.groupby("week"):
                storage.write_parquet(ROSTER / "depth_charts" / "NFL" / str(season) / f"W{int(wk):02d}.parquet", part)
            job.rows_written += len(dc)
            print(f"NFL depth charts: {len(dc)} rows, weeks {sorted(dc.week.unique().tolist())}")
    if "injuries" in what:
        raw = nflverse_context.fetch_asset(rm, "injuries", season)
        if raw is None:
            print(f"NFL injuries {season}: not published yet")
        else:
            inj = nflverse_context.normalize_injuries(raw, season, games, resolver)
            n = storage.append_csv(ROSTER / "injuries" / "NFL" / f"{season}.csv", inj, ["injury_row_id"], on_duplicate="skip")
            job.rows_written += n
            print(f"NFL injuries: {n} new rows ({len(inj)} in file)")
    if "qbr" in what:
        raw = nflverse_context.fetch_asset(rm, "qbr")
        pa = storage.read_table(REF / "player_aliases.parquet")
        q = nflverse_context.normalize_qbr(raw, season, games, pa)
        path = config.TABLES / "stats" / "player_game_stats" / "NFL" / f"{season}.parquet"
        pgs = storage.read_table(path)
        if not pgs.empty and not q.empty:
            pgs = pgs.drop(columns=["qbr"]).merge(q, on=["game_id", "player_id"], how="left")
            storage.write_parquet(path, pgs)
            print(f"NFL QBR: {int(pgs.qbr.notna().sum())} of {len(pgs)} QB game rows now carry QBR")
    job.api_calls = rm.calls_this_run


# ---- CFB --------------------------------------------------------------------------
def cfb(what: set[str], season: int, job: JobRun, vlog: ValidationLog):
    rm = RequestManager("cfbd", job.job_run_id)
    resolver = ids.AliasResolver.load()
    games = storage.read_table(storage.games_path("CFB", season))
    week = _current_week(games) if not games.empty else 1
    unmatched: set[str] = set()
    if "rosters" in what:
        try:
            res = cfbd_context.fetch_roster(rm, season)
            payloads = [res.payload]
            ts = res.retrieved_at
        except ProviderError as e:
            if "400" not in str(e):
                raise
            teams = storage.read_table(REF / "teams.parquet"); teams = teams[teams.league == "CFB"]
            payloads, ts = [], _now()
            for _, t in teams.iterrows():
                payloads.append(cfbd_context.fetch_roster(rm, season, t.school_or_city).payload)
        prior_files = sorted(glob.glob(str(ROSTER / "roster_snapshots" / "CFB" / str(season - 1) / "*.parquet")))
        prior = pd.read_parquet(prior_files[-1]) if prior_files else None
        ros_all, pl_all = [], []
        for pl in payloads:
            ros, players = cfbd_context.normalize_roster(pl, season, week, resolver, ts, prior, unmatched)
            ros_all.append(ros); pl_all.append(players)
        ros = pd.concat(ros_all, ignore_index=True); players = pd.concat(pl_all, ignore_index=True)
        storage.write_parquet(ROSTER / "roster_snapshots" / "CFB" / str(season) / f"W{week:02d}.parquet", ros)
        _merge_by_key(REF / "players" / "CFB.parquet", players.drop_duplicates("player_id"), ["player_id"])
        job.rows_written += len(ros)
        print(f"CFB rosters: {len(ros)} players on {ros.team_id.nunique()} teams as of week {week}")
    if "rankings" in what:
        res = cfbd_context.fetch_rankings(rm, season, week)
        rk = cfbd_context.normalize_rankings(res.payload, resolver, res.retrieved_at, unmatched)
        if not rk.empty:
            _merge_by_key(CONTEXT / "rankings" / f"{season}.parquet", rk, ["season", "week", "poll", "team_id"])
            job.rows_written += len(rk)
        print(f"CFB rankings week {week}: {len(rk)} rows, polls {sorted(rk.poll.unique().tolist()) if not rk.empty else []}")
    if "coaches" in what:
        res = cfbd_context.fetch_coaches(rm, season)
        co = cfbd_context.normalize_coaches(res.payload, season, resolver, res.retrieved_at, unmatched)
        if not co.empty:
            co["coach_row_id"] = co.team_id + "_" + co.season.astype(str) + "_" + co.role + "_" + co.coach_id
            n = storage.append_csv(ROSTER / "coaches.csv", co, ["coach_row_id"], on_duplicate="skip")
            job.rows_written += n
            print(f"CFB head coaches {season}: {n} new rows; {int(co.needs_manual_dates.sum())} teams with mid-season change need manual dates")
    if "venues" in what:
        res = cfbd_context.fetch_venues(rm)
        v = cfbd_context.normalize_venues(res.payload, res.retrieved_at)
        _merge_by_key(REF / "venues.parquet", v, ["venue_id"])
        job.rows_written += len(v)
        print(f"CFB venues: {len(v)}")
    for u in sorted(unmatched):
        vlog.warn("ALIAS_UNMATCHED", u, "team", u, "team_aliases row (non-FBS teams expected)")
    job.api_calls = rm.calls_this_run


# ---- shared -------------------------------------------------------------------------
def load_manual(job: JobRun, vlog: ValidationLog):
    teams = storage.read_table(REF / "teams.parquet")
    known = set(teams.team_id) if not teams.empty else set()
    # NFL venues (static)
    nv = pd.read_csv(MANUAL / "nfl_venues.csv")
    nv["retrieved_at"] = _now().isoformat()
    for c in ("elevation_m", "capacity", "surface"):
        nv[c] = None
    _merge_by_key(REF / "venues.parquet", nv, ["venue_id"])
    # coordinators
    co = pd.read_csv(MANUAL / "coaches_manual.csv")
    co = co[co.coach_name.notna() & (co.coach_name.astype(str).str.strip() != "")]
    rows = []
    for _, r in co.iterrows():
        if r.team_id not in known:
            vlog.reject("IDENTITY", f"coaches_manual:{r.team_id}", "team_id", r.team_id, "known team_id"); continue
        if r.role not in ("OC", "DC", "HC"):
            vlog.reject("RANGE", f"coaches_manual:{r.team_id}", "role", r.role, "OC|DC|HC"); continue
        cid = str(r.coach_name).lower().replace(" ", "_").replace(".", "")
        rows.append({"team_id": r.team_id, "season": int(r.season), "role": r.role, "coach_name": r.coach_name, "coach_id": cid,
                     "effective_from": r.effective_from, "effective_to": r.effective_to if pd.notna(r.effective_to) else None,
                     "is_first_season_in_role": None, "source": "manual", "entered_by": r.entered_by, "retrieved_at": _now().isoformat(),
                     "coach_row_id": f"{r.team_id}_{int(r.season)}_{r.role}_{cid}"})
    if rows:
        job.rows_written += storage.append_csv(ROSTER / "coaches.csv", pd.DataFrame(rows), ["coach_row_id"], on_duplicate="skip")
    # rivalries
    rv = pd.read_csv(MANUAL / "rivalries.csv")
    rows = []
    for _, r in rv.iterrows():
        if r.team_a not in known or r.team_b not in known:
            vlog.warn("IDENTITY", f"rivalries:{r.team_a}-{r.team_b}", "team", f"{r.team_a},{r.team_b}", "known team_ids"); continue
        rows.append({"list_name": "RIVALRY", "league": r.league, "key_a": r.team_a, "key_b": r.team_b, "value": r.get("name"),
                     "entered_by": r.entered_by, "entered_at": _now().isoformat()})
    if rows:
        out = pd.DataFrame(rows)
        storage.write_parquet(config.TABLES / "ops" / "manual_lists.parquet", out)
        print(f"rivalries loaded: {len(out)}")
    # CFB injuries
    inj = pd.read_csv(MANUAL / "injuries_cfb.csv")
    inj = inj[inj.player_name.notna() & (inj.player_name.astype(str).str.strip() != "")]
    rows = []
    for _, r in inj.iterrows():
        if r.team_id not in known:
            vlog.reject("IDENTITY", f"injuries_cfb:{r.team_id}", "team_id", r.team_id, "known team_id"); continue
        if r.status not in ("OUT", "DOUBTFUL", "QUESTIONABLE", "PROBABLE", "IR"):
            vlog.reject("RANGE", f"injuries_cfb:{r.player_name}", "status", r.status, "OUT|DOUBTFUL|QUESTIONABLE|PROBABLE|IR"); continue
        rows.append({"injury_row_id": f"{r.team_id}_{str(r.player_name).replace(' ', '')}_{int(r.season)}W{int(r.week):02d}_{r.status}_{r.report_date}",
                     "league": "CFB", "season": int(r.season), "week": int(r.week), "game_id": None, "team_id": r.team_id,
                     "player_id": None, "player_name": r.player_name, "position": r.position, "depth_slot": None, "status": r.status,
                     "practice_status": None, "injury_desc": r.injury_desc if pd.notna(r.injury_desc) else None,
                     "report_date": r.report_date, "source": "manual", "entered_by": r.entered_by, "retrieved_at": _now().isoformat(),
                     "effective_at": pd.Timestamp(r.report_date).isoformat()})
    if rows:
        job.rows_written += storage.append_csv(ROSTER / "injuries" / "CFB" / f"{config.SEASON}.csv", pd.DataFrame(rows), ["injury_row_id"], on_duplicate="skip")
    # kickoff overrides
    ko = pd.read_csv(MANUAL / "kickoff_overrides.csv")
    if not ko.empty:
        ko["entered_at"] = _now().isoformat()
        storage.append_csv(config.TABLES / "ops" / "kickoff_overrides.csv", ko, ["game_id", "kickoff_utc"], on_duplicate="skip")


def weather(leagues: list[str], season: int, job: JobRun, vlog: ValidationLog):
    rm = RequestManager("open_meteo", job.job_run_id)
    venues = storage.read_table(REF / "venues.parquet")
    if venues.empty:
        job.message += " no venues table; run --what venues manual first;"; return
    vidx = venues.set_index("venue_id")
    now = pd.Timestamp(_now())
    horizon = now + pd.Timedelta(days=15)
    total = 0
    cache: dict[str, dict] = {}
    for league in leagues:
        games = storage.read_table(storage.games_path(league, season))
        if games.empty:
            continue
        up = games[(games.status == "SCHEDULED") & games.kickoff_utc.notna()].copy()
        up["k"] = pd.to_datetime(up.kickoff_utc, utc=True)
        up = up[(up.k >= now - pd.Timedelta(hours=4)) & (up.k <= horizon)]
        rows = []
        for _, g in up.iterrows():
            if pd.isna(g.venue_id) or g.venue_id not in vidx.index:
                vlog.warn("VENUE_MISSING", g.game_id, "venue_id", g.venue_id, "venues row"); continue
            v = vidx.loc[g.venue_id]
            roof = g.venue_roof if isinstance(g.get("venue_roof"), str) else v.roof
            if roof in open_meteo.INDOOR_ROOFS:
                rows.append(open_meteo.snapshot_row(g.game_id, g.k, roof, None, _now())); continue
            if pd.isna(v.latitude) or pd.isna(v.longitude):
                vlog.warn("VENUE_NO_COORDS", g.game_id, "latitude", None, "coordinates"); continue
            key = f"{round(float(v.latitude), 2)},{round(float(v.longitude), 2)}"
            if key not in cache:
                try:
                    cache[key] = rm.get(open_meteo.FORECAST, params={"latitude": v.latitude, "longitude": v.longitude, "hourly": open_meteo.HOURLY,
                                                                    "forecast_days": 16, **open_meteo.UNITS}).payload
                except (ProviderError, BudgetExceeded) as e:
                    vlog.warn("PROVIDER_FAIL", g.game_id, "open_meteo", str(e)[:80], "200"); cache[key] = None
            payload = cache[key]
            vals = open_meteo.pick_hour(payload, g.k) if payload else None
            rows.append(open_meteo.snapshot_row(g.game_id, g.k, roof, vals, _now()))
        if rows:
            df = pd.DataFrame(rows).merge(games[["game_id", "week"]], on="game_id")
            for wk, part in df.groupby("week"):
                total += storage.append_csv(CONTEXT / "weather_snapshots" / league / str(season) / f"W{int(wk):02d}.csv",
                                            part.drop(columns="week"), ["game_id", "retrieved_at"], on_duplicate="skip")
    job.rows_written += total
    job.api_calls += rm.calls_this_run
    print(f"weather: {total} snapshot rows, {rm.calls_this_run} Open-Meteo calls")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=["NFL", "CFB", "BOTH"])
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--what", nargs="+", default=["all"])
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    what = set(a.what)
    if "all" in what:
        what = {"players", "rosters", "injuries", "qbr", "rankings", "coaches", "venues", "manual", "weather"}
    leagues = ["NFL", "CFB"] if a.league == "BOTH" else [a.league]
    with JobRun("CONTEXT", a.league, a.trigger) as job:
        vlog = ValidationLog(job.job_run_id, "context")
        if "manual" in what:
            load_manual(job, vlog)
        if "NFL" in leagues and what & {"players", "rosters", "injuries", "qbr"}:
            nfl(what, a.season, job)
        if "CFB" in leagues and what & {"rosters", "rankings", "coaches", "venues"}:
            cfb(what, a.season, job, vlog)
        if "weather" in what:
            weather(leagues, a.season, job, vlog)
        vlog.flush()
        if vlog.rejects:
            job.status = "PARTIAL"; job.message += f" {vlog.rejects} manual rows rejected;"


if __name__ == "__main__":
    main()
