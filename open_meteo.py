"""
nflverse roster-intelligence adapter (NFL): snap counts, weekly player stats, draft picks, PFR defensive pressures.

  snap_counts/snap_counts_{season}.parquet     per player per game: offense/defense/ST snaps and shares (keyed by pfr_player_id)
  stats_player/stats_player_week_{season}      per player per game: passing/rushing/receiving/defense box (keyed by gsis player_id)
  draft_picks/draft_picks.parquet              every pick with gsis_id, cfb_player_id, college
  pfr_advstats/advstats_week_def_{season}      per defender per game: def_pressures, def_sacks, ... (keyed by pfr_player_id)

Produces one table: player_season_usage (one row per player per team per season) with snaps, shares and
production, which the roster engine turns into returning production, continuity and QB status.
"""
from __future__ import annotations

import pandas as pd

from pipeline import ids
from providers.nflverse_context import _team_map, _POS_NORM
from providers.nflverse_stats import fetch_asset as _fetch  # same release-asset fetcher

ASSETS = {
    "snaps": "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{season}.parquet",
    "pstats": "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.parquet",
    "draft": "https://github.com/nflverse/nflverse-data/releases/download/draft_picks/draft_picks.parquet",
    "pfr_def": "https://github.com/nflverse/nflverse-data/releases/download/pfr_advstats/advstats_week_def_{season}.parquet",
}


def fetch(rm, asset: str, season: int | None = None):
    import config
    config.NFLVERSE_ASSETS.setdefault(asset, ASSETS[asset])
    return _fetch(rm, asset, season)


def player_season_usage(snaps: pd.DataFrame | None, pstats: pd.DataFrame | None, pfr_def: pd.DataFrame | None,
                        season: int, resolver: ids.AliasResolver, player_aliases: pd.DataFrame) -> pd.DataFrame:
    """Regular season only. Snap shares are relative to the team's total snaps over games the player was rostered."""
    pfr_to_pid = dict(zip(player_aliases[player_aliases.provider == "pfr"].provider_id.astype(str),
                          player_aliases[player_aliases.provider == "pfr"].player_id))
    rows = {}
    ts = None
    if snaps is not None and not snaps.empty:
        ts = snaps.attrs["retrieved_at"].isoformat()
        s = snaps[snaps.game_type == "REG"].copy()
        s["team_id"] = _team_map(s.team, resolver)
        s["player_id"] = s.pfr_player_id.astype(str).map(pfr_to_pid)
        s = s[s.player_id.notna()]
        team_tot = s.groupby(["team_id", "game_id"]).agg(off=("offense_snaps", "max"), deff=("defense_snaps", "max")).groupby("team_id").sum()
        g = s.groupby(["player_id", "team_id"]).agg(off_snaps=("offense_snaps", "sum"), def_snaps=("defense_snaps", "sum"), st_snaps=("st_snaps", "sum"),
                                                    games=("game_id", "nunique"), position=("position", "first"),
                                                    off_starts=("offense_pct", lambda x: int((x >= 0.5).sum())),
                                                    def_starts=("defense_pct", lambda x: int((x >= 0.5).sum())))
        for (pid, tid), r in g.iterrows():
            tot = team_tot.loc[tid] if tid in team_tot.index else None
            rows[(pid, tid)] = {"player_id": pid, "team_id": tid, "season": season, "position_raw": r.position,
                                "off_snaps": int(r.off_snaps), "def_snaps": int(r.def_snaps), "st_snaps": int(r.st_snaps),
                                "off_snap_share": float(r.off_snaps / tot.off) if tot is not None and tot.off else None,
                                "def_snap_share": float(r.def_snaps / tot.deff) if tot is not None and tot.deff else None,
                                "games": int(r.games), "off_starts": int(r.off_starts), "def_starts": int(r.def_starts)}
    if pstats is not None and not pstats.empty:
        ts = ts or pstats.attrs["retrieved_at"].isoformat()
        p = pstats[pstats.season_type == "REG"].copy()
        p["team_id"] = _team_map(p.team, resolver)
        p["player_id"] = "NFL_P_" + p.player_id.astype(str)
        agg = p.groupby(["player_id", "team_id"]).agg(
            pass_att=("attempts", "sum"), pass_cmp=("completions", "sum"), pass_yds=("passing_yards", "sum"), pass_td=("passing_tds", "sum"),
            pass_int=("passing_interceptions", "sum"), sacks_taken=("sacks_suffered", "sum"), pass_epa=("passing_epa", "sum"),
            rush_att=("carries", "sum"), rush_yds=("rushing_yards", "sum"), rush_td=("rushing_tds", "sum"), rush_epa=("rushing_epa", "sum"),
            targets=("targets", "sum"), receptions=("receptions", "sum"), rec_yds=("receiving_yards", "sum"), rec_td=("receiving_tds", "sum"),
            tackles=("def_tackles_solo", "sum"), tfl=("def_tackles_for_loss", "sum"), sacks=("def_sacks", "sum"), qb_hits=("def_qb_hits", "sum"),
            ints=("def_interceptions", "sum"), pbu=("def_pass_defended", "sum"), ff=("def_fumbles_forced", "sum"),
            games_with_stats=("game_id", "nunique"), position_raw2=("position", "first"))
        for (pid, tid), r in agg.iterrows():
            row = rows.setdefault((pid, tid), {"player_id": pid, "team_id": tid, "season": season, "position_raw": r.position_raw2,
                                               "off_snaps": None, "def_snaps": None, "st_snaps": None, "off_snap_share": None,
                                               "def_snap_share": None, "games": int(r.games_with_stats), "off_starts": None, "def_starts": None})
            row.update({k: (float(v) if pd.notna(v) else None) for k, v in r.drop(["games_with_stats", "position_raw2"]).items()})
    if pfr_def is not None and not pfr_def.empty:
        d = pfr_def[pfr_def.game_type == "REG"].copy()
        d["team_id"] = _team_map(d.team, resolver)
        d["player_id"] = d.pfr_player_id.astype(str).map(pfr_to_pid)
        d = d[d.player_id.notna()]
        pr = d.groupby(["player_id", "team_id"]).def_pressures.sum()
        for (pid, tid), v in pr.items():
            if (pid, tid) in rows:
                rows[(pid, tid)]["pressures"] = int(v)
    out = pd.DataFrame(list(rows.values()))
    if not out.empty:
        out["position"] = out.position_raw.map(lambda x: _POS_NORM.get(str(x), None))
        out["source"] = "nflverse"; out["retrieved_at"] = ts
    return out


def draft_rows(draft: pd.DataFrame, season: int, resolver: ids.AliasResolver) -> pd.DataFrame:
    d = draft[draft.season == season].copy()
    d["team_id"] = _team_map(d.team.replace({"LVR": "LV", "GNB": "GB", "KAN": "KC", "NWE": "NE", "NOR": "NO", "SFO": "SF", "TAM": "TB",
                                             "SDG": "LAC", "OAK": "LV", "STL": "LAR", "LAR": "LA", "JAC": "JAX", "WSH": "WAS"}), resolver)
    return pd.DataFrame({"season": d.season, "round": d["round"], "pick": d.pick, "team_id": d.team_id,
                         "player_id": "NFL_P_" + d.gsis_id.astype(str).where(d.gsis_id.notna(), None),
                         "cfb_player_slug": d.cfb_player_id, "name": d.pfr_player_name, "position": d.position, "college": d.college})
