"""
python -m pipeline.jobs.ingest_stats --league NFL --season 2025            # whole season from nflverse (1 pbp file)
python -m pipeline.jobs.ingest_stats --league NFL --season 2026 --weeks 1  # in-season: only completed weeks are rebuilt
python -m pipeline.jobs.ingest_stats --league CFB --season 2026 --weeks 1  # 5 CFBD calls per week

Outputs (Phase 3 layout, REBUILDABLE):
  data/tables/stats/plays/{league}/{season}/W{ww}.parquet
  data/tables/stats/drives/{league}/{season}.parquet
  data/tables/stats/team_game_stats/{league}/{season}.parquet
  data/tables/stats/team_game_advanced/{league}/{season}.parquet     (2 rows per team-game: garbage filtered / not)
  data/tables/stats/player_game_stats/{league}/{season}.parquet

Only games with status FINAL in our games table are processed; a game is never partially ingested.
CFB: metric engine computes every column from plays/drives, then CFBD's own advanced values overlay the
columns they cover (source recorded in `overlay_source`). NFL: everything computed from nflfastR + FTN + PFR.
"""
from __future__ import annotations
import argparse
import sys

import pandas as pd

import config
from pipeline import ids, storage, metrics_game
from pipeline.log import JobRun, ValidationLog
from providers import cfbd_stats, nflverse_stats
from providers.base import RequestManager, BudgetExceeded

STATS = config.TABLES / "stats"


def _merge_replace(path, new: pd.DataFrame, key_cols: list[str]) -> int:
    """Rebuildable tables: rows for the (game_ids) present in `new` are replaced, others kept."""
    if new.empty:
        return 0
    cur = storage.read_table(path)
    if not cur.empty:
        drop = new.game_id.unique()
        cur = cur[~cur.game_id.isin(drop)]
        new = pd.concat([cur, new], ignore_index=True)
    storage.write_parquet(path, new.drop_duplicates(key_cols, keep="last"))
    return len(new)


def _advanced_rows(plays: pd.DataFrame, drives: pd.DataFrame, games: pd.DataFrame, league: str, source: str) -> pd.DataFrame:
    rows = []
    for _, g in games.iterrows():
        if plays[plays.game_id == g.game_id].empty:
            continue
        for tid, opp in ((g.home_team_id, g.away_team_id), (g.away_team_id, g.home_team_id)):
            for gf in (False, True):
                rows.append(metrics_game.team_game_advanced(plays, drives, g.game_id, tid, opp, league, gf, source,
                                                            plays.retrieved_at.iloc[0], plays[plays.game_id == g.game_id].effective_at.iloc[0]))
    return pd.DataFrame(rows)


def _store_all(league, season, plays, drives, box, adv, qb, job):
    n = 0
    if not plays.empty:
        plays = plays.merge(storage.read_table(storage.games_path(league, season))[["game_id", "week"]], on="game_id", how="left")
        for wk, part in plays.groupby("week"):
            n += _merge_replace(STATS / "plays" / league / str(season) / f"W{int(wk):02d}.parquet", part.drop(columns="week"), ["play_id"])
    n += _merge_replace(STATS / "drives" / league / f"{season}.parquet", drives, ["drive_id"])
    n += _merge_replace(STATS / "team_game_stats" / league / f"{season}.parquet", box, ["game_id", "team_id"])
    n += _merge_replace(STATS / "team_game_advanced" / league / f"{season}.parquet", adv, ["game_id", "team_id", "is_garbage_filtered"])
    n += _merge_replace(STATS / "player_game_stats" / league / f"{season}.parquet", qb, ["game_id", "team_id", "player_id"])
    job.rows_written += n


# ---- NFL --------------------------------------------------------------------------
def run_nfl(season: int, weeks: list[int] | None, job: JobRun) -> None:
    rm = RequestManager("nflverse", job.job_run_id)
    resolver = ids.AliasResolver.load()
    games = storage.read_table(storage.games_path("NFL", season))
    final = games[games.status == "FINAL"]
    if weeks:
        final = final[final.week.isin(weeks)]
    if final.empty:
        job.status = "SKIPPED"; job.message = "no FINAL games to ingest"; return
    pbp = nflverse_stats.fetch_asset(rm, "pbp", season)
    if pbp is None:
        job.status = "SKIPPED"; job.message = f"nflverse pbp {season} not published yet"; return
    ftn = nflverse_stats.fetch_asset(rm, "ftn", season)
    pfr_pass = nflverse_stats.fetch_asset(rm, "pfr_pass", season)
    pfr_def = nflverse_stats.fetch_asset(rm, "pfr_def", season)
    plays, drives = nflverse_stats.normalize_plays(pbp, ftn, final, resolver, weeks)
    if plays.empty:
        job.status = "SKIPPED"; job.message = "pbp has no rows for requested games (not yet posted)"; return
    results = storage.read_table(config.TABLES / "results" / "NFL" / f"{season}.csv").set_index("game_id")
    box_rows = []
    for _, g in final.iterrows():
        if plays[plays.game_id == g.game_id].empty:
            continue
        r = results.loc[g.game_id] if g.game_id in results.index else None
        for tid, opp, home in ((g.home_team_id, g.away_team_id, True), (g.away_team_id, g.home_team_id, False)):
            pts = (int(r.home_score) if home else int(r.away_score)) if r is not None else None
            pa = (int(r.away_score) if home else int(r.home_score)) if r is not None else None
            row = metrics_game.team_game_box_from_plays(plays, drives, g.game_id, tid, opp, home, pts, pa)
            row.update({"source": "nflverse", "retrieved_at": plays.retrieved_at.iloc[0], "effective_at": plays[plays.game_id == g.game_id].effective_at.iloc[0]})
            box_rows.append(row)
    box = pd.DataFrame(box_rows)
    adv = _advanced_rows(plays, drives, final, "NFL", "nflverse")
    # PFR pressure overlay -> rates using dropbacks from box
    pr = nflverse_stats.team_pressure_rates(pfr_pass, pfr_def, final, resolver)
    if not pr.empty and not adv.empty:
        adv = adv.merge(pr, on=["game_id", "team_id"], how="left")
        db = box.set_index(["game_id", "team_id"]).dropbacks
        adv["off_pressure_rate_allowed"] = [ (p / db.get((g, t)) if pd.notna(p) and db.get((g, t)) else None) for g, t, p in zip(adv.game_id, adv.team_id, adv.get("off_pressures_allowed", pd.Series([None]*len(adv))))]
        opp_db = {(g, t): db.get((g, o)) for g, t, o in zip(adv.game_id, adv.team_id, adv.opponent_id)}
        adv["def_pressure_rate"] = [ (p / opp_db[(g, t)] if pd.notna(p) and opp_db[(g, t)] else None) for g, t, p in zip(adv.game_id, adv.team_id, adv.get("def_pressures", pd.Series([None]*len(adv))))]
        adv = adv.drop(columns=[c for c in ("off_pressures_allowed", "def_pressures") if c in adv.columns])
    qb = nflverse_stats.qb_game_stats(pbp, pfr_pass, final, resolver, weeks)
    _store_all("NFL", season, plays, drives, box, adv, qb, job)
    job.api_calls = rm.calls_this_run
    print(f"NFL {season}: games={plays.game_id.nunique()} plays={len(plays)} drives={len(drives)} box={len(box)} adv={len(adv)} qb={len(qb)}")


# ---- CFB --------------------------------------------------------------------------
def run_cfb(season: int, weeks: list[int], season_type: str, verify: bool, job: JobRun, force: bool = False) -> None:
    rm = RequestManager("cfbd", job.job_run_id)
    resolver = ids.AliasResolver.load()
    games = storage.read_table(storage.games_path("CFB", season))
    if games.empty or "week" not in games.columns:
        job.status = "SKIPPED"; job.message = f"no games table for CFB {season}; run ingest_schedules first"; return
    have_adv = storage.read_table(STATS / "team_game_advanced" / "CFB" / f"{season}.parquet")
    have_ids = set(have_adv.game_id) if not have_adv.empty else set()
    vlog = ValidationLog(job.job_run_id, "stats")
    for wk in weeks:
        wk_games = games[(games.week == wk) & (games.status == "FINAL") & (games.season_type == ("REG" if season_type == "regular" else "POST"))]
        if wk_games.empty:
            print(f"CFB {season} W{wk}: no FINAL games; skipping"); continue
        if not force and set(wk_games.game_id) <= have_ids:
            print(f"CFB {season} W{wk}: stats already ingested for all {len(wk_games)} games; skipping (use --force)"); continue
        if rm.remaining_monthly() < config.CFBD_STATS_CALLS_PER_WEEK + 20:
            job.status = "PARTIAL"; job.message += f" budget stop before week {wk};"; break
        missing: set[str] = set()
        try:
            r_plays = cfbd_stats.fetch_plays(rm, season, wk, season_type)
            r_drives = cfbd_stats.fetch_drives(rm, season, wk, season_type)
            r_box = cfbd_stats.fetch_team_box(rm, season, wk, season_type)
            r_adv = cfbd_stats.fetch_advanced(rm, season, wk, True, season_type)
            r_qb = cfbd_stats.fetch_player_box(rm, season, wk, season_type)
        except BudgetExceeded as e:
            job.status = "PARTIAL"; job.message += f" {e};"; break
        plays = cfbd_stats.normalize_plays(r_plays.payload, wk_games, resolver, r_plays.retrieved_at, missing)
        drives = cfbd_stats.normalize_drives(r_drives.payload, wk_games, resolver, r_drives.retrieved_at, missing)
        box = cfbd_stats.normalize_team_box(r_box.payload, wk_games, resolver, r_box.retrieved_at, missing)
        adv_native = cfbd_stats.normalize_advanced(r_adv.payload, wk_games, resolver, r_adv.retrieved_at, True, missing)
        qb = cfbd_stats.normalize_qb_box(r_qb.payload, wk_games, resolver, r_qb.retrieved_at, missing)
        if verify:
            for line in (cfbd_stats.schema_report("plays", r_plays.payload, missing) + cfbd_stats.schema_report("drives", r_drives.payload, set())
                         + cfbd_stats.schema_report("box", r_box.payload, set()) + cfbd_stats.schema_report("advanced", r_adv.payload, set())):
                print("VERIFY", line)
        # fill box fields that only plays can provide unambiguously
        if not plays.empty and not box.empty:
            sc = plays[plays.play_type.isin(["PASS", "RUSH", "SACK"])]
            for i, b in box.iterrows():
                o = sc[(sc.game_id == b.game_id) & (sc.offense_team_id == b.team_id)]
                box.loc[i, "plays"] = int(len(o))
                box.loc[i, "pass_int"] = int(((o.is_turnover == True) & (o.turnover_type == "INT")).sum())   # noqa: E712
                box.loc[i, "sacks_taken"] = int((o.is_sack == True).sum())                                    # noqa: E712
                box.loc[i, "dropbacks"] = int((o.is_dropback == True).sum())                                   # noqa: E712
                d = sc[(sc.game_id == b.game_id) & (sc.offense_team_id == b.opponent_id)]
                box.loc[i, "takeaways"] = int((d.is_turnover == True).sum())                                   # noqa: E712
        adv = _advanced_rows(plays, drives, wk_games, "CFB", "cfbd")
        # overlay CFBD-native advanced values on the garbage-filtered rows (that is what excludeGarbageTime=true returns)
        if not adv.empty and not adv_native.empty:
            adv = adv.set_index(["game_id", "team_id", "is_garbage_filtered"])
            nat = adv_native.set_index(["game_id", "team_id", "is_garbage_filtered"])
            cols = [c for c in nat.columns if c in adv.columns and c != "opponent_id"]
            nat = nat[cols].apply(pd.to_numeric, errors="coerce")
            cols = [c for c in cols if nat[c].notna().any()]      # a native column with no values never overlays
            common = nat.index.intersection(adv.index)
            for c in cols:
                adv[c] = adv[c].astype(float)
                adv.loc[common, c] = nat.loc[common, c].astype(float)
            adv = adv.reset_index()
            adv["overlay_source"] = "cfbd_advanced"
            adv["overlay_cols"] = ",".join(cols)
        for m in sorted(missing):
            vlog.warn("FIELD_MISSING", f"{season}_W{wk}", m, "", "present")
        _store_all("CFB", season, plays, drives, box, adv, qb, job)
        print(f"CFB {season} W{wk}: games={wk_games.shape[0]} plays={len(plays)} drives={len(drives)} box={len(box)} adv={len(adv)} qb={len(qb)} calls_left={rm.remaining_monthly()}")
    vlog.flush()
    job.api_calls = rm.calls_this_run


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--weeks", type=int, nargs="*")
    p.add_argument("--season-type", default="regular", choices=["regular", "postseason"])
    p.add_argument("--verify", action="store_true")
    p.add_argument("--force", action="store_true", help="re-ingest weeks that already have stats")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    if a.season < config.MIN_ALLOWED_SEASON:
        sys.exit(f"season {a.season} refused (MIN_ALLOWED_SEASON)")
    with JobRun(f"{a.league}_STATS", a.league, a.trigger) as job:
        if a.league == "NFL":
            run_nfl(a.season, a.weeks, job)
        else:
            if not a.weeks:
                games = storage.read_table(storage.games_path("CFB", a.season))
                done = games[games.status == "FINAL"].week.unique().tolist() if not games.empty else []
                a.weeks = sorted(done)[-2:]   # default: the two most recent completed weeks
            run_cfb(a.season, a.weeks, a.season_type, a.verify, job, a.force)


if __name__ == "__main__":
    main()
