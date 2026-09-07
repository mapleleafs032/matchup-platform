"""
Row-level validation for the tables built in this phase. Returns the clean frame; rejects are logged.
Rules run in the order listed in Phase 3 §10.
"""
from __future__ import annotations
import re

import pandas as pd

import config
from pipeline.log import ValidationLog

GAME_ID_RE = re.compile(r"^\d{4}_(CFB|NFL)_[WP]\d{2}_[A-Z0-9]+_[A-Z0-9]+$")


def _in_range(v, lo, hi) -> bool:
    return pd.isna(v) or (lo <= v <= hi)


def validate_games(df: pd.DataFrame, league: str, season: int, vlog: ValidationLog) -> pd.DataFrame:
    if df.empty or "game_id" not in df.columns:
        return pd.DataFrame(columns=["game_id"])
    keep = []
    seen = set()
    for _, r in df.iterrows():
        gid = r["game_id"]
        ok = True
        if not GAME_ID_RE.match(str(gid)):
            vlog.reject("SCHEMA", gid, "game_id", gid, "pattern"); ok = False
        if r["season"] != season:
            vlog.reject("SEASON_MISMATCH", gid, "season", r["season"], season); ok = False
        if r["season"] < config.MIN_ALLOWED_SEASON:
            vlog.reject("SEASON_TOO_EARLY", gid, "season", r["season"], f">={config.MIN_ALLOWED_SEASON}"); ok = False
        if r["league"] != league:
            vlog.reject("LEAGUE_MISMATCH", gid, "league", r["league"], league); ok = False
        lo, hi = config.VALIDATION_RANGES["week_cfb" if league == "CFB" else "week_nfl"]
        if not (lo <= int(r["week"]) <= hi):
            vlog.reject("RANGE", gid, "week", r["week"], f"{lo}-{hi}"); ok = False
        if r["away_team_id"] == r["home_team_id"]:
            vlog.reject("IDENTITY", gid, "teams", r["away_team_id"], "away != home"); ok = False
        if gid in seen:
            vlog.reject("DUP_GAME", gid, "game_id", gid, "unique"); ok = False
        if pd.isna(r.get("kickoff_utc")) and not bool(r.get("kickoff_is_tba", False)):
            vlog.reject("SCHEMA", gid, "kickoff_utc", None, "timestamp or kickoff_is_tba=True"); ok = False
        if league == "CFB" and pd.isna(r.get("is_fcs_game")):
            vlog.reject("FCS_UNTAGGED", gid, "is_fcs_game", None, "bool"); ok = False
        if ok:
            keep.append(gid)
            seen.add(gid)
    return df[df.game_id.isin(keep)].copy()


def validate_market(df: pd.DataFrame, known_game_ids: set[str], vlog: ValidationLog) -> pd.DataFrame:
    keep_idx = []
    sr = config.VALIDATION_RANGES["spread_home"]
    tr = config.VALIDATION_RANGES["total"]
    mr = config.VALIDATION_RANGES["moneyline"]
    for i, r in df.iterrows():
        key = r["snapshot_id"]
        ok = True
        if r["game_id"] not in known_game_ids:
            vlog.reject("IDENTITY", key, "game_id", r["game_id"], "known game"); ok = False
        if not _in_range(r["spread_home"], *sr):
            vlog.reject("RANGE", key, "spread_home", r["spread_home"], sr); ok = False
        if not _in_range(r["total"], *tr):
            vlog.reject("RANGE", key, "total", r["total"], tr); ok = False
        for col in ("ml_home", "ml_away"):
            v = r[col]
            if pd.notna(v) and not (mr[0] <= v <= mr[1]):
                # books post placeholder prices like -100000 on huge favorites; not a real market -> unavailable
                vlog.warn("ML_PLACEHOLDER", key, col, v, f"|v|<={mr[1]} -> set NULL")
                df.loc[i, col] = None
            elif pd.notna(v) and -100 < v < 100:
                vlog.reject("RANGE", key, col, v, "American odds, |v|>=100"); ok = False
        if pd.notna(r["spread_home"]) and (abs(r["spread_home"] * 2) % 1 != 0):
            vlog.reject("RANGE", key, "spread_home", r["spread_home"], "multiple of 0.5"); ok = False
        # a spread with no total and no ML is suspicious but allowed; all three missing is rejected
        if pd.isna(r["spread_home"]) and pd.isna(r["total"]) and pd.isna(r["ml_home"]):
            vlog.reject("SCHEMA", key, "all_markets", None, "at least one market"); ok = False
        if ok:
            keep_idx.append(i)
    return df.loc[keep_idx].copy()
