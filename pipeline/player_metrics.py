"""
Player-level metrics, the way Game on Paper and similar sites present them.

Everything is AS OF a week: only games played before that week count, so an archived matchup page shows
what was known at the time, never what happened later.

NFL   computed from our own play-by-play (nflfastR), which carries passer, rusher and receiver on every
      play along with EPA, air yards and completion probability. Rate stats exclude garbage time, as
      Game on Paper's adjusted stats do; volume stats count every snap.
          QB        dropbacks, EPA per dropback, success rate, CPOE, sack rate, average depth of target
          rusher    carries, EPA per rush, success rate, explosive-run rate (10+ yards), yards per carry
          receiver  targets, target share, catch rate, EPA per target, depth of target, air-yard share

CFB   CFBD's play feed names no players, so college is built from the player box lines, which give
      volume and efficiency but not EPA. Season EPA per player comes from CFBD's player PPA endpoint
      and is attached only for the current week, since it is a season-to-date figure.

Ranks are national, among players with enough volume to be meaningful -- a back with four carries is
not ranked against one with two hundred.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from pipeline import storage

STATS = config.TABLES / "stats"
# minimum volume per team game to be ranked
QUALIFY = {"qb": 12.0, "rusher": 5.0, "receiver": 2.5}


def _names(league: str) -> dict:
    pl = storage.read_table(config.TABLES / "ref" / "players" / f"{league}.parquet")
    if pl.empty or "player_id" not in pl.columns:
        return {}
    cols = [c for c in ("full_name", "position") if c in pl.columns]
    return pl.drop_duplicates("player_id").set_index("player_id")[cols].to_dict("index")


def _plays_before(league: str, season: int, week: int) -> pd.DataFrame:
    frames = []
    for wk in range(1, week):
        p = storage.read_table(STATS / "plays" / league / str(season) / f"W{wk:02d}.parquet")
        if not p.empty:
            frames.append(p)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _rank(df: pd.DataFrame, col: str, qualified: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """National rank among qualified players; unqualified players get no rank rather than a misleading one."""
    out = pd.Series(np.nan, index=df.index)
    q = df[qualified & df[col].notna()]
    if not q.empty:
        out.loc[q.index] = q[col].rank(ascending=not higher_is_better, method="min")
    return out


def nfl_players(season: int, week: int) -> pd.DataFrame:
    plays = _plays_before("NFL", season, week)
    if plays.empty:
        return pd.DataFrame()
    games_by_team = plays.groupby("offense_team_id").game_id.nunique().to_dict()
    clean = plays[~plays.is_garbage_time.fillna(False).astype(bool)] if "is_garbage_time" in plays.columns else plays
    names = _names("NFL")
    rows = []

    # quarterbacks: dropbacks are passes, sacks and scrambles; a scramble is credited to the runner
    db = plays[plays.is_dropback.fillna(False).astype(bool)].copy()
    db["qb"] = db.passer_id.where(db.passer_id.notna(), db.rusher_id)
    dbc = clean[clean.is_dropback.fillna(False).astype(bool)].copy()
    dbc["qb"] = dbc.passer_id.where(dbc.passer_id.notna(), dbc.rusher_id)
    for (qb, team), g in db.groupby(["qb", "offense_team_id"]):
        c = dbc[(dbc.qb == qb) & (dbc.offense_team_id == team)]
        att = g[g.air_yards.notna()] if "air_yards" in g.columns else g.iloc[0:0]
        rows.append({"player_id": qb, "team_id": team, "role": "qb", "volume": len(g),
                     "games": games_by_team.get(team, 1),
                     "dropbacks": len(g), "epa_per_dropback": c.ppa.mean() if len(c) else np.nan,
                     "success_rate": c.is_success.mean() if len(c) else np.nan,
                     "cpoe": c.cpoe.mean() if "cpoe" in c.columns and c.cpoe.notna().any() else np.nan,
                     "sack_rate": g.is_sack.fillna(False).astype(bool).mean(),
                     "adot": att.air_yards.mean() if len(att) else np.nan,
                     "pass_yds": g[g.is_complete.fillna(False).astype(bool)].yards_gained.sum()})

    # rushers: designed runs only -- scrambles belong to the passing game
    ru = plays[plays.rusher_id.notna() & ~plays.is_dropback.fillna(False).astype(bool)]
    ruc = clean[clean.rusher_id.notna() & ~clean.is_dropback.fillna(False).astype(bool)]
    for (pid, team), g in ru.groupby(["rusher_id", "offense_team_id"]):
        c = ruc[(ruc.rusher_id == pid) & (ruc.offense_team_id == team)]
        rows.append({"player_id": pid, "team_id": team, "role": "rusher", "volume": len(g),
                     "games": games_by_team.get(team, 1),
                     "carries": len(g), "epa_per_rush": c.ppa.mean() if len(c) else np.nan,
                     "success_rate": c.is_success.mean() if len(c) else np.nan,
                     "explosive_rate": (g.yards_gained >= 10).mean(),
                     "yards_per_carry": g.yards_gained.mean(), "rush_yds": g.yards_gained.sum()})

    # receivers: every targeted pass, with shares of the team's targets and air yards
    tg = plays[plays.receiver_id.notna()]
    tgc = clean[clean.receiver_id.notna()]
    team_targets = tg.groupby("offense_team_id").size()
    team_air = tg.groupby("offense_team_id").air_yards.sum() if "air_yards" in tg.columns else pd.Series(dtype=float)
    for (pid, team), g in tg.groupby(["receiver_id", "offense_team_id"]):
        c = tgc[(tgc.receiver_id == pid) & (tgc.offense_team_id == team)]
        comp = g.is_complete.fillna(False).astype(bool)
        air = g.air_yards.sum() if "air_yards" in g.columns else np.nan
        rows.append({"player_id": pid, "team_id": team, "role": "receiver", "volume": len(g),
                     "games": games_by_team.get(team, 1),
                     "targets": len(g), "target_share": len(g) / team_targets.get(team, np.nan),
                     "receptions": int(comp.sum()), "catch_rate": comp.mean(),
                     "epa_per_target": c.ppa.mean() if len(c) else np.nan,
                     "adot": g.air_yards.mean() if "air_yards" in g.columns else np.nan,
                     "air_yard_share": (air / team_air.get(team, np.nan)) if team_air.get(team) else np.nan,
                     "rec_yds": g[comp].yards_gained.sum()})

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["name"] = df.player_id.map(lambda p: (names.get(p) or {}).get("full_name"))
    df["position"] = df.player_id.map(lambda p: (names.get(p) or {}).get("position"))
    return _add_ranks(df)


def _add_ranks(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    per_game = df.volume / df.games.clip(lower=1)
    rank_cols = {
        "qb": [("epa_per_dropback", True), ("success_rate", True), ("cpoe", True), ("sack_rate", False),
               ("pass_yds", True), ("pass_rating", True), ("epa_pass", True)],
        "rusher": [("epa_per_rush", True), ("success_rate", True), ("explosive_rate", True),
                   ("yards_per_carry", True), ("rush_yds", True), ("epa_rush", True)],
        "receiver": [("target_share", True), ("epa_per_target", True), ("catch_rate", True),
                     ("air_yard_share", True), ("rec_yds", True), ("yards_per_rec", True)],
    }
    for role, cols in rank_cols.items():
        m = df.role == role
        qualified = m & (per_game >= QUALIFY[role])
        df.loc[m, "qualified"] = qualified[m]
        for col, hib in cols:
            if col in df.columns:
                df.loc[m, f"{col}_rank"] = _rank(df, col, qualified, hib)[m]
    return df


def cfb_players(season: int, week: int, attach_epa: bool) -> pd.DataFrame:
    pgs = storage.read_table(STATS / "player_game_stats" / "CFB" / f"{season}.parquet")
    games = storage.read_table(storage.games_path("CFB", season))
    if pgs.empty or games.empty:
        return pd.DataFrame()
    before = set(games[games.week < week].game_id)
    pgs = pgs[pgs.game_id.isin(before)]
    if pgs.empty:
        return pd.DataFrame()
    num = lambda c: pd.to_numeric(pgs[c], errors="coerce") if c in pgs.columns else pd.Series(np.nan, index=pgs.index)
    pgs = pgs.assign(**{c: num(c) for c in ("pass_att", "pass_cmp", "pass_yds", "pass_td", "pass_int",
                                            "rush_att", "rush_yds", "rush_td", "receptions", "rec_yds", "rec_td")})
    games_by_team = pgs.groupby("team_id").game_id.nunique().to_dict()
    agg = pgs.groupby(["player_id", "team_id"]).agg(
        name=("player_name", "last"), pass_att=("pass_att", "sum"), pass_cmp=("pass_cmp", "sum"),
        pass_yds=("pass_yds", "sum"), pass_td=("pass_td", "sum"), pass_int=("pass_int", "sum"),
        rush_att=("rush_att", "sum"), rush_yds=("rush_yds", "sum"), rush_td=("rush_td", "sum"),
        receptions=("receptions", "sum"), rec_yds=("rec_yds", "sum"), rec_td=("rec_td", "sum")).reset_index()
    agg["games"] = agg.team_id.map(games_by_team).fillna(1)
    rows = []
    for r in agg.itertuples():
        if r.pass_att and r.pass_att > 0:
            rating = (8.4 * r.pass_yds + 330 * r.pass_td + 100 * r.pass_cmp - 200 * r.pass_int) / r.pass_att
            rows.append({"player_id": r.player_id, "team_id": r.team_id, "name": r.name, "role": "qb",
                         "volume": r.pass_att, "games": r.games, "pass_att": r.pass_att,
                         "comp_pct": r.pass_cmp / r.pass_att, "pass_yds": r.pass_yds, "pass_td": r.pass_td,
                         "pass_int": r.pass_int, "yards_per_att": r.pass_yds / r.pass_att, "pass_rating": rating})
        if r.rush_att and r.rush_att > 0:
            rows.append({"player_id": r.player_id, "team_id": r.team_id, "name": r.name, "role": "rusher",
                         "volume": r.rush_att, "games": r.games, "carries": r.rush_att,
                         "rush_yds": r.rush_yds, "rush_td": r.rush_td, "yards_per_carry": r.rush_yds / r.rush_att})
        if r.receptions and r.receptions > 0:
            rows.append({"player_id": r.player_id, "team_id": r.team_id, "name": r.name, "role": "receiver",
                         # the college box reports catches, not targets, so volume is receptions here
                         "volume": r.receptions, "games": r.games, "receptions": r.receptions,
                         "rec_yds": r.rec_yds, "rec_td": r.rec_td, "yards_per_rec": r.rec_yds / r.receptions})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if attach_epa:
        ppa = storage.read_table(STATS / "player_ppa" / "CFB" / f"{season}.parquet")
        if not ppa.empty:
            e = ppa.drop_duplicates("player_id").set_index("player_id")
            df["epa_pass"] = df.player_id.map(e.epa_pass)
            df["epa_rush"] = df.player_id.map(e.epa_rush)
    return _add_ranks(df)


def league_players(league: str, season: int, week: int, current_week: bool = False) -> pd.DataFrame:
    return nfl_players(season, week) if league == "NFL" else cfb_players(season, week, attach_epa=current_week)


def team_key_players(table: pd.DataFrame, team_id: str) -> dict:
    """The players who matter most for one team: top passer, three backs, four receivers."""
    if table is None or table.empty:
        return {}
    t = table[table.team_id == team_id]
    out = {}
    for role, n in (("qb", 1), ("rusher", 3), ("receiver", 4)):
        r = t[t.role == role].sort_values("volume", ascending=False).head(n)
        out[role] = [{k: (None if (isinstance(v, float) and np.isnan(v)) else v) for k, v in row.items()}
                     for row in r.to_dict("records")]
    return out
