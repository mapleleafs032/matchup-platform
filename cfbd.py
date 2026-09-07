"""
As-of team metrics (Phase 3 §5). ONE function builds metrics for production and for backtesting:

    build_week(league, season, week) ->
        team_metrics_asof rows for every team playing that week, every window, RAW and OPP_ADJ
        team_ratings rows for that week

Look-ahead protection is structural:
  * every team-game row carries effective_at (game end); a window only sees rows with effective_at < cutoff
  * cutoff for raw windows  = that game's kickoff_utc
  * cutoff for the ridge fit = the earliest kickoff of the week (so a Thursday result never adjusts Saturday's
    opponents in the same week; conservative by design, documented in the row as ratings_cutoff)
  * the previous season's prior is computed with cutoff = season end and is never re-fit with current data

Windows: SEASON LAST3 LAST5 HOME AWAY CONF NONCONF DAY NIGHT FAV DOG VS_RANKED BLEND
BLEND = config.RECENCY_WEIGHTS mix of SEASON / LAST5 / LAST3 (metric by metric; NULL if any part NULL).
Early season: SEASON and BLEND are blended with the prior-season OPP_ADJ SEASON values using
config.PRIOR_WEIGHT_BY_WEEK[league]; prior_blend_weight is stored on the row.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config
from pipeline import storage, ratings
from pipeline.metric_registry import REGISTRY

STATS = config.TABLES / "stats"
WINDOWS = ["SEASON", "LAST3", "LAST5", "HOME", "AWAY", "CONF", "NONCONF", "DAY", "NIGHT", "FAV", "DOG", "VS_RANKED", "BLEND"]
_D_COLS = ["total_yards", "plays", "rush_yds", "rush_att", "pass_yds", "pass_att", "dropbacks", "third_down_conv", "third_down_att",
           "fourth_down_conv", "fourth_down_att", "sacks_taken", "turnovers"]


# ---- 1. assemble team-game rows -------------------------------------------------------
def load_team_games(league: str, season: int) -> pd.DataFrame:
    box = storage.read_table(STATS / "team_game_stats" / league / f"{season}.parquet")
    adv = storage.read_table(STATS / "team_game_advanced" / league / f"{season}.parquet")
    games = storage.read_table(storage.games_path(league, season))
    if box.empty or adv.empty or games.empty:
        return pd.DataFrame()
    adv = adv[adv.is_garbage_filtered == config.USE_GARBAGE_FILTERED].drop(columns=["is_garbage_filtered", "source", "retrieved_at", "effective_at", "opponent_id"], errors="ignore")
    # opponent's box columns as d_*
    opp = box[["game_id", "team_id"] + _D_COLS].rename(columns={"team_id": "opponent_id", **{c: f"d_{c}" for c in _D_COLS}})
    tg = box.merge(opp, on=["game_id", "opponent_id"], how="left").merge(adv, on=["game_id", "team_id"], how="left")
    g = games[["game_id", "week", "season_type", "kickoff_utc", "neutral_site", "conference_game", "is_fcs_game", "venue_id", "home_team_id"]]
    tg = tg.merge(g, on="game_id", how="inner")
    tg["kickoff_utc"] = pd.to_datetime(tg.kickoff_utc, utc=True)
    tg["effective_at"] = pd.to_datetime(tg.effective_at, utc=True)
    tg["neutral_site"] = tg.neutral_site.fillna(False).astype(bool)
    tg["is_fcs_game"] = tg.is_fcs_game.fillna(False).astype(bool)
    tg["weight"] = np.where(tg.is_fcs_game, config.FCS_GAME_WEIGHT, 1.0)
    # derived per-game values
    tg["scoring_margin"] = tg.points - tg.points_allowed
    tg["points_per_game"] = tg.points; tg["points_allowed_per_game"] = tg.points_allowed
    ypp_o = tg.total_yards / tg.plays.replace(0, np.nan); ypp_d = tg.d_total_yards / tg.d_plays.replace(0, np.nan)
    tg["net_yards_per_play"] = ypp_o - ypp_d
    tg["rush_yds_per_game"] = tg.rush_yds; tg["opp_rush_yds_per_game"] = tg.d_rush_yds
    tg["pass_yds_per_game"] = tg.pass_yds; tg["opp_pass_yds_per_game"] = tg.d_pass_yds
    tg["turnover_margin"] = tg.takeaways - tg.turnovers
    tg["giveaways_per_game"] = tg.turnovers; tg["takeaways_per_game"] = tg.takeaways
    tg["plays_per_game"] = tg.plays
    tg["yards_per_play_off"] = ypp_o
    # local kickoff hour -> DAY/NIGHT
    venues = storage.read_table(config.TABLES / "ref" / "venues.parquet")
    tz = dict(zip(venues.venue_id, venues.timezone)) if not venues.empty else {}
    def local_hour(row):
        if pd.isna(row.kickoff_utc):
            return None
        z = tz.get(row.venue_id) or ("America/New_York" if league == "NFL" else "America/Chicago")
        try:
            return row.kickoff_utc.astimezone(ZoneInfo(z)).hour
        except Exception:
            return None
    tg["local_hour"] = tg.apply(local_hour, axis=1)
    tg["is_night"] = tg.local_hour.map(lambda h: None if h is None else h >= config.NIGHT_GAME_LOCAL_HOUR)
    tg = _attach_favorite(tg, league, season)
    tg = _attach_opp_ranked(tg, league, season)
    return tg.sort_values(["team_id", "kickoff_utc"]).reset_index(drop=True)


def _attach_favorite(tg: pd.DataFrame, league: str, season: int) -> pd.DataFrame:
    """is_favorite from the closing line (NFL backfill) or the last market snapshot before kickoff."""
    tg["is_favorite"] = None
    cl = storage.read_table(config.TABLES / "market" / "closing_lines" / league / f"{season}.parquet")
    spread = {}
    if not cl.empty:
        spread.update(dict(zip(cl.game_id, cl.spread_home)))
    snap_dir = config.TABLES / "market" / "snapshots" / league / str(season)
    if snap_dir.exists():
        parts = [pd.read_csv(p) for p in sorted(snap_dir.glob("*.csv"))]
        if parts:
            s = pd.concat(parts, ignore_index=True)
            s = s[s.spread_home.notna()]
            s["retrieved_at"] = pd.to_datetime(s.retrieved_at, utc=True)
            kick = tg.drop_duplicates("game_id").set_index("game_id").kickoff_utc
            s = s[s.game_id.map(kick).notna()]
            s = s[s.retrieved_at < s.game_id.map(kick)]
            for gid, grp in s.groupby("game_id"):
                if gid not in spread:
                    last = grp.sort_values("retrieved_at").iloc[-1]
                    spread[gid] = last.spread_home
    if spread:
        sh = tg.game_id.map(spread)
        home_fav = sh < 0
        tg["is_favorite"] = np.where(sh.isna(), None, np.where(tg.is_home, home_fav, ~home_fav))
    return tg


def _attach_opp_ranked(tg: pd.DataFrame, league: str, season: int) -> pd.DataFrame:
    tg["opp_ranked"] = None
    if league != "CFB":
        return tg
    rk = storage.read_table(config.TABLES / "context" / "rankings" / f"{season}.parquet")
    if rk.empty:
        return tg
    ap = rk[rk.poll == "AP"]
    ranked = set(zip(ap.week, ap.team_id))
    tg["opp_ranked"] = [((w, o) in ranked) for w, o in zip(tg.week, tg.opponent_id)]
    return tg


# ---- 2. windows ---------------------------------------------------------------------------
def window_rows(team_rows: pd.DataFrame, window: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    r = team_rows[team_rows.effective_at < cutoff]
    if window == "SEASON" or window == "BLEND":
        return r
    if window == "LAST3":
        return r.tail(3)
    if window == "LAST5":
        return r.tail(5)
    if window == "HOME":
        return r[r.is_home & ~r.neutral_site]
    if window == "AWAY":
        return r[~r.is_home & ~r.neutral_site]
    if window == "CONF":
        return r[r.conference_game == True]      # noqa: E712
    if window == "NONCONF":
        return r[r.conference_game == False]     # noqa: E712
    if window == "DAY":
        return r[r.is_night == False]            # noqa: E712
    if window == "NIGHT":
        return r[r.is_night == True]             # noqa: E712
    if window == "FAV":
        return r[r.is_favorite == True]          # noqa: E712
    if window == "DOG":
        return r[r.is_favorite == False]         # noqa: E712
    if window == "VS_RANKED":
        return r[r.opp_ranked == True]           # noqa: E712
    raise ValueError(window)


# ---- 3. aggregation per registry --------------------------------------------------------
_PLAN = []   # compiled registry: (key, kind, a, b)
for _, _m in REGISTRY.iterrows():
    _agg = _m["agg"]
    if _agg == "mean":
        _PLAN.append((_m["metric_key"], "mean", _m["metric_key"], None))
    elif _agg.startswith("wmean:"):
        _PLAN.append((_m["metric_key"], "wmean", _m["metric_key"], _agg.split(":")[1]))
    elif _agg.startswith("ratio:"):
        _n, _d = _agg.split(":")[1].split("/")
        _PLAN.append((_m["metric_key"], "ratio", _n, _d))
_NEEDED = sorted({c for _, k, a, b in _PLAN for c in (a, b) if c} | {f"{k}_adj" for k, _, _, _ in _PLAN})


def aggregate(rows: pd.DataFrame, adjusted: bool = False) -> dict[str, tuple[float | None, int]]:
    """Returns {metric_key: (value, n)} following each metric's agg rule. NULL when no data. Vectorized."""
    if rows.empty:
        return {k: (None, 0) for k, _, _, _ in _PLAN}
    cols = {c: pd.to_numeric(rows[c], errors="coerce").to_numpy(dtype=float) for c in _NEEDED if c in rows.columns}
    w_all = rows.weight.to_numpy(dtype=float)
    out = {}
    for key, kind, a, b in _PLAN:
        acol = f"{key}_adj" if (adjusted and f"{key}_adj" in cols) else a
        if acol not in cols:
            out[key] = (None, 0); continue
        v = cols[acol]
        if kind == "mean":
            w = w_all
        elif kind == "wmean":
            w = (np.nan_to_num(cols[b]) if b in cols else np.ones_like(v)) * w_all
        else:  # ratio
            if b not in cols:
                out[key] = (None, 0); continue
            d = cols[b]
            mask = ~np.isnan(v) & ~np.isnan(d) & (d > 0)
            n = int(mask.sum())
            out[key] = ((float((v[mask] * w_all[mask]).sum() / (d[mask] * w_all[mask]).sum()), n) if n else (None, 0))
            continue
        mask = ~np.isnan(v) & (w > 0)
        n = int(mask.sum())
        out[key] = ((float((v[mask] * w[mask]).sum() / w[mask].sum()), n) if n else (None, 0))
    return out


def blend_values(season_v: dict, last5_v: dict, last3_v: dict) -> dict:
    wts = config.RECENCY_WEIGHTS
    out = {}
    for k in season_v:
        s, l5, l3 = season_v[k][0], last5_v[k][0], last3_v[k][0]
        if s is None:
            out[k] = (None, 0); continue
        if l5 is None or l3 is None:      # not enough games: fall back to season value
            out[k] = (s, season_v[k][1]); continue
        out[k] = (wts["SEASON"] * s + wts["LAST5"] * l5 + wts["LAST3"] * l3, season_v[k][1])
    return out


def prior_weight(league: str, week: int) -> float:
    sched = config.PRIOR_WEIGHT_BY_WEEK[league]
    return float(sched.get(week, sched["default"]))


def apply_prior(values: dict, prior: dict | None, w: float) -> tuple[dict, list[str]]:
    flags = []
    if prior is None or w <= 0:
        if w > 0:
            flags.append("PRIOR_MISSING")
        return values, flags
    out = {}
    for k, (v, n) in values.items():
        pv = prior.get(k)
        if pv is None:
            out[k] = (v, n); continue
        if v is None:
            out[k] = (pv, 0); continue
        out[k] = (w * pv + (1 - w) * v, n)
    flags.append("PRIOR_BLENDED")
    return out, flags


# ---- 4. build one week ---------------------------------------------------------------------
def build_week(league: str, season: int, week: int, tg: pd.DataFrame | None = None, prior_season_values: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (team_metrics_asof rows, team_ratings rows) for every team with a game in `week`."""
    games = storage.read_table(storage.games_path(league, season))
    wk = games[(games.week == week) & (games.season_type == "REG") & games.kickoff_utc.notna()].copy()
    if wk.empty:
        return pd.DataFrame(), pd.DataFrame()
    wk["kickoff_utc"] = pd.to_datetime(wk.kickoff_utc, utc=True)
    if tg is None:
        tg = load_team_games(league, season)
    ratings_cutoff = wk.kickoff_utc.min()
    rat, fits = ratings.build_ratings(tg, league, season, week, ratings_cutoff) if not tg.empty else (pd.DataFrame(), {})
    tg_adj = ratings.adjust_game_values(tg, fits) if not tg.empty else tg
    by_team = {t: sub for t, sub in tg_adj.groupby("team_id")} if not tg_adj.empty else {}
    pw = prior_weight(league, week)
    built_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for _, g in wk.iterrows():
        for tid in (g.home_team_id, g.away_team_id):
            if tid.startswith("CFB_FCS"):
                continue
            team_rows = by_team.get(tid, tg_adj.iloc[0:0] if not tg_adj.empty else pd.DataFrame())
            cutoff = g.kickoff_utc
            for adjusted in (False, True):
                vals = {w: aggregate(window_rows(team_rows, w, cutoff), adjusted) if not team_rows.empty else {k: (None, 0) for k in REGISTRY.metric_key} for w in WINDOWS if w != "BLEND"}
                vals["BLEND"] = blend_values(vals["SEASON"], vals["LAST5"], vals["LAST3"])
                for w in WINDOWS:
                    v = vals[w]
                    flags: list[str] = []
                    pbw = 0.0
                    if adjusted and w in ("SEASON", "BLEND") and pw > 0:
                        # the prior is last season's OPP_ADJ values, so it is only blended into OPP_ADJ rows
                        prior = (prior_season_values or {}).get(tid)
                        v, f = apply_prior(v, prior, pw); flags += f
                        pbw = pw if prior else 0.0
                    n_games = int(len(window_rows(team_rows, w, cutoff))) if not team_rows.empty else 0
                    if n_games < config.LOW_SAMPLE_GAMES[league]:
                        flags.append("LOW_SAMPLE")
                    if not team_rows.empty and window_rows(team_rows, w, cutoff).is_fcs_game.any():
                        flags.append("FCS_OPP_IN_WINDOW")
                    rows.append({
                        "team_id": tid, "season": season, "as_of_game_id": g.game_id, "as_of_ts": cutoff.isoformat(),
                        "ratings_cutoff": ratings_cutoff.isoformat(), "window": w, "games_n": n_games,
                        "is_garbage_filtered": config.USE_GARBAGE_FILTERED, "adjustment": "OPP_ADJ" if adjusted else "RAW",
                        "_values": v, "prior_blend_weight": pbw, "quality_flags": flags,
                        "build_version": config.PIPELINE_VERSION, "built_at": built_at,
                    })
    out = pd.DataFrame(rows)
    out = _rank_and_pack(out, league)
    return out, rat


def _rank_and_pack(df: pd.DataFrame, league: str) -> pd.DataFrame:
    """Ranks/percentiles within (window, adjustment) across all teams in this build; packs the JSON."""
    if df.empty:
        return df
    hib = dict(zip(REGISTRY.metric_key, REGISTRY.higher_is_better))
    min_n = dict(zip(REGISTRY.metric_key, REGISTRY.min_sample_n))
    packed = [None] * len(df)
    quality = [0.0] * len(df)
    for (w, adj), grp in df.groupby(["window", "adjustment"]):
        idx = grp.index.tolist()
        for key in REGISTRY.metric_key:
            vals = np.array([grp.at[i, "_values"][key][0] if grp.at[i, "_values"][key][0] is not None else np.nan for i in idx], dtype=float)
            valid = ~np.isnan(vals)
            ranks = np.full(len(vals), np.nan); pcts = np.full(len(vals), np.nan)
            if valid.sum() >= 2:
                v = vals[valid] if hib[key] else -vals[valid]
                order = pd.Series(v).rank(ascending=False, method="min").to_numpy()
                ranks[valid] = order
                pcts[valid] = 1 - (order - 1) / max(valid.sum() - 1, 1)
            for j, i in enumerate(idx):
                if packed[i] is None:
                    packed[i] = {}
                val, n = grp.at[i, "_values"][key]
                packed[i][key] = None if val is None else {"v": round(float(val), 4), "rank": int(ranks[j]) if not np.isnan(ranks[j]) else None,
                                                             "pct": round(float(pcts[j]), 3) if not np.isnan(pcts[j]) else None,
                                                             "n": int(n), "low_n": bool(n < min_n[key]), "adj": adj}
    for i in range(len(df)):
        m = packed[i] or {}
        coverage = sum(1 for k in m if m[k] is not None) / max(len(m), 1)
        games_n = int(df.at[i, "games_n"])
        sample = min(games_n / config.QUALITY_TARGET_GAMES[league], 1.0)
        quality[i] = round(0.6 * sample + 0.4 * coverage, 3)
    df = df.copy()
    df["metrics"] = [json.dumps(p) for p in packed]
    df["data_quality"] = quality
    df["quality_flags"] = df.quality_flags.map(lambda f: ",".join(f))
    return df.drop(columns=["_values"])


def prior_season_values(league: str, prior_season: int) -> dict | None:
    """Final OPP_ADJ SEASON metrics of the previous season, per team: {team_id: {metric_key: value}}."""
    tg = load_team_games(league, prior_season)
    if tg.empty:
        return None
    cutoff = tg.effective_at.max() + pd.Timedelta(days=1)
    _, fits = ratings.build_ratings(tg, league, prior_season, 99, cutoff)
    tg_adj = ratings.adjust_game_values(tg, fits)
    out = {}
    for tid, sub in tg_adj.groupby("team_id"):
        out[tid] = {k: v for k, (v, n) in aggregate(sub[sub.effective_at < cutoff], adjusted=True).items()}
    return out
