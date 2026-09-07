"""
Expected-score model (master prompt §32-35, §63-67).

Two ridge regressions per league, fit on 2021-2025 with walk-forward evaluation:
    margin_home = f(matchup edge_raw features, blended rating diff, home-field indicator)
    total       = g(both teams' blended scoring/efficiency/pace metrics, weather)
    proj_home = (total + margin) / 2 ; proj_away = (total - margin) / 2
    win_prob_home = Phi(margin / sigma), sigma = residual SD on training data

Why this shape:
  * the matchup engine's edge_raw values are the model's features, so every prediction decomposes into
    per-category contributions (coefficient x edge_raw) for the "Why?" panel (§63) — the weights the
    matchup engine displays are exactly these fitted coefficients (§35)
  * ridge shrinkage handles the correlated categories (§67); lambda is chosen by K-fold CV inside training
  * the home-field coefficient IS the league HFA, learned from data (§69)
  * the market is never a feature, so model-vs-market comparison is meaningful (§25)
  * no look-ahead: features are built from as-of tables; training uses only seasons before the evaluated one
"""
from __future__ import annotations
import glob
import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from pipeline import storage
from pipeline.matchup_engine import CATEGORIES
from pipeline import asof

AN = config.TABLES / "analytics"
MODEL_DIR = config.TABLES / "model"
MARGIN_FEATURES = [c for c in CATEGORIES if c not in ("HOME_FIELD",)] + ["home_field", "rating_diff_blend"]
TOTAL_KEYS = ["points_per_game", "points_allowed_per_game", "off_ppa_play", "def_ppa_play", "plays_per_game", "off_sec_per_play", "off_explosive_play_rate", "def_explosive_play_rate_allowed"]
TOTAL_FEATURES = [f"{side}_{k}" for side in ("home", "away") for k in TOTAL_KEYS] + ["wind_mph", "indoor", "prior_total_league"]


# ---- feature assembly -------------------------------------------------------------------
def _prior_final_ratings(league: str, season: int) -> pd.Series:
    r = storage.read_table(AN / "team_ratings" / league / f"{season - 1}.parquet")
    if r.empty:
        return pd.Series(dtype=float)
    last = r[r.as_of_week == r.as_of_week.max()]
    return last.set_index("team_id").rating_overall


def week_features(league: str, season: int, week: int) -> pd.DataFrame:
    """One row per game (home perspective) with margin + total features. Missing values -> NaN (imputed at fit/predict)."""
    edges = storage.read_table(AN / "matchup_edges" / league / str(season) / f"W{week:02d}.parquet")
    if edges.empty:
        return pd.DataFrame()
    piv = edges.pivot_table(index="game_id", columns="category", values="edge_raw", aggfunc="first")
    piv = piv.reindex(columns=CATEGORIES)
    hf = edges[edges.category == "HOME_FIELD"].set_index("game_id").edge_raw
    piv["home_field"] = hf
    games = storage.read_table(storage.games_path(league, season)).set_index("game_id")
    piv["home_team_id"] = games.home_team_id; piv["away_team_id"] = games.away_team_id
    piv["kickoff_utc"] = games.kickoff_utc; piv["season"] = season; piv["week"] = week; piv["league"] = league
    # blended rating diff: prior-season final rating and current as-of rating, mixed by the early-season prior schedule
    rat = storage.read_table(AN / "team_ratings" / league / f"{season}.parquet")
    cur = rat[rat.as_of_week == week].set_index("team_id").rating_overall if not rat.empty and (rat.as_of_week == week).any() else pd.Series(dtype=float)
    prior = _prior_final_ratings(league, season)
    w = asof.prior_weight(league, week) if not prior.empty else 0.0
    def blend(t):
        c = cur.get(t); p = prior.get(t)
        if c is None or pd.isna(c):
            return p if (p is not None and not pd.isna(p)) else np.nan
        if p is None or pd.isna(p):
            return c
        return w * p + (1 - w) * c
    piv["rating_diff_blend"] = [blend(h) - blend(a) if not (pd.isna(blend(h)) or pd.isna(blend(a))) else np.nan for h, a in zip(piv.home_team_id, piv.away_team_id)]
    piv["prior_weight"] = w
    # total features from team_metrics_asof BLEND / OPP_ADJ (prior-blended early season)
    m = storage.read_table(AN / "team_metrics_asof" / league / str(season) / f"W{week:02d}.parquet")
    vals = {}
    if not m.empty:
        sub = m[(m.window == "BLEND") & (m.adjustment == "OPP_ADJ")]
        for _, r in sub.iterrows():
            d = json.loads(r.metrics)
            vals[(r.team_id, r.as_of_game_id)] = {k: (d[k]["v"] if d.get(k) else np.nan) for k in TOTAL_KEYS}
    for side, col in (("home", "home_team_id"), ("away", "away_team_id")):
        for k in TOTAL_KEYS:
            piv[f"{side}_{k}"] = [vals.get((t, gid), {}).get(k, np.nan) for t, gid in zip(piv[col], piv.index)]
    wx = edges[edges.category == "WEATHER"].set_index("game_id").inputs.map(lambda s: json.loads(s) if isinstance(s, str) else {})
    piv["wind_mph"] = wx.map(lambda d: d.get("wind_mph", np.nan) if not d.get("indoor") else 0.0)
    piv["indoor"] = wx.map(lambda d: 1.0 if d.get("indoor") else 0.0)
    piv["prior_total_league"] = config.LEAGUE_AVG_TOTAL[league]
    # targets
    res = storage.read_table(config.TABLES / "results" / league / f"{season}.csv")
    if not res.empty:
        res = res.set_index("game_id")
        piv["margin_home"] = res.margin_home; piv["total"] = res.total
    else:
        piv["margin_home"] = np.nan; piv["total"] = np.nan
    return piv.reset_index()


def season_features(league: str, season: int) -> pd.DataFrame:
    parts = []
    for p in sorted(glob.glob(str(AN / "matchup_edges" / league / str(season) / "W*.parquet"))):
        wk = int(p.split("W")[-1][:2])
        f = week_features(league, season, wk)
        if not f.empty:
            parts.append(f)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# ---- ridge ----------------------------------------------------------------------------------
class Ridge:
    def __init__(self, features: list[str], lam: float):
        self.features, self.lam = features, lam
        self.mu = None; self.sd = None; self.coef = None; self.intercept = None; self.fill = None

    def _X(self, df: pd.DataFrame) -> np.ndarray:
        X = df[self.features].astype(float).to_numpy()
        X = np.where(np.isnan(X), self.fill, X)
        return (X - self.mu) / self.sd

    def fit(self, df: pd.DataFrame, target: str) -> "Ridge":
        X = df[self.features].astype(float).to_numpy()
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                self.fill = np.nanmean(X, axis=0)
        self.fill = np.where(np.isnan(self.fill), 0.0, self.fill)   # an all-missing feature (e.g. TALENT in NFL) is imputed 0 -> zero coefficient effect
        X = np.where(np.isnan(X), self.fill, X)
        self.mu = X.mean(axis=0); self.sd = X.std(axis=0); self.sd = np.where(self.sd < 1e-9, 1.0, self.sd)
        Xs = (X - self.mu) / self.sd
        y = df[target].astype(float).to_numpy()
        self.intercept = float(y.mean())
        yc = y - self.intercept
        A = Xs.T @ Xs + self.lam * np.eye(Xs.shape[1]); b = Xs.T @ yc
        self.coef = np.linalg.solve(A, b)
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self.intercept + self._X(df) @ self.coef

    def contributions(self, df: pd.DataFrame) -> pd.DataFrame:
        """Per-feature points contribution for each row (coef x standardized feature)."""
        return pd.DataFrame(self._X(df) * self.coef, columns=self.features, index=df.index)

    def coef_per_raw_unit(self) -> dict:
        return {f: float(c / s) for f, c, s in zip(self.features, self.coef, self.sd)}

    def to_dict(self) -> dict:
        return {"features": self.features, "lam": self.lam, "mu": self.mu.tolist(), "sd": self.sd.tolist(), "fill": self.fill.tolist(),
                "coef": self.coef.tolist(), "intercept": self.intercept}

    @classmethod
    def from_dict(cls, d: dict) -> "Ridge":
        r = cls(d["features"], d["lam"])
        r.mu = np.array(d["mu"]); r.sd = np.array(d["sd"]); r.fill = np.array(d["fill"]); r.coef = np.array(d["coef"]); r.intercept = d["intercept"]
        return r


def cv_lambda(df: pd.DataFrame, features: list[str], target: str, grid=(1, 3, 10, 30, 100, 300, 1000), k: int = 5, seed: int = 7) -> float:
    rng = np.random.default_rng(seed)
    idx = np.arange(len(df)); rng.shuffle(idx); folds = np.array_split(idx, k)
    best, best_err = grid[0], np.inf
    for lam in grid:
        errs = []
        for f in folds:
            test = df.iloc[f]; train = df.drop(df.index[f])
            m = Ridge(features, lam).fit(train, target)
            errs.append(np.mean(np.abs(m.predict(test) - test[target].to_numpy())))
        e = float(np.mean(errs))
        if e < best_err:
            best, best_err = lam, e
    return best


# ---- model bundle ---------------------------------------------------------------------------
def fit_models(train: pd.DataFrame, league: str) -> dict:
    tr = train[train.margin_home.notna()].copy()
    lam_m = cv_lambda(tr, MARGIN_FEATURES, "margin_home"); lam_t = cv_lambda(tr, TOTAL_FEATURES, "total")
    mm = Ridge(MARGIN_FEATURES, lam_m).fit(tr, "margin_home"); tm = Ridge(TOTAL_FEATURES, lam_t).fit(tr, "total")
    resid = tr.margin_home.to_numpy() - mm.predict(tr)
    sigma = float(np.std(resid))
    return {"margin": mm, "total": tm, "sigma_margin": sigma, "n_train": int(len(tr)), "lam_margin": lam_m, "lam_total": lam_t,
            "train_seasons": sorted(tr.season.unique().tolist())}


def predict_rows(models: dict, feats: pd.DataFrame, model_version: str, is_backtest: bool) -> pd.DataFrame:
    if feats.empty:
        return pd.DataFrame()
    mm, tm = models["margin"], models["total"]
    margin = mm.predict(feats); total = np.clip(tm.predict(feats), config.TOTAL_FLOOR, None)
    sigma = models["sigma_margin"]
    wp = np.array([0.5 * (1 + math.erf(x / (sigma * math.sqrt(2)))) for x in margin])
    contrib = mm.contributions(feats)
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for i, r in feats.reset_index(drop=True).iterrows():
        c = contrib.iloc[i]
        why = sorted([{"category": f, "points": round(float(v), 2)} for f, v in c.items() if abs(v) >= 0.05], key=lambda x: -abs(x["points"]))
        avail = [f for f in MARGIN_FEATURES if not pd.isna(r[f])]
        quality = round(len(avail) / len(MARGIN_FEATURES), 3)
        rows.append({
            "prediction_id": f"{r.game_id}_{model_version}_{now}", "game_id": r.game_id, "model_version": model_version, "predicted_at": now,
            "is_pregame_final": False, "is_backtest": is_backtest, "as_of_ts": r.kickoff_utc,
            "proj_away_pts": round(float((total[i] - margin[i]) / 2), 2), "proj_home_pts": round(float((total[i] + margin[i]) / 2), 2),
            "proj_margin_home": round(float(margin[i]), 2), "proj_total": round(float(total[i]), 2), "win_prob_home": round(float(wp[i]), 4),
            "margin_sd": round(sigma, 2), "market_spread_home": None, "market_total": None, "spread_diff": None, "total_diff": None,
            "qb_home_id": None, "qb_away_id": None, "data_quality": quality, "quality_flags": "" if quality > 0.8 else "FEATURES_IMPUTED",
            "contributions": json.dumps(why), "inputs_hash": "",
        })
    return pd.DataFrame(rows)


def save_model(models: dict, league: str, model_version: str, backtest_summary: dict | None, is_active: bool) -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = MODEL_DIR / "model_versions.json"
    all_models = json.loads(path.read_text()) if path.exists() else {}
    if is_active:
        for k, v in all_models.items():
            if v.get("league") == league:
                v["is_active"] = False
    all_models[model_version] = {
        "model_version": model_version, "league": league, "description": "ridge margin + ridge total on matchup edges; walk-forward validated",
        "weights": {"margin_coef_per_raw_unit": models["margin"].coef_per_raw_unit(), "lam_margin": models["lam_margin"], "lam_total": models["lam_total"],
                    "sigma_margin": models["sigma_margin"], "recency": config.RECENCY_WEIGHTS, "prior_schedule": config.PRIOR_WEIGHT_BY_WEEK[league],
                    "ridge_lambda_ratings": config.RIDGE_LAMBDA[league]},
        "features": MARGIN_FEATURES, "total_features": TOTAL_FEATURES, "trained_on_seasons": models["train_seasons"], "n_train": models["n_train"],
        "backtest_summary": backtest_summary, "created_at": datetime.now(timezone.utc).isoformat(), "is_active": is_active,
        "margin_model": models["margin"].to_dict(), "total_model": models["total"].to_dict(),
    }
    path.write_text(json.dumps(all_models, indent=1))


def load_active_model(league: str) -> tuple[str, dict] | tuple[None, None]:
    path = MODEL_DIR / "model_versions.json"
    if not path.exists():
        return None, None
    all_models = json.loads(path.read_text())
    for k, v in all_models.items():
        if v.get("league") == league and v.get("is_active"):
            return k, {"margin": Ridge.from_dict(v["margin_model"]), "total": Ridge.from_dict(v["total_model"]), "sigma_margin": v["weights"]["sigma_margin"],
                       "n_train": v["n_train"], "lam_margin": v["weights"]["lam_margin"], "lam_total": v["weights"]["lam_total"], "train_seasons": v["trained_on_seasons"]}
    return None, None
