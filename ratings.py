"""
Roster intelligence (master prompt §11-15). All functions are pure DataFrame -> DataFrame so they run
identically for the live season and for backfilled seasons.

Inputs (tables built by ingest_context / build_roster --what fetch):
  roster_snapshots      who is on the team now (per week)
  player_season_usage   prior season: snaps/usage + production per player per team (both leagues)
  transfers, draft_picks, recruits, recruiting_classes, team_talent, coaches, injuries, depth_charts (NFL)
  player_game_stats     current season QB game rows (for in-season starter detection)

Rules that matter for accuracy:
  * "returning" means the same player_id on the same team_id; a transfer-in is NEW even if productive elsewhere
  * production shares are computed against the prior season's team totals, never against a league average
  * OL returning starts are exact for the NFL (snap counts) and a labeled PROXY for CFB (no OL snaps published)
  * a departing player whose destination is unknown is DEPARTED_UNKNOWN, never assumed drafted or graduated
  * a QB status with no evidence is UNKNOWN with low confidence; last year's QB metrics are never inherited
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config

DEF_POS = {"DL", "EDGE", "LB", "CB", "S"}
SEC_POS = {"CB", "S"}


def _share(prior: pd.DataFrame, col: str, returning_ids: set[str]) -> float | None:
    if col not in prior.columns:
        return None
    v = pd.to_numeric(prior[col], errors="coerce").fillna(0.0)
    tot = float(v.sum())
    if tot <= 0:
        return None
    return float(v[prior.player_id.isin(returning_ids)].sum() / tot)


def _def_production(p: pd.DataFrame) -> pd.Series:
    w = config.DEF_PRODUCTION_WEIGHTS
    out = pd.Series(0.0, index=p.index)
    for col, wt in w.items():
        if col in p.columns:
            out = out + pd.to_numeric(p[col], errors="coerce").fillna(0.0) * wt
    return out


def returning_production(league: str, season: int, week: int, roster_now: pd.DataFrame, usage_prior: pd.DataFrame) -> pd.DataFrame:
    """Position-weighted returning production per team (method = derived_position_weighted)."""
    rows = []
    if roster_now.empty or usage_prior.empty:
        return pd.DataFrame()
    now_by_team = {t: set(s.player_id) for t, s in roster_now.groupby("team_id")}
    up = usage_prior.copy()
    up["def_prod"] = _def_production(up)
    for tid, prior in up.groupby("team_id"):
        cur = now_by_team.get(tid)
        if cur is None:
            continue
        ret = set(prior.player_id) & cur
        pos = (prior["position"] if "position" in prior.columns else pd.Series(None, index=prior.index)).fillna(prior.get("position_raw", pd.Series(index=prior.index))).astype(str)
        ol_prior = prior[pos.isin(["OL", "T", "G", "C", "OT", "OG"])]
        if league == "NFL":
            rp_off = _share(prior, "off_snaps", ret); rp_def = _share(prior, "def_snaps", ret)
            ol_starts = int(pd.to_numeric(ol_prior[ol_prior.player_id.isin(ret)].off_starts, errors="coerce").fillna(0).sum()) if not ol_prior.empty else None
            ol_proxy = False
            pressure_ret = _share(prior, "pressures", ret)
            sec = prior[pos.isin(SEC_POS)]
            sec_ret = _share(sec, "def_snaps", ret) if not sec.empty else None
            rp_rec = _share(prior, "targets", ret)
        else:
            rp_off = _share(prior, "usage_overall", ret)
            rp_def = _share(prior, "def_prod", ret)
            ol_starts = int(ol_prior.player_id.isin(ret).sum()) if not ol_prior.empty else None      # PROXY: OL players retained
            ol_proxy = True
            prior_press = prior.assign(_p=pd.to_numeric(prior.get("qb_hurries", 0), errors="coerce").fillna(0) + pd.to_numeric(prior.get("sacks", 0), errors="coerce").fillna(0))
            pressure_ret = _share(prior_press, "_p", ret)
            sec = prior[pos.isin(SEC_POS)]
            sec_ret = _share(sec, "def_prod", ret) if not sec.empty else None
            rp_rec = _share(prior, "receptions", ret)
        rp_pass = _share(prior, "pass_att", ret)
        rp_rush = _share(prior, "rush_att", ret)
        w = config.RP_POSITION_WEIGHTS
        parts = {"passing": rp_pass, "ol": (ol_starts / max(config.OL_STARTS_FULL[league], 1) if ol_starts is not None else None),
                 "receiving": rp_rec, "rushing": rp_rush, "defense": rp_def}
        avail = {k: v for k, v in parts.items() if v is not None}
        rp_total = float(sum(min(v, 1.0) * w[k] for k, v in avail.items()) / sum(w[k] for k in avail)) if avail else None
        rows.append({"team_id": tid, "season": season, "as_of_week": week, "rp_total": rp_total, "rp_offense": rp_off, "rp_defense": rp_def,
                     "rp_passing": rp_pass, "rp_rushing": rp_rush, "rp_receiving": rp_rec,
                     "ol_starts_returning": ol_starts, "ol_starts_returning_is_proxy": ol_proxy,
                     "def_pressure_returning": pressure_ret, "secondary_snaps_returning": sec_ret,
                     "returning_players": len(ret), "prior_players": int(prior.player_id.nunique()),
                     "method": "derived_position_weighted", "source": "derived", "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat()})
    return pd.DataFrame(rows)


def departures(league: str, season: int, roster_now: pd.DataFrame, usage_prior: pd.DataFrame, draft: pd.DataFrame | None,
               portal: pd.DataFrame | None) -> pd.DataFrame:
    """Per team: prior-season contributors not on the current roster, categorized DRAFT / TRANSFER / DEPARTED_UNKNOWN."""
    if roster_now.empty or usage_prior.empty:
        return pd.DataFrame()
    now = {t: set(s.player_id) for t, s in roster_now.groupby("team_id")}
    on_other_team = {pid: t for t, s in now.items() for pid in s}
    drafted = set(draft.player_id.dropna()) if draft is not None and not draft.empty else set()
    portal_names = set()
    if portal is not None and not portal.empty:
        portal_names = set(zip(portal.from_team_id, portal.player_name.str.lower().str.replace(r"[^a-z]", "", regex=True)))
    up = usage_prior.copy(); up["def_prod"] = _def_production(up)
    rows = []
    for tid, prior in up.groupby("team_id"):
        cur = now.get(tid)
        if cur is None:
            continue
        gone = prior[~prior.player_id.isin(cur)]
        for _, p in gone.iterrows():
            nm = str(p.get("player_name", "") or "").lower()
            nm = "".join(ch for ch in nm if ch.isalpha())
            if p.player_id in drafted:
                cat = "DRAFT"
            elif (tid, nm) in portal_names:
                cat = "TRANSFER"
            elif p.player_id in on_other_team:
                cat = "TRANSFER" if league == "CFB" else "CHANGED_TEAM"
            else:
                cat = "DEPARTED_UNKNOWN"
            rows.append({"team_id": tid, "season": season, "player_id": p.player_id, "player_name": p.get("player_name"),
                         "position": p.get("position"), "category": cat,
                         "prior_usage": p.get("usage_overall") if league == "CFB" else p.get("off_snap_share"),
                         "prior_pass_att": p.get("pass_att"), "prior_rush_att": p.get("rush_att"), "prior_receptions": p.get("receptions"),
                         "prior_def_prod": float(p.def_prod)})
    return pd.DataFrame(rows)


def evaluate_transfers(season: int, portal: pd.DataFrame, roster_now: pd.DataFrame, players: pd.DataFrame,
                       usage_prior: pd.DataFrame) -> pd.DataFrame:
    """Match portal rows to roster players by name + destination, attach prior-season usage/production from origin."""
    if portal is None or portal.empty:
        return pd.DataFrame()
    t = portal.copy()
    key = lambda s: s.astype(str).str.lower().str.replace(r"[^a-z]", "", regex=True)
    ros = roster_now.merge(players[["player_id", "full_name"]], on="player_id", how="left")
    ros["_k"] = key(ros.full_name)
    t["_k"] = key(t.player_name)
    m = t.merge(ros[["_k", "team_id", "player_id"]].rename(columns={"team_id": "to_team_id"}), on=["_k", "to_team_id"], how="left", suffixes=("", "_ros"))
    m["player_id"] = m.player_id_ros.where(m.player_id_ros.notna(), None)
    m = m.drop(columns=["player_id_ros"])
    if not usage_prior.empty:
        up = usage_prior.copy(); up["_k"] = key(up.player_name) if "player_name" in up.columns else None
        up["def_prod"] = _def_production(up)
        cols = ["_k", "team_id", "usage_overall", "pass_att", "pass_yds", "rush_att", "rush_yds", "receptions", "rec_yds", "def_prod"]
        cols = [c for c in cols if c in up.columns]
        m = m.merge(up[cols].rename(columns={"team_id": "from_team_id"}), on=["_k", "from_team_id"], how="left")
        m["prior_season_usage"] = m.get("usage_overall")
        m["prior_production"] = m.apply(lambda r: {k: (None if pd.isna(r.get(k)) else float(r.get(k))) for k in ("pass_att", "pass_yds", "rush_att", "rush_yds", "receptions", "rec_yds", "def_prod")}, axis=1).astype(str)
    m["projected_role"] = np.where(m.prior_season_usage.fillna(0) >= 0.3, "STARTER", np.where(m.prior_season_usage.fillna(0) >= 0.1, "ROTATION", np.where(m.rating.fillna(0) >= 0.9, "STARTER", "DEPTH")))
    return m.drop(columns=["_k", "usage_overall"], errors="ignore")


def talent_scores(season: int, classes: pd.DataFrame, talent: pd.DataFrame, transfers: pd.DataFrame | None) -> pd.DataFrame:
    """Roster talent (CFB): weighted last-4 recruiting classes + talent composite + transfer class quality, scaled 0..1 by rank."""
    if classes.empty:
        return pd.DataFrame()
    wts = config.RECRUIT_CLASS_WEIGHTS
    c = classes[classes.season.between(season - 3, season)].copy()
    c["w"] = c.season.map(lambda y: wts.get(season - y, 0.0))
    c["pts_w"] = c.class_points.fillna(0) * c.w
    agg = c.groupby("team_id").agg(class_points_4yr=("pts_w", "sum"), blue_chip_ratio_4yr=("blue_chip_ratio", "mean"),
                                   avg_rating_4yr=("avg_rating", "mean"), five_stars_4yr=("five_stars", "sum"), four_stars_4yr=("four_stars", "sum")).reset_index()
    if not talent.empty:
        agg = agg.merge(talent[talent.season == season][["team_id", "talent_composite", "talent_rank"]], on="team_id", how="left")
    if transfers is not None and not transfers.empty:
        tin = transfers[transfers.to_team_id.notna()].groupby("to_team_id").agg(transfer_in_rating_sum=("rating", "sum"), transfers_in=("transfer_id", "count")).reset_index().rename(columns={"to_team_id": "team_id"})
        agg = agg.merge(tin, on="team_id", how="left")
        agg["transfer_class_rank"] = agg.transfer_in_rating_sum.rank(ascending=False, method="min").astype("Int64")
    agg["season"] = season
    agg["talent_score"] = agg.class_points_4yr.rank(pct=True)
    return agg


def qb_status(league: str, season: int, week: int, cutoff: pd.Timestamp, team_ids: list[str], roster_now: pd.DataFrame,
              players: pd.DataFrame, pgs_all: pd.DataFrame, usage_prior: pd.DataFrame, injuries: pd.DataFrame,
              depth_nfl: pd.DataFrame | None, transfers: pd.DataFrame | None, recruits: pd.DataFrame | None) -> pd.DataFrame:
    """Projected starting QB per team as of cutoff, with evidence, flags, confidence and career line."""
    pgs = pgs_all[pd.to_datetime(pgs_all.effective_at, utc=True) < cutoff] if not pgs_all.empty else pgs_all
    cur = pgs[pgs.season == season] if not pgs.empty and "season" in pgs.columns else pgs
    prior = usage_prior[usage_prior.position.fillna(usage_prior.get("position_raw", "")).astype(str).str.upper().isin(["QB"])] if not usage_prior.empty else usage_prior
    inj_out = set()
    if injuries is not None and not injuries.empty:
        i = injuries[(injuries.season == season) & (injuries.week == week) & injuries.status.isin(["OUT", "IR", "DOUBTFUL"])]
        if "player_id" in i.columns:
            inj_out = set(i.player_id.dropna())
    rows = []
    for tid in team_ids:
        ros_qbs = roster_now[(roster_now.team_id == tid) & (roster_now.position == "QB")]
        cand, basis, conf, flags = None, "UNKNOWN", 0.35, []
        # 1) in-season: last game's primary passer for this team
        t_cur = cur[cur.team_id == tid] if not cur.empty else cur
        if not t_cur.empty:
            last_gid = t_cur.sort_values("effective_at").game_id.iloc[-1]
            lg = t_cur[t_cur.game_id == last_gid].sort_values("pass_att", ascending=False)
            cand, basis, conf = lg.player_id.iloc[0], "last_game_starter", 0.9
        # 2) preseason: NFL depth chart QB1 / CFB prior starter or transfer
        if cand is None:
            if league == "NFL" and depth_nfl is not None and not depth_nfl.empty:
                d = depth_nfl[(depth_nfl.team_id == tid) & (depth_nfl.slot == "QB1") & (depth_nfl.rank_in_slot == 1)]
                if not d.empty:
                    cand, basis, conf = d.player_id.iloc[0], "depth_chart", 0.8
            if cand is None and not prior.empty:
                p_same = prior[(prior.team_id == tid) & prior.player_id.isin(ros_qbs.player_id)].sort_values("pass_att", ascending=False)
                if not p_same.empty and p_same.pass_att.iloc[0] and p_same.pass_att.iloc[0] >= 50:
                    cand, basis, conf = p_same.player_id.iloc[0], "prior_season_starter", 0.7
                else:
                    p_in = prior[prior.player_id.isin(ros_qbs.player_id) & (prior.team_id != tid)].sort_values("pass_att", ascending=False)
                    if not p_in.empty and p_in.pass_att.iloc[0] and p_in.pass_att.iloc[0] >= 100:
                        cand, basis, conf = p_in.player_id.iloc[0], "transfer_prior_production", 0.55
        # 3) injury replacement
        if cand is not None and cand in inj_out:
            flags.append("STARTER_INJURED")
            alt = None
            if not t_cur.empty:
                others = t_cur[t_cur.player_id != cand].groupby("player_id").pass_att.sum().sort_values(ascending=False)
                alt = others.index[0] if len(others) else None
            if alt is None and league == "NFL" and depth_nfl is not None and not depth_nfl.empty:
                d = depth_nfl[(depth_nfl.team_id == tid) & (depth_nfl.slot == "QB1") & (depth_nfl.rank_in_slot == 2)]
                alt = d.player_id.iloc[0] if not d.empty else None
            cand, basis, conf = alt, "injury_replacement", 0.5 if alt else 0.2
            flags.append("INJURY_REPLACEMENT")
        # status flags
        prior_same = prior[(prior.team_id == tid)].sort_values("pass_att", ascending=False) if not prior.empty else prior
        prior_starter = prior_same.player_id.iloc[0] if not prior_same.empty else None
        if cand is not None:
            if cand == prior_starter:
                flags.append("RETURNING_STARTER")
            elif not prior.empty and cand in set(prior[prior.team_id != tid].player_id):
                flags.append("TRANSFER_STARTER" if league == "CFB" else "NEW_TEAM_STARTER")
            elif not prior.empty and cand in set(prior[prior.team_id == tid].player_id):
                flags.append("NEW_STARTER_SAME_TEAM")
            else:
                flags.append("ROOKIE_OR_NO_PRIOR_DATA")
        else:
            flags.append("UNKNOWN_STARTER")
        # career line from our tables (2021+ only)
        career = pgs[pgs.player_id == cand] if cand is not None and not pgs.empty else pd.DataFrame()
        starts = int((career.pass_att >= 10).sum()) if not career.empty else 0
        att = float(career.pass_att.sum()) if not career.empty else 0.0
        rows.append({"team_id": tid, "season": season, "week": week, "as_of_ts": cutoff.isoformat(), "player_id": cand,
                     "player_name": players.set_index("player_id").full_name.get(cand) if cand and not players.empty else None,
                     "projection_basis": basis, "confidence": conf, "flags": ",".join(flags),
                     "career_games_10att": starts, "career_att": att,
                     "career_cmp_pct": float(career.pass_cmp.sum() / att) if att else None,
                     "career_ypa": float(career.pass_yds.sum() / att) if att else None,
                     "career_td": float(career.pass_td.sum()) if att else None, "career_int": float(career.pass_int.sum()) if att else None,
                     "career_ppa_dropback": float((career.ppa_dropback * career.dropbacks).sum() / career.dropbacks.sum()) if not career.empty and career.dropbacks.sum() else None,
                     "season_att": float(t_cur[t_cur.player_id == cand].pass_att.sum()) if cand and not t_cur.empty else 0.0,
                     "prior_season_starter_id": prior_starter, "prior_starter_returning": bool(prior_starter in set(ros_qbs.player_id)) if prior_starter else False})
    return pd.DataFrame(rows)


_SLOT_PLAN = [("QB", ["QB1"], 3), ("RB", ["RB1"], 3), ("WR", ["WR1", "WR2", "WR3"], 2), ("TE", ["TE1"], 2), ("OL", ["LT", "LG", "C", "RG", "RT"], 2),
              ("EDGE", ["EDGE1", "EDGE2"], 2), ("DL", ["DL1", "DL2"], 2), ("LB", ["LB1", "LB2"], 2), ("CB", ["CB1", "CB2", "NB"], 2), ("S", ["S1", "S2"], 2)]


def project_depth_chart_cfb(season: int, week: int, roster_now: pd.DataFrame, usage_prior: pd.DataFrame, usage_cur: pd.DataFrame | None,
                            transfers: pd.DataFrame | None, recruits: pd.DataFrame | None) -> pd.DataFrame:
    """Projected depth chart (is_projected=True). Ordering: current-season usage > prior-season usage (any team) >
    portal rating > recruit rating > roster order. OL slots are filled in usage order, not by true position."""
    if roster_now.empty:
        return pd.DataFrame()
    ros = roster_now.copy()
    # every lookup is collapsed to one value per player: portal and recruit feeds can list a player twice
    uc = usage_cur.groupby("player_id").usage_overall.max() if usage_cur is not None and not usage_cur.empty else pd.Series(dtype=float)
    up = usage_prior.groupby("player_id").usage_overall.max() if not usage_prior.empty and "usage_overall" in usage_prior.columns else pd.Series(dtype=float)
    tr = (transfers.dropna(subset=["player_id"]).groupby("player_id").rating.max()
          if transfers is not None and not transfers.empty and "player_id" in transfers.columns else pd.Series(dtype=float))
    rc = pd.Series(dtype=float)
    if recruits is not None and not recruits.empty and "athlete_id" in recruits.columns:
        r = recruits.dropna(subset=["athlete_id"]).copy()
        r["pid"] = "CFB_P_" + r.athlete_id.astype(int).astype(str)
        rc = r.groupby("pid").rating.max()
    ros["u_cur"] = ros.player_id.map(uc); ros["u_prior"] = ros.player_id.map(up); ros["portal"] = ros.player_id.map(tr); ros["recruit"] = ros.player_id.map(rc)
    rows = []
    for tid, team in ros.groupby("team_id"):
        for pos, slots, depth in _SLOT_PLAN:
            pool = team[team.position == pos].copy()
            if pool.empty:
                continue
            pool["score"] = (pool.u_cur.fillna(0) * 1000 + pool.u_prior.fillna(0) * 100 + pool.portal.fillna(0) * 10 + pool.recruit.fillna(0))
            pool = pool.sort_values(["score", "years_exp"], ascending=[False, False])
            ordered = pool.player_id.tolist()
            basis_of = lambda r: ("usage_current" if pd.notna(r.u_cur) and r.u_cur > 0 else "usage_prior" if pd.notna(r.u_prior) and r.u_prior > 0
                                  else "portal_rank" if pd.notna(r.portal) else "recruit_rank" if pd.notna(r.recruit) else "roster_order")
            i = 0
            for rank in range(1, depth + 1):
                for slot in slots:
                    if i >= len(ordered):
                        break
                    r = pool.iloc[i]
                    b = basis_of(r)
                    rows.append({"team_id": tid, "season": season, "week": week, "slot": slot, "player_id": r.player_id, "rank_in_slot": rank,
                                 "is_projected": True, "projection_basis": b,
                                 "confidence": {"usage_current": 0.8, "usage_prior": 0.65, "portal_rank": 0.5, "recruit_rank": 0.35, "roster_order": 0.25}[b],
                                 "source": "derived", "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat()})
                    i += 1
    return pd.DataFrame(rows)


def continuity(league: str, season: int, rp: pd.DataFrame, qb: pd.DataFrame, coaches: pd.DataFrame, transfers: pd.DataFrame | None) -> pd.DataFrame:
    """Roster continuity index 0..1 used to scale the early-season prior (Phase 8)."""
    if rp.empty:
        return pd.DataFrame()
    w = config.CONTINUITY_WEIGHTS[league]
    hc_same = {}
    if coaches is not None and not coaches.empty:
        hc = coaches[coaches.role == "HC"]
        for tid, g in hc.groupby("team_id"):
            now = set(g[g.season == season].coach_id); prev = set(g[g.season == season - 1].coach_id)
            hc_same[tid] = bool(now & prev) if now and prev else None
    qb_cont = {}
    if qb is not None and not qb.empty:
        for _, r in qb.iterrows():
            f = str(r["flags"])          # r["flags"], not r.flags: Series.flags is a pandas attribute
            qb_cont[r.team_id] = 1.0 if "RETURNING_STARTER" in f else 0.5 if ("TRANSFER_STARTER" in f or "NEW_TEAM_STARTER" in f) else 0.25 if "NEW_STARTER_SAME_TEAM" in f else 0.0
    tin = transfers.groupby("to_team_id").size() if transfers is not None and not transfers.empty else pd.Series(dtype=int)
    rows = []
    for _, r in rp.iterrows():
        parts = {"rp_total": r.rp_total, "qb": qb_cont.get(r.team_id), "hc": (1.0 if hc_same.get(r.team_id) else 0.0) if hc_same.get(r.team_id) is not None else None,
                 "portal_churn": (1 - min(tin.get(r.team_id, 0) / config.PORTAL_CHURN_FULL, 1.0)) if league == "CFB" else None}
        avail = {k: v for k, v in parts.items() if v is not None and k in w}
        idx = float(sum(v * w[k] for k, v in avail.items()) / sum(w[k] for k in avail)) if avail else None
        rows.append({"team_id": r.team_id, "season": season, "continuity_index": idx, **{f"c_{k}": v for k, v in parts.items()},
                     "hc_changed": (not hc_same[r.team_id]) if hc_same.get(r.team_id) is not None else None})
    return pd.DataFrame(rows)
