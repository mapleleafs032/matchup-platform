"""
nflverse play-by-play / charting adapter (NFL). Extends providers/nflverse.py (schedules).

Sources (GitHub release assets, free):
  pbp/play_by_play_{season}.parquet            nflfastR play-by-play, 372 cols, revised in-season
  ftn_charting/ftn_charting_{season}.parquet   FTN charting 2022+, joined on nflverse_play_id (CC-BY-SA, attribute in UI)
  pfr_advstats/advstats_week_pass_{season}     per QB per week: times_pressured, blitzed, hurried, hit (PFR)
  pfr_advstats/advstats_week_def_{season}      per defender per week: def_pressures, def_sacks, ...

Normalization decisions:
  * Slim play table follows Phase 3 `plays`. play_type: pass->PASS (sack->SACK), run->RUSH, qb_kneel->KNEEL,
    qb_spike->SPIKE, no_play->PENALTY, kickoff/punt/field_goal/extra_point kept by name.
  * Scrambles are play_type=RUSH with is_dropback=True (nflfastR qb_scramble). Metric engine treats them as dropbacks.
  * garbage time: pre-snap vegas_wp outside config.NFL_GARBAGE_WP_BAND.
  * success: nflfastR `success` (EPA > 0).
  * drives: nflfastR fixed_drive; points = posteam_score_post at last play of the drive minus posteam_score at first play.
    Defensive TDs against the offense are not credited to the offense (points can be negative -> clipped to 0).
  * effective_at for every row = the game's kickoff date (+4h) so the as-of builder can use it.
"""
from __future__ import annotations
import io
from datetime import timedelta

import numpy as np
import pandas as pd

import config
from pipeline import ids
from providers.base import RequestManager

_PLAY_TYPE = {"pass": "PASS", "run": "RUSH", "qb_kneel": "KNEEL", "qb_spike": "SPIKE", "no_play": "PENALTY",
              "kickoff": "KICKOFF", "punt": "PUNT", "field_goal": "FG", "extra_point": "XP"}
_DRIVE_RESULT = {"Touchdown": "TD", "Field goal": "FG", "Missed field goal": "MISSED_FG", "Punt": "PUNT",
                 "Turnover": "TURNOVER", "Turnover on downs": "DOWNS", "End of half": "END_HALF",
                 "Opp touchdown": "TURNOVER", "Safety": "SAFETY"}


def fetch_asset(rm: RequestManager, asset: str, season: int) -> pd.DataFrame | None:
    """Returns None (not an error) when the season file does not exist yet (e.g., before Week 1)."""
    from providers.base import ProviderError
    url = config.NFLVERSE_ASSETS[asset].format(season=season)
    try:
        res = rm.get(url, raw_bytes=True, timeout=120)
    except ProviderError as e:
        if "404" in str(e):
            return None
        raise
    df = pd.read_parquet(io.BytesIO(res.payload))
    df.attrs["raw_id"] = res.raw_id
    df.attrs["retrieved_at"] = res.retrieved_at
    return df


def _game_map(games: pd.DataFrame) -> dict[str, dict]:
    """nflverse game_id -> our game row (via provider_game_ids json)."""
    out = {}
    for _, g in games.iterrows():
        try:
            nv = str(g.provider_game_ids).split('"nflverse":"')[1].split('"')[0]
        except IndexError:
            continue
        out[nv] = g
    return out


def normalize_plays(pbp: pd.DataFrame, ftn: pd.DataFrame | None, games: pd.DataFrame, resolver: ids.AliasResolver,
                    weeks: list[int] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (plays_slim, drives). Only games present in our games table are kept."""
    gm = _game_map(games)
    p = pbp[pbp.game_id.isin(gm.keys())].copy()
    if weeks is not None:
        p = p[p.week.isin(weeks)]
    p = p[p.posteam.notna() & p.play_type.notna()]
    if ftn is not None and not ftn.empty:
        f = ftn[["nflverse_game_id", "nflverse_play_id", "is_play_action", "is_rpo", "n_blitzers", "n_pass_rushers",
                 "is_interception_worthy", "qb_location", "is_motion", "is_no_huddle", "is_screen_pass"]].rename(
            columns={"nflverse_game_id": "game_id", "nflverse_play_id": "play_id"})
        p = p.merge(f, on=["game_id", "play_id"], how="left")
    else:
        for c in ("is_play_action", "is_rpo", "n_blitzers", "n_pass_rushers", "is_interception_worthy"):
            p[c] = np.nan
    retrieved_at = pbp.attrs["retrieved_at"].isoformat()
    lo, hi = config.NFL_GARBAGE_WP_BAND

    def team(abbr):
        return resolver.resolve("nflverse", alias=abbr)

    rows = []
    for _, r in p.iterrows():
        g = gm[r.game_id]
        off = team(r.posteam); deff = team(r.defteam)
        pt = _PLAY_TYPE.get(r.play_type, "OTHER")
        is_sack = bool(r.sack == 1)
        if pt == "PASS" and is_sack:
            pt = "SACK"
        is_scramble = bool(r.qb_scramble == 1)
        turnover_type = "INT" if r.interception == 1 else ("FUM" if r.fumble_lost == 1 else None)
        rows.append({
            "play_id": f"{g.game_id}_{int(r.play_id)}", "game_id": g.game_id,
            "offense_team_id": off, "defense_team_id": deff,
            "period": int(r.qtr) if pd.notna(r.qtr) else None,
            "clock_sec_remaining": int(r.quarter_seconds_remaining) if pd.notna(r.quarter_seconds_remaining) else None,
            "game_sec_remaining": int(r.game_seconds_remaining) if pd.notna(r.game_seconds_remaining) else None,
            "drive_id": f"{g.game_id}_{int(r.fixed_drive)}" if pd.notna(r.fixed_drive) else None,
            "down": int(r.down) if pd.notna(r.down) else None,
            "distance": int(r.ydstogo) if pd.notna(r.ydstogo) else None,
            "yardline_100": int(r.yardline_100) if pd.notna(r.yardline_100) else None,
            "play_type": pt,
            "yards_gained": int(r.yards_gained) if pd.notna(r.yards_gained) else 0,
            "is_success": bool(r.success == 1) if pd.notna(r.success) else None,
            "ppa": float(r.epa) if pd.notna(r.epa) else None,
            "is_dropback": bool(r.qb_dropback == 1),
            "is_scramble": is_scramble,
            "is_sack": is_sack,
            "is_turnover": turnover_type is not None,
            "turnover_type": turnover_type,
            "is_td": bool(r.touchdown == 1) and str(r.td_team) == str(r.posteam),
            "is_complete": bool(r.complete_pass == 1),
            "is_garbage_time": bool(pd.notna(r.vegas_wp) and not (lo <= r.vegas_wp <= hi)),
            "score_diff_pre": int(r.score_differential) if pd.notna(r.score_differential) else None,
            "wp_pre": float(r.vegas_wp) if pd.notna(r.vegas_wp) else None,
            "passer_id": f"NFL_P_{r.passer_player_id}" if pd.notna(r.passer_player_id) else None,
            "rusher_id": f"NFL_P_{r.rusher_player_id}" if pd.notna(r.rusher_player_id) else None,
            "receiver_id": f"NFL_P_{r.receiver_player_id}" if pd.notna(r.receiver_player_id) else None,
            "air_yards": int(r.air_yards) if pd.notna(r.air_yards) else None,
            "run_gap": r.run_gap if pd.notna(r.run_gap) else None,
            "run_location": r.run_location if pd.notna(r.run_location) else None,
            "shotgun": bool(r.shotgun == 1),
            "play_action": bool(r.is_play_action) if pd.notna(r.is_play_action) else None,
            "rpo": bool(r.is_rpo) if pd.notna(r.is_rpo) else None,
            "n_blitzers": int(r.n_blitzers) if pd.notna(r.n_blitzers) else None,
            "n_pass_rushers": int(r.n_pass_rushers) if pd.notna(r.n_pass_rushers) else None,
            "qb_hit": bool(r.qb_hit == 1),
            "is_tfl": bool(r.tackled_for_loss == 1),
            "is_pbu": bool(pd.notna(r.pass_defense_1_player_id)),
            "is_ff": bool(r.fumble_forced == 1),
            "int_worthy": bool(r.is_interception_worthy) if pd.notna(r.is_interception_worthy) else None,
            "cpoe": float(r.cpoe) if pd.notna(r.cpoe) else None,
            "posteam_score": int(r.posteam_score) if pd.notna(r.posteam_score) else None,
            "posteam_score_post": int(r.posteam_score_post) if pd.notna(r.posteam_score_post) else None,
            "source": "nflverse", "retrieved_at": retrieved_at,
            "effective_at": (pd.Timestamp(g.kickoff_utc) + timedelta(hours=4)).isoformat() if pd.notna(g.kickoff_utc) else retrieved_at,
        })
    plays = pd.DataFrame(rows)
    drives = _drives_from_plays(plays, p, gm, retrieved_at)
    return plays, drives


def _drives_from_plays(plays: pd.DataFrame, pbp: pd.DataFrame, gm: dict, retrieved_at: str) -> pd.DataFrame:
    rows = []
    src = pbp[pbp.fixed_drive.notna()]
    for (nv_gid, drv), grp in src.groupby(["game_id", "fixed_drive"], sort=False):
        g = gm[nv_gid]
        grp = grp.sort_values("play_id")
        first, last = grp.iloc[0], grp.iloc[-1]
        drive_id = f"{g.game_id}_{int(drv)}"
        ours = plays[plays.drive_id == drive_id]
        if ours.empty:
            continue
        off = ours.offense_team_id.iloc[0]
        result = _DRIVE_RESULT.get(str(last.fixed_drive_result), "OTHER")
        pts = None
        if pd.notna(first.posteam_score) and pd.notna(last.posteam_score_post):
            pts = max(0, int(last.posteam_score_post) - int(first.posteam_score))
        min_y100 = ours.yardline_100.min()
        rows.append({
            "drive_id": drive_id, "game_id": g.game_id, "offense_team_id": off,
            "defense_team_id": ours.defense_team_id.iloc[0], "drive_number": int(drv),
            "start_period": int(first.qtr) if pd.notna(first.qtr) else None,
            "start_yardline_100": int(ours.yardline_100.dropna().iloc[0]) if ours.yardline_100.notna().any() else None,
            "end_yardline_100": int(ours.yardline_100.dropna().iloc[-1]) if ours.yardline_100.notna().any() else None,
            "plays": int(len(ours[ours.play_type.isin(["PASS", "RUSH", "SACK"])])),
            "yards": int(ours.yards_gained.sum()),
            "elapsed_sec": int(first.game_seconds_remaining - last.game_seconds_remaining) if pd.notna(first.game_seconds_remaining) and pd.notna(last.game_seconds_remaining) else None,
            "result": result, "points": pts,
            "reached_opp_40": bool(pd.notna(min_y100) and min_y100 <= 40),
            "reached_rz": bool(pd.notna(min_y100) and min_y100 <= 20),
            "is_garbage_time": bool(ours.is_garbage_time.iloc[0]),
            "source": "nflverse", "retrieved_at": retrieved_at, "effective_at": ours.effective_at.iloc[0],
        })
    return pd.DataFrame(rows)


def qb_game_stats(pbp: pd.DataFrame, pfr_pass: pd.DataFrame | None, games: pd.DataFrame, resolver: ids.AliasResolver,
                  weeks: list[int] | None = None) -> pd.DataFrame:
    """player_game_stats rows for passers (QB) from pbp, enriched with PFR pressure counts when available."""
    gm = _game_map(games)
    p = pbp[pbp.game_id.isin(gm.keys()) & pbp.passer_player_id.notna() & (pbp.qb_dropback == 1)]
    if weeks is not None:
        p = p[p.week.isin(weeks)]
    retrieved_at = pbp.attrs["retrieved_at"].isoformat()
    rows = []
    for (nv_gid, team, pid), grp in p.groupby(["game_id", "posteam", "passer_player_id"]):
        g = gm[nv_gid]
        att = grp[grp.play_type == "pass"]
        att_real = att[att.sack != 1]
        rows.append({
            "game_id": g.game_id, "team_id": resolver.resolve("nflverse", alias=team), "player_id": f"NFL_P_{pid}",
            "position": "QB", "started": None, "snaps_off": None, "snaps_def": None,
            "pass_att": int(len(att_real)), "pass_cmp": int((att_real.complete_pass == 1).sum()),
            "pass_yds": int(att_real.yards_gained.sum()), "pass_td": int((att_real.pass_touchdown == 1).sum()),
            "pass_int": int((att_real.interception == 1).sum()), "sacks_taken": int((grp.sack == 1).sum()),
            "dropbacks": int(len(grp)), "ppa_dropback": float(grp.epa.mean()) if grp.epa.notna().any() else None,
            "qbr": None, "cpoe": float(att_real.cpoe.mean()) if att_real.cpoe.notna().any() else None,
            "int_worthy": None, "pressured_dropbacks": None, "pressured_ppa": None, "clean_ppa": None,
            "rush_att": None, "rush_yds": None, "rush_td": None, "targets": None, "receptions": None, "rec_yds": None,
            "rec_td": None, "tackles": None, "tfl": None, "sacks": None, "pressures": None, "ints": None, "pbu": None, "ff": None,
            "source": "nflverse", "retrieved_at": retrieved_at,
            "effective_at": (pd.Timestamp(g.kickoff_utc) + timedelta(hours=4)).isoformat() if pd.notna(g.kickoff_utc) else retrieved_at,
        })
    out = pd.DataFrame(rows)
    if pfr_pass is not None and not pfr_pass.empty and not out.empty:
        pf = pfr_pass.copy()
        pf["game_id"] = pf.game_id.map(lambda x: gm[x].game_id if x in gm else None)
        pf = pf[pf.game_id.notna()]
        pf["team_id"] = pf.team.map(lambda t: resolver.resolve("nflverse", alias=t))
        # PFR has no gsis id; join on (game, team) when a team has exactly one passer row on both sides
        one = out.groupby(["game_id", "team_id"]).player_id.transform("count") == 1
        pf_one = pf.groupby(["game_id", "team_id"]).pfr_player_id.transform("count") == 1
        merged = out[one].merge(pf[pf_one][["game_id", "team_id", "times_pressured", "times_blitzed", "times_hurried", "times_hit"]],
                                on=["game_id", "team_id"], how="left")
        out.loc[one, "pressured_dropbacks"] = merged.times_pressured.values
    return out


def team_pressure_rates(pfr_pass: pd.DataFrame | None, pfr_def: pd.DataFrame | None, games: pd.DataFrame,
                        resolver: ids.AliasResolver) -> pd.DataFrame:
    """Per team-game pressure counts from PFR weekly files. NULL where absent.
    off_pressures_allowed = sum of times_pressured across the team's passers (one count per dropback -> true rate).
    def_pressures         = sum of individual defenders' pressures; several defenders can be credited on one
                            dropback, so def_pressure_rate can exceed the play-level rate. Labeled as such in
                            metric_definitions ("PFR individual pressures per opponent dropback")."""
    gm = _game_map(games)
    rows = {}
    if pfr_pass is not None and not pfr_pass.empty:
        pf = pfr_pass[pfr_pass.game_id.isin(gm.keys())]
        agg = pf.groupby(["game_id", "team"]).agg(pressured=("times_pressured", "sum"), dropbacks=("times_pressured_pct", "size"))
        # times_pressured_pct is per-player; recompute team rate as pressured / (attempts + sacks) using pbp later.
        for (nv, team), r in agg.iterrows():
            key = (gm[nv].game_id, resolver.resolve("nflverse", alias=team))
            rows.setdefault(key, {})["off_pressures_allowed"] = int(r.pressured)
    if pfr_def is not None and not pfr_def.empty:
        pd_ = pfr_def[pfr_def.game_id.isin(gm.keys())]
        agg = pd_.groupby(["game_id", "team"]).def_pressures.sum()
        for (nv, team), v in agg.items():
            key = (gm[nv].game_id, resolver.resolve("nflverse", alias=team))
            rows.setdefault(key, {})["def_pressures"] = int(v)
    return pd.DataFrame([{"game_id": k[0], "team_id": k[1], **v} for k, v in rows.items()])
