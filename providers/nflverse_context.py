"""
nflverse context adapter (NFL): players, weekly rosters, depth charts, injuries, ESPN QBR.

Assets (verified 2026-09-06):
  players/players.parquet                        one row per player, gsis_id, draft info, ids for other providers
  weekly_rosters/roster_weekly_{season}.parquet  one row per player per team-week incl. status (ACT/RES/...)
  depth_charts/depth_charts_{season}.parquet     TIMESTAMPED SNAPSHOTS: dt, team, gsis_id, pos_grp, pos_abb, pos_rank.
                                                 No week column. We keep, per team-week, the latest snapshot whose
                                                 dt <= that week's kickoff, so a depth chart can never be "from the future".
  injuries/injuries_{season}.parquet             official report rows per team-week with report_status/practice_status
  espn_data/qbr_week_level.parquet               ESPN QBR per QB per game (join: ESPN game id + ESPN player id)

Slot mapping (Phase 3 depth_charts.slot) from nflverse pos_abb within pos_grp; raw pos_abb is kept alongside.
"""
from __future__ import annotations
import io

import pandas as pd

import config
from pipeline import ids
from providers.base import RequestManager, ProviderError

ASSETS = {
    "players": "https://github.com/nflverse/nflverse-data/releases/download/players/players.parquet",
    "rosters": "https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/roster_weekly_{season}.parquet",
    "depth": "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.parquet",
    "injuries": "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.parquet",
    "qbr": "https://github.com/nflverse/nflverse-data/releases/download/espn_data/qbr_week_level.parquet",
}

_POS_NORM = {"QB": "QB", "RB": "RB", "FB": "RB", "HB": "RB", "WR": "WR", "TE": "TE", "T": "OL", "OT": "OL", "G": "OL", "OG": "OL",
             "C": "OL", "OL": "OL", "DE": "EDGE", "OLB": "EDGE", "DT": "DL", "NT": "DL", "DL": "DL", "ILB": "LB", "MLB": "LB",
             "LB": "LB", "CB": "CB", "DB": "CB", "S": "S", "SS": "S", "FS": "S", "K": "K", "P": "P", "LS": "LS"}

# nflverse depth-chart abbreviations -> Phase 3 slot. Scheme-dependent for the front seven.
_SLOT_43 = {"LDE": "EDGE1", "RDE": "EDGE2", "LDT": "DL1", "RDT": "DL2", "WLB": "LB1", "MLB": "LB2", "SLB": "LB3"}
_SLOT_34 = {"LDE": "DL1", "NT": "DL2", "RDE": "DL3", "WLB": "EDGE1", "SLB": "EDGE2", "LILB": "LB1", "RILB": "LB2", "WILB": "LB1", "SILB": "LB2"}
_SLOT_COMMON = {"LCB": "CB1", "RCB": "CB2", "NB": "NB", "FS": "S1", "SS": "S2", "QB": "QB1", "RB": "RB1", "FB": "FB1", "TE": "TE1",
                "LT": "LT", "LG": "LG", "C": "C", "RG": "RG", "RT": "RT", "PK": "K", "P": "P", "LS": "LS"}


def _team_map(series: pd.Series, resolver: ids.AliasResolver) -> pd.Series:
    """Resolve each distinct abbreviation once (depth-chart files have 500k+ rows)."""
    lookup = {t: resolver.resolve("nflverse", alias=t) for t in series.dropna().unique()}
    return series.map(lookup)


def fetch_asset(rm: RequestManager, asset: str, season: int | None = None) -> pd.DataFrame | None:
    url = ASSETS[asset].format(season=season)
    try:
        res = rm.get(url, raw_bytes=True, timeout=120)
    except ProviderError as e:
        if "404" in str(e):
            return None
        raise
    df = pd.read_parquet(io.BytesIO(res.payload))
    df.attrs["retrieved_at"] = res.retrieved_at
    return df


# ---- players ------------------------------------------------------------------
def normalize_players(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    r = raw[raw.gsis_id.notna()]
    ts = raw.attrs["retrieved_at"].isoformat()
    players = pd.DataFrame({
        "player_id": "NFL_P_" + r.gsis_id.astype(str), "league": "NFL", "full_name": r.display_name,
        "position": r.position.map(lambda p: _POS_NORM.get(str(p), None)), "position_raw": r.position,
        "birth_date": pd.to_datetime(r.birth_date, errors="coerce").dt.date, "height_in": r.height, "weight_lb": r.weight,
        "draft_year": r.draft_year, "draft_round": r.draft_round, "draft_pick": r.draft_pick,
        "recruit_stars": None, "recruit_rating": None, "recruit_rank_natl": None, "source": "nflverse", "retrieved_at": ts,
    })
    alias_rows = []
    for col, prov in (("espn_id", "espn"), ("pfr_id", "pfr"), ("pff_id", "pff"), ("gsis_id", "nflverse")):
        sub = r[r[col].notna()]
        alias_rows.append(pd.DataFrame({"provider": prov, "provider_id": sub[col].astype(str), "player_id": "NFL_P_" + sub.gsis_id.astype(str)}))
    return players, pd.concat(alias_rows, ignore_index=True).drop_duplicates(["provider", "provider_id"])


# ---- rosters ------------------------------------------------------------------
def normalize_rosters(raw: pd.DataFrame, season: int, resolver: ids.AliasResolver, prior: pd.DataFrame | None) -> pd.DataFrame:
    """One row per player per team-week. arrival_type derived against the prior season's final roster when given."""
    ts = raw.attrs["retrieved_at"].isoformat()
    r = raw[raw.gsis_id.notna()].copy()
    r["team_id"] = _team_map(r.team, resolver)
    prior_map: dict[str, str] = {}
    if prior is not None and not prior.empty:
        last_wk = prior.week.max()
        prior_map = dict(zip(prior[prior.week == last_wk].player_id, prior[prior.week == last_wk].team_id))
    rows = []
    for _, x in r.iterrows():
        pid = f"NFL_P_{x.gsis_id}"
        prior_team = prior_map.get(pid)
        if x.rookie_year == season or (pd.notna(x.entry_year) and int(x.entry_year) == season):
            arrival = "DRAFT" if pd.notna(x.draft_number) else "UDFA"
        elif prior_team is None:
            arrival = "FA" if prior_map else None
        elif prior_team == x.team_id:
            arrival = "RETURNING"
        else:
            arrival = "FA"       # trade vs FA not distinguishable from rosters alone
        rows.append({
            "team_id": x.team_id, "season": season, "week": int(x.week), "player_id": pid,
            "position": _POS_NORM.get(str(x.position), None), "jersey": int(x.jersey_number) if pd.notna(x.jersey_number) else None,
            "class_year": None, "years_exp": int(x.years_exp) if pd.notna(x.years_exp) else None,
            "status": str(x.status) if pd.notna(x.status) else None,
            "is_new_to_team": (prior_team != x.team_id) if prior_map else None, "arrival_type": arrival,
            "prior_team_id": prior_team, "source": "nflverse", "retrieved_at": ts,
        })
    return pd.DataFrame(rows)


# ---- depth charts (snapshots -> per team-week) ------------------------------------
def normalize_depth_charts(raw: pd.DataFrame, season: int, games: pd.DataFrame, resolver: ids.AliasResolver) -> pd.DataFrame:
    ts = raw.attrs["retrieved_at"].isoformat()
    d = raw[raw.gsis_id.notna()].copy()
    d["dt"] = pd.to_datetime(d.dt, utc=True)
    d["team_id"] = _team_map(d.team, resolver)
    # each team's kickoff per week
    g = games[games.kickoff_utc.notna()]
    kick = pd.concat([g[["week", "home_team_id", "kickoff_utc"]].rename(columns={"home_team_id": "team_id"}),
                      g[["week", "away_team_id", "kickoff_utc"]].rename(columns={"away_team_id": "team_id"})])
    kick["kickoff_utc"] = pd.to_datetime(kick.kickoff_utc, utc=True)
    rows = []
    by_team = {t: sub for t, sub in d.groupby("team_id")}
    for (team_id, week), k in kick.groupby(["team_id", "week"]):
        cutoff = k.kickoff_utc.iloc[0]
        dt_ = by_team.get(team_id)
        snaps = dt_[dt_.dt <= cutoff] if dt_ is not None else d.iloc[0:0]
        if snaps.empty:
            continue
        snap = snaps[snaps.dt == snaps.dt.max()].copy()
        # WRs: nflverse uses three distinct pos_slot values (X/Z/slot) and a global pos_rank across all WRs.
        # Map slots in ascending pos_slot order to WR1..WR3 and rank players within each slot.
        wr = snap[snap.pos_abb == "WR"]
        wr_slot_index = {ps: i + 1 for i, ps in enumerate(sorted(wr.pos_slot.unique()))}
        wr_rank = wr.groupby("pos_slot").pos_rank.rank(method="first").astype(int)
        snap.loc[wr.index, "_wr_slot"] = wr.pos_slot.map(wr_slot_index)
        snap.loc[wr.index, "_wr_rank"] = wr_rank
        for _, x in snap.iterrows():
            grp = str(x.pos_grp)
            abb = str(x.pos_abb)
            if "4-3" in grp:
                slot = _SLOT_43.get(abb) or _SLOT_COMMON.get(abb)
            elif "3-4" in grp:
                slot = _SLOT_34.get(abb) or _SLOT_COMMON.get(abb)
            elif "Special" in grp:
                slot = _SLOT_COMMON.get(abb)
            else:  # offense group e.g. '3WR 1TE'
                slot = f"WR{int(x._wr_slot)}" if abb == "WR" else _SLOT_COMMON.get(abb)
            if slot is None:
                continue
            rows.append({
                "team_id": team_id, "season": season, "week": int(week), "slot": slot, "player_id": f"NFL_P_{x.gsis_id}",
                "rank_in_slot": int(x._wr_rank) if abb == "WR" else int(x.pos_rank), "is_projected": False, "projection_basis": None, "confidence": 1.0,
                "pos_abb_raw": abb, "scheme": grp, "snapshot_dt": snap.dt.iloc[0].isoformat(),
                "source": "nflverse", "retrieved_at": ts,
            })
    return pd.DataFrame(rows).drop_duplicates(["team_id", "season", "week", "slot", "rank_in_slot"], keep="first")


# ---- injuries ----------------------------------------------------------------------
_STATUS = {"Out": "OUT", "Doubtful": "DOUBTFUL", "Questionable": "QUESTIONABLE", "Probable": "PROBABLE"}
_PRACTICE = {"Did Not Participate In Practice": "DNP", "Limited Participation in Practice": "LP", "Full Participation in Practice": "FP"}


def normalize_injuries(raw: pd.DataFrame, season: int, games: pd.DataFrame, resolver: ids.AliasResolver) -> pd.DataFrame:
    ts = raw.attrs["retrieved_at"].isoformat()
    r = raw[raw.gsis_id.notna()].copy()
    r["team_id"] = _team_map(r.team, resolver)
    gm = {}
    for _, g in games.iterrows():
        gm[(g.home_team_id, int(g.week), g.season_type)] = g
        gm[(g.away_team_id, int(g.week), g.season_type)] = g
    rows = []
    for _, x in r.iterrows():
        st = _STATUS.get(str(x.report_status), None)
        pr = _PRACTICE.get(str(x.practice_status), None)
        if st is None and pr in (None, "FP"):
            continue   # resting/full participation with no designation is not an injury row
        season_type = "REG" if str(x.season_type) == "REG" else "POST"
        g = gm.get((x.team_id, int(x.week), season_type))
        report_date = None
        if g is not None and pd.notna(g.kickoff_utc):
            report_date = (pd.Timestamp(g.kickoff_utc) - pd.Timedelta(days=1)).date()   # official reports finalize Fri/Sat
        rows.append({
            "injury_row_id": f"{x.team_id}_{x.gsis_id}_{season}W{int(x.week):02d}_{st or 'NA'}_{pr or 'NA'}",
            "league": "NFL", "season": season, "week": int(x.week), "game_id": g.game_id if g is not None else None,
            "team_id": x.team_id, "player_id": f"NFL_P_{x.gsis_id}", "position": _POS_NORM.get(str(x.position), None),
            "depth_slot": None, "status": st or "UNKNOWN", "practice_status": pr,
            "injury_desc": x.report_primary_injury if pd.notna(x.report_primary_injury) else x.practice_primary_injury,
            "report_date": report_date, "source": "nflverse", "entered_by": None, "retrieved_at": ts,
            "effective_at": pd.Timestamp(report_date).isoformat() if report_date else ts,
        })
    return pd.DataFrame(rows)


# ---- ESPN QBR -----------------------------------------------------------------------
def normalize_qbr(raw: pd.DataFrame, season: int, games: pd.DataFrame, player_aliases: pd.DataFrame) -> pd.DataFrame:
    """Returns (game_id, team_id?, player_id, qbr) rows for the season, to be merged into player_game_stats.qbr."""
    q = raw[(raw.season == season)].copy()
    espn_to_gid = {}
    for _, g in games.iterrows():
        try:
            espn = str(g.provider_game_ids).split('"espn":"')[1].split('"')[0]
            if espn:
                espn_to_gid[espn] = g.game_id
        except IndexError:
            pass
    pa = player_aliases[player_aliases.provider == "espn"]
    espn_to_pid = dict(zip(pa.provider_id.astype(str), pa.player_id))
    q["game_id"] = q.game_id.astype(str).map(espn_to_gid)
    q["player_id"] = q.player_id.astype(str).map(espn_to_pid)
    q = q[q.game_id.notna() & q.player_id.notna()]
    return pd.DataFrame({"game_id": q.game_id, "player_id": q.player_id, "qbr": q.qbr_total.astype(float)})
