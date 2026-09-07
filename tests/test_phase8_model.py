from __future__ import annotations
import numpy as np
import pandas as pd
import pytest

from pipeline import model as M


def _synthetic(n=600, seed=1):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(0, 1, n) for f in M.MARGIN_FEATURES})
    df["home_field"] = 1.0
    df["margin_home"] = 2.0 + 3.0 * df["QB"] + 2.0 * df["OVERALL_OFF"] + 4.0 * df["rating_diff_blend"] + rng.normal(0, 8, n)
    for f in M.TOTAL_FEATURES:
        df[f] = rng.normal(0, 1, n)
    df["total"] = 44 + 3 * df["home_points_per_game"] + rng.normal(0, 8, n)
    df["game_id"] = [f"G{i}" for i in range(n)]; df["kickoff_utc"] = "2025-10-01T00:00:00Z"; df["season"] = 2025
    return df


def test_ridge_recovers_signal_and_contributions_sum_to_prediction():
    df = _synthetic()
    m = M.Ridge(M.MARGIN_FEATURES, 3.0).fit(df, "margin_home")
    cpu = m.coef_per_raw_unit()
    assert 2.0 < cpu["QB"] < 4.0 and 3.0 < cpu["rating_diff_blend"] < 5.0 and abs(cpu["WEATHER"]) < 0.8
    pred = m.predict(df.head(5)); contrib = m.contributions(df.head(5)).sum(axis=1) + m.intercept
    assert np.allclose(pred, contrib)


def test_predict_rows_shape_and_win_prob():
    df = _synthetic()
    models = M.fit_models(df, "NFL")
    p = M.predict_rows(models, df.head(4), "NFL_test", is_backtest=True)
    assert len(p) == 4 and set(["proj_home_pts", "proj_away_pts", "proj_margin_home", "proj_total", "win_prob_home", "contributions"]) <= set(p.columns)
    assert np.allclose(p.proj_home_pts - p.proj_away_pts, p.proj_margin_home, atol=0.02)
    assert np.allclose(p.proj_home_pts + p.proj_away_pts, p.proj_total, atol=0.02)
    assert ((p.win_prob_home > 0.5) == (p.proj_margin_home > 0)).all()
    assert 6 < models["sigma_margin"] < 12


def test_missing_feature_is_imputed_never_zero_filled_as_signal():
    df = _synthetic()
    df.loc[:, "TALENT"] = np.nan                       # entirely unavailable feature (NFL)
    m = M.Ridge(M.MARGIN_FEATURES, 3.0).fit(df, "margin_home")
    assert m.coef_per_raw_unit()["TALENT"] == 0.0
    row = df.head(1).copy(); row.loc[:, "QB"] = np.nan
    assert np.isfinite(m.predict(row)).all()


def test_model_roundtrip():
    df = _synthetic()
    m = M.Ridge(M.MARGIN_FEATURES, 3.0).fit(df, "margin_home")
    m2 = M.Ridge.from_dict(m.to_dict())
    assert np.allclose(m.predict(df.head(3)), m2.predict(df.head(3)))


def test_lock_freezes_pregame_record(tmp_path, monkeypatch):
    import json, config
    from pipeline import storage
    from pipeline.jobs import lock
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables"); monkeypatch.setattr(config, "SNAPSHOTS", tmp_path / "snapshots")
    monkeypatch.setattr(lock, "MODEL", tmp_path / "tables" / "model"); monkeypatch.setattr(lock, "AN", tmp_path / "tables" / "analytics"); monkeypatch.setattr(lock, "ROSTER", tmp_path / "tables" / "roster")
    import pipeline.log as L
    monkeypatch.setattr(L, "JOB_LOG", tmp_path / "tables" / "ops" / "job_log.csv")
    games = pd.DataFrame([{"game_id": "2026_NFL_W01_NE_SEA", "league": "NFL", "season": 2026, "season_type": "REG", "week": 1, "away_team_id": "NFL_NE", "home_team_id": "NFL_SEA",
                           "kickoff_utc": "2026-09-01T00:00:00Z", "status": "SCHEDULED", "locked_at": None, "neutral_site": False}])
    storage.write_parquet(storage.games_path("NFL", 2026), games)
    preds = pd.DataFrame([
        {"prediction_id": "p1", "game_id": "2026_NFL_W01_NE_SEA", "model_version": "NFL_v1.0", "predicted_at": "2026-08-30T00:00:00Z", "proj_margin_home": 3.0, "proj_total": 44.0, "proj_home_pts": 23.5, "proj_away_pts": 20.5, "win_prob_home": 0.58},
        {"prediction_id": "p2", "game_id": "2026_NFL_W01_NE_SEA", "model_version": "NFL_v1.0", "predicted_at": "2026-08-31T12:00:00Z", "proj_margin_home": 4.0, "proj_total": 45.0, "proj_home_pts": 24.5, "proj_away_pts": 20.5, "win_prob_home": 0.61},
        {"prediction_id": "p3", "game_id": "2026_NFL_W01_NE_SEA", "model_version": "NFL_v1.0", "predicted_at": "2026-09-01T01:00:00Z", "proj_margin_home": 9.0, "proj_total": 45.0, "proj_home_pts": 27.0, "proj_away_pts": 18.0, "win_prob_home": 0.7},  # AFTER kickoff: must be ignored
    ])
    (tmp_path / "tables" / "model" / "predictions" / "NFL").mkdir(parents=True); preds.to_csv(tmp_path / "tables" / "model" / "predictions" / "NFL" / "2026.csv", index=False)
    snaps = pd.DataFrame([{"snapshot_id": "a", "game_id": "2026_NFL_W01_NE_SEA", "retrieved_at": "2026-08-31T00:00:00Z", "book": "consensus", "spread_home": -3.5, "total": 44.5, "ml_home": -180, "ml_away": 155},
                          {"snapshot_id": "b", "game_id": "2026_NFL_W01_NE_SEA", "retrieved_at": "2026-08-31T23:00:00Z", "book": "consensus", "spread_home": -3.0, "total": 44.0, "ml_home": -160, "ml_away": 140},
                          {"snapshot_id": "c", "game_id": "2026_NFL_W01_NE_SEA", "retrieved_at": "2026-09-01T02:00:00Z", "book": "consensus", "spread_home": -1.0, "total": 40.0, "ml_home": -110, "ml_away": -110}])  # after kickoff
    d = tmp_path / "tables" / "market" / "snapshots" / "NFL" / "2026"; d.mkdir(parents=True); snaps.to_csv(d / "W01.csv", index=False)
    with L.JobRun("LOCK", "NFL") as job:
        n = lock.lock_league("NFL", 2026, job, pd.Timestamp("2026-09-01T05:00:00Z"))
    assert n == 1
    g = storage.read_table(storage.games_path("NFL", 2026)).iloc[0]; assert g.status == "LOCKED"
    idx = pd.read_csv(tmp_path / "tables" / "model" / "pregame_snapshots_index.csv").iloc[0]
    assert idx.prediction_id == "p2" and idx.closing_spread_home == -3.0 and idx.closing_total == 44.0     # last pre-kick prediction and line
    snap = json.loads((tmp_path / "snapshots" / "pregame_2026_NFL_W01_NE_SEA.json").read_text())
    assert snap["prediction"]["prediction_id"] == "p2" and len(snap["market_history"]) == 2
    # a second lock pass changes nothing (snapshot written once)
    with L.JobRun("LOCK", "NFL") as job:
        assert lock.lock_league("NFL", 2026, job, pd.Timestamp("2026-09-02T05:00:00Z")) == 0
    # results arrive -> evaluation row, status FINAL, snapshot untouched
    (tmp_path / "tables" / "results" / "NFL").mkdir(parents=True)
    pd.DataFrame([{"game_id": "2026_NFL_W01_NE_SEA", "away_score": 17, "home_score": 24, "margin_home": 7, "total": 41}]).to_csv(tmp_path / "tables" / "results" / "NFL" / "2026.csv", index=False)
    with L.JobRun("LOCK", "NFL") as job:
        assert lock.evaluate_league("NFL", 2026, job) == 1
    ev = pd.read_csv(tmp_path / "tables" / "model" / "model_evaluation" / "NFL" / "2026.csv").iloc[0]
    assert ev.prediction_id == "p2" and ev.margin_error == -3.0 and ev.winner_correct and ev.model_ats_result == "WIN" and ev.model_ou_result == "LOSS"
    assert storage.read_table(storage.games_path("NFL", 2026)).iloc[0].status == "FINAL"
    assert json.loads((tmp_path / "snapshots" / "pregame_2026_NFL_W01_NE_SEA.json").read_text())["prediction"]["proj_margin_home"] == 4.0
