"""
Game-level metric engine. One implementation for both leagues, fed by the slim `plays` and `drives`
tables (Phase 3 schema). Provider-native values (CFBD advanced stats) are overlaid afterwards by the
caller where they exist, and the overlay is recorded per field in `overlay_sources`.

Definitions (also registered in metric_definitions in a later phase):
  scrimmage play   : play_type in {PASS, RUSH, SACK}
  dropback         : is_dropback (pass attempt, sack, or scramble)
  success          : NFL = nflfastR `success` (EPA > 0); CFB = yardage rule (50%/70%/100% of distance)
                     -> stored with metric_system so they are never compared across leagues
  explosive        : rush >= EXPLOSIVE_RUSH_YDS or pass >= EXPLOSIVE_PASS_YDS (config)
  standard downs   : 1st; 2nd & <=7; 3rd/4th & <=4      (CFBD convention)
  passing downs    : everything else
  line yards       : Football Outsiders formula per rush: <0 -> 1.2x, 0..4 -> 1x, 5..10 -> 0.5x, >10 -> 0
  opportunity rate : rushes gaining >= 4
  stuff rate       : rushes gaining <= 0
  power success    : rushes on 3rd/4th & <=2 that convert
  neutral script   : |score diff| <= 8 and period <= 3
  scoring opp      : drive that reached the opponent 40 (yardline_100 <= 40)
  garbage time     : flagged upstream on each play (provider-specific rule, see providers)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config

SCRIMMAGE = ("PASS", "RUSH", "SACK")


def _rate(num, den):
    return float(num) / float(den) if den and den > 0 else None


def _mean(s: pd.Series):
    s = s.dropna()
    return float(s.mean()) if len(s) else None


def _line_yards(y: pd.Series) -> pd.Series:
    y = y.astype(float)
    return np.where(y < 0, 1.2 * y, np.where(y <= 4, y, np.where(y <= 10, 4 + 0.5 * (y - 4), 7.0)))


def _is_standard_down(down, dist):
    return (down == 1) | ((down == 2) & (dist <= 7)) | (down.isin([3, 4]) & (dist <= 4))


def _success_cfb(down, dist, gained):
    need = np.where(down == 1, 0.5 * dist, np.where(down == 2, 0.7 * dist, dist))
    return gained >= need


def _flag(df: pd.DataFrame, col: str) -> pd.Series:
    return (df[col] == True) if col in df.columns else pd.Series(False, index=df.index)   # noqa: E712


def _havoc_parts(sc: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(front-seven havoc, secondary havoc). NFL has TFL/PBU/FF flags; CFB uses a proxy (negative plays, sacks,
    turnovers) that CFBD's native havoc overlays afterwards."""
    tfl = _flag(sc, "is_tfl") | ((sc.yards_gained < 0) & (sc.play_type == "RUSH"))
    front = tfl | (sc.is_sack == True) | _flag(sc, "is_ff")                                     # noqa: E712
    secondary = ((sc.is_turnover == True) & (sc.turnover_type == "INT")) | _flag(sc, "is_pbu")  # noqa: E712
    return front, secondary


def _havoc_mask(sc: pd.DataFrame) -> pd.Series:
    f, d = _havoc_parts(sc)
    return f | d


def _seconds_per_play(df: pd.DataFrame) -> float | None:
    """Mean elapsed seconds between consecutive offensive scrimmage snaps within the same drive."""
    d = df.sort_values(["drive_id", "game_sec_remaining"], ascending=[True, False])
    d = d[d.game_sec_remaining.notna()]
    if d.empty:
        return None
    gaps = d.groupby("drive_id").game_sec_remaining.diff(-1)   # positive = seconds consumed before next snap
    gaps = gaps[(gaps > 0) & (gaps <= 60)]
    return float(gaps.mean()) if len(gaps) else None


def offense_side(plays: pd.DataFrame, drives: pd.DataFrame, team_id: str, prefix: str, league: str) -> dict:
    """Metrics for `team_id` when it had the ball (prefix 'off_') or its opponents had it (prefix 'def_')."""
    p = plays[plays.offense_team_id == team_id].copy()
    sc = p[p.play_type.isin(SCRIMMAGE)].copy()
    n = len(sc)
    out: dict = {f"{prefix}plays": n if n else None}
    if n == 0:
        return out
    # success flag per league
    if league == "CFB":
        sc["is_success"] = _success_cfb(sc.down, sc.distance, sc.yards_gained)
    db = sc[sc.is_dropback == True]           # noqa: E712
    ru = sc[(sc.play_type == "RUSH") & (sc.is_dropback != True)]  # noqa: E712  (excludes scrambles)
    std = _is_standard_down(sc.down, sc.distance)
    out.update({
        f"{prefix}ppa_play": _mean(sc.ppa),
        f"{prefix}ppa_pass": _mean(db.ppa),
        f"{prefix}ppa_rush": _mean(ru.ppa),
        f"{prefix}ppa_dropback": _mean(db.ppa),
        f"{prefix}ppa_early": _mean(sc[sc.down.isin([1, 2])].ppa),
        f"{prefix}ppa_late": _mean(sc[sc.down.isin([3, 4])].ppa),
        f"{prefix}success_rate": _mean(sc.is_success.astype(float)),
        f"{prefix}success_pass": _mean(db.is_success.astype(float)),
        f"{prefix}success_rush": _mean(ru.is_success.astype(float)),
        f"{prefix}success_std_downs": _mean(sc[std].is_success.astype(float)),
        f"{prefix}success_pass_downs": _mean(sc[~std].is_success.astype(float)),
        f"{prefix}explosiveness": _mean(sc[sc.is_success == True].ppa),     # noqa: E712  IsoPPP-style
    })
    exp_r = (ru.yards_gained >= config.EXPLOSIVE_RUSH_YDS)
    exp_p = (db.yards_gained >= config.EXPLOSIVE_PASS_YDS) & (db.play_type == "PASS")
    if prefix == "off_":
        out.update({
            "off_explosive_play_rate": _rate(exp_r.sum() + exp_p.sum(), n),
            "off_rush_10plus_rate": _rate(exp_r.sum(), len(ru)),
            "off_pass_20plus_rate": _rate(exp_p.sum(), len(db)),
            "off_line_yards": _mean(pd.Series(_line_yards(ru.yards_gained))) if len(ru) else None,
            "off_opportunity_rate": _rate((ru.yards_gained >= 4).sum(), len(ru)),
            "off_stuff_rate_allowed": _rate((ru.yards_gained <= 0).sum(), len(ru)),
            "off_power_success": _rate(((ru.down.isin([3, 4])) & (ru.distance <= 2) & (ru.yards_gained >= ru.distance)).sum(),
                                       ((ru.down.isin([3, 4])) & (ru.distance <= 2)).sum()),
            "off_second_level_yds": _mean(ru.yards_gained.clip(lower=5, upper=10) - 5) if len(ru) else None,
            "off_open_field_yds": _mean(ru.yards_gained.clip(lower=10) - 10) if len(ru) else None,
            "off_pass_rate": _rate(len(db), n),
            "off_early_down_pass_rate": _rate((sc.down.isin([1, 2]) & (sc.is_dropback == True)).sum(), sc.down.isin([1, 2]).sum()),  # noqa: E712
            "off_sec_per_play": _seconds_per_play(sc),
        })
        neutral = sc[(sc.score_diff_pre.abs() <= 8) & (sc.period <= 3)]
        out["off_neutral_pass_rate"] = _rate((neutral.is_dropback == True).sum(), len(neutral)) if len(neutral) else None  # noqa: E712
        out["off_neutral_sec_per_play"] = _seconds_per_play(neutral) if len(neutral) else None
        out["off_havoc_allowed"] = _rate(_havoc_mask(sc).sum(), n)
        # NFL-only charting
        if league == "NFL":
            out["off_play_action_rate"] = _rate((db.play_action == True).sum(), len(db)) if db.play_action.notna().any() else None  # noqa: E712
            out["off_rpo_rate"] = _rate((sc.rpo == True).sum(), n) if sc.rpo.notna().any() else None      # noqa: E712
            out["off_shotgun_rate"] = _rate((sc.shotgun == True).sum(), n) if sc.shotgun.notna().any() else None  # noqa: E712
            out["off_avg_air_yards"] = _mean(db[db.play_type == "PASS"].air_yards)
            out["off_deep_pass_rate"] = _rate((db.air_yards >= 20).sum(), (db.play_type == "PASS").sum())
    else:
        out.update({
            "def_explosive_play_rate_allowed": _rate(exp_r.sum() + exp_p.sum(), n),
            "def_line_yards_allowed": _mean(pd.Series(_line_yards(ru.yards_gained))) if len(ru) else None,
            "def_stuff_rate": _rate((ru.yards_gained <= 0).sum(), len(ru)),
        })
        front, db_ = _havoc_parts(sc)
        out["def_havoc_front"] = _rate(front.sum(), n)
        out["def_havoc_db"] = _rate(db_.sum(), n)
        out["def_havoc"] = _rate((front | db_).sum(), n)
        if league == "NFL":
            out["def_blitz_rate"] = _rate((db.n_blitzers > 0).sum(), db.n_blitzers.notna().sum()) if db.n_blitzers.notna().any() else None
            std_rush = db[db.n_pass_rushers.notna() & (db.n_pass_rushers <= 4)]
            out["def_pressure_no_blitz_rate"] = _rate(((std_rush.qb_hit == True) | (std_rush.is_sack == True)).sum(), len(std_rush)) if len(std_rush) else None  # noqa: E712
    # drives
    d = drives[drives.offense_team_id == team_id]
    if len(d):
        opps = d[d.reached_opp_40 == True]     # noqa: E712
        rz = d[d.reached_rz == True]           # noqa: E712
        if prefix == "off_":
            out["off_scoring_opps"] = int(len(opps))
            out["off_pts_per_scoring_opp"] = _mean(opps.points)
            out["off_td_rate_opp_territory"] = _rate((opps.result == "TD").sum(), len(opps))
            out["off_avg_start_yardline"] = _mean(100 - d.start_yardline_100)      # own-goal-relative
            out["off_rz_trips"] = int(len(rz))
            out["off_rz_td_rate"] = _rate((rz.result == "TD").sum(), len(rz))
        else:
            out["def_pts_per_scoring_opp_allowed"] = _mean(opps.points)
            out["def_avg_start_yardline_allowed"] = _mean(100 - d.start_yardline_100)
            out["def_rz_td_rate_allowed"] = _rate((rz.result == "TD").sum(), len(rz))
    return out


def team_game_advanced(plays: pd.DataFrame, drives: pd.DataFrame, game_id: str, team_id: str, opponent_id: str,
                       league: str, garbage_filtered: bool, source: str, retrieved_at: str, effective_at: str) -> dict:
    p = plays[plays.game_id == game_id]
    d = drives[drives.game_id == game_id]
    if garbage_filtered:
        p = p[p.is_garbage_time != True]       # noqa: E712
        d = d[d.is_garbage_time != True]       # noqa: E712
    row = {"game_id": game_id, "team_id": team_id, "opponent_id": opponent_id, "is_garbage_filtered": garbage_filtered,
           "metric_system": "PPA_CFBD" if league == "CFB" else "EPA_NFLFASTR"}
    row.update(offense_side(p, d, team_id, "off_", league))
    row.update(offense_side(p, d, opponent_id, "def_", league))   # opponent's offense = this team's defense
    row.update({"source": source, "retrieved_at": retrieved_at, "effective_at": effective_at})
    return row


def team_game_box_from_plays(plays: pd.DataFrame, drives: pd.DataFrame, game_id: str, team_id: str, opponent_id: str,
                             is_home: bool, points: int | None, points_allowed: int | None) -> dict:
    """Box-score line derived from plays (used for NFL; CFB uses provider box + this for cross-checks)."""
    p = plays[(plays.game_id == game_id)]
    o = p[(p.offense_team_id == team_id) & p.play_type.isin(SCRIMMAGE)]
    dfn = p[(p.offense_team_id == opponent_id) & p.play_type.isin(SCRIMMAGE)]
    db = o[o.is_dropback == True]      # noqa: E712
    ru = o[(o.play_type == "RUSH") & (o.is_dropback != True)]   # noqa: E712
    att = o[o.play_type == "PASS"]
    third = o[o.down == 3]
    fourth = o[o.down == 4]
    conv3 = (third.yards_gained >= third.distance) | (third.is_td == True)     # noqa: E712
    conv4 = (fourth.yards_gained >= fourth.distance) | (fourth.is_td == True)  # noqa: E712
    return {
        "game_id": game_id, "team_id": team_id, "opponent_id": opponent_id, "is_home": is_home, "is_garbage_filtered": False,
        "points": points, "points_allowed": points_allowed, "plays": int(len(o)),
        "total_yards": int(o.yards_gained.sum()), "rush_att": int(len(ru)), "rush_yds": int(ru.yards_gained.sum()),
        "rush_td": int((ru.is_td == True).sum()), "pass_att": int(len(att)),            # noqa: E712
        "pass_cmp": int(att.is_complete.sum()) if "is_complete" in att else None,
        "pass_yds": int(att.yards_gained.sum()), "pass_td": int((att.is_td == True).sum()),   # noqa: E712
        "pass_int": int(((att.is_turnover == True) & (att.turnover_type == "INT")).sum()) if "turnover_type" in att else None,  # noqa: E712
        "sacks_taken": int((o.is_sack == True).sum()), "sack_yds_lost": int(-o[o.is_sack == True].yards_gained.sum()),  # noqa: E712
        "dropbacks": int(len(db)), "first_downs": None,
        "third_down_att": int(len(third)), "third_down_conv": int(conv3.sum()),
        "fourth_down_att": int(len(fourth)), "fourth_down_conv": int(conv4.sum()),
        "fumbles_lost": int(((o.is_turnover == True) & (o.turnover_type == "FUM")).sum()) if "turnover_type" in o else None,  # noqa: E712
        "turnovers": int((o.is_turnover == True).sum()), "takeaways": int((dfn.is_turnover == True).sum()),  # noqa: E712
        "penalties": None, "penalty_yds": None, "possession_sec": None,
        "tfl": int(((dfn.yards_gained < 0) & (dfn.play_type == "RUSH")).sum() + (dfn.is_sack == True).sum()),  # noqa: E712
        "sacks_made": int((dfn.is_sack == True).sum()),     # noqa: E712
        "int_made": int(((dfn.is_turnover == True) & (dfn.turnover_type == "INT")).sum()) if "turnover_type" in dfn else None,  # noqa: E712
        "pass_deflections": None, "fumbles_forced": None, "punts": None, "punt_yds": None, "fg_att": None, "fg_made": None,
    }
