"""
python -m pipeline.jobs.backtest --league NFL
python -m pipeline.jobs.backtest --league CFB --activate      # also fit the final model on all seasons and mark it active

Walk-forward: for each evaluated season S in BACKTEST_SEASONS, the model is fit ONLY on seasons < S.
The first season (no earlier data) is evaluated with a model fit on the other seasons and flagged
"in_sample_warning" so it is reported separately and never mistaken for out-of-sample.

Baselines reported next to the model (§66):
  market   : closing spread as the margin prediction (the number to beat)
  prior    : blended rating diff + league HFA only
  ratings  : opponent-adjusted rating diff only

Writes (REBUILDABLE — backtests are re-runnable):
  data/tables/model/backtest/{league}/predictions_{model_version}.csv
  data/tables/model/backtest/{league}/evaluation_{model_version}.csv
  data/tables/model/backtest/{league}/report_{model_version}.md
"""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from pipeline import model as M, storage
from pipeline.log import JobRun

BT = config.TABLES / "model" / "backtest"


def closing(league: str, season: int) -> pd.DataFrame:
    cl = storage.read_table(config.TABLES / "market" / "closing_lines" / league / f"{season}.parquet")
    if cl.empty:
        return pd.DataFrame(columns=["game_id", "spread_home", "total"])
    pref = [b for b in config.CLOSING_BOOK_PRIORITY if b in set(cl.book)]
    if pref:
        cl = cl[cl.book == pref[0]]
    return cl.drop_duplicates("game_id")[["game_id", "spread_home", "total"]].rename(columns={"spread_home": "close_spread_home", "total": "close_total"})


def evaluate(pred: pd.DataFrame, feats: pd.DataFrame, league: str) -> pd.DataFrame:
    df = pred.merge(feats[["game_id", "season", "week", "margin_home", "total", "home_field"]], on="game_id", how="inner")
    df = df[df.margin_home.notna()]
    cl = pd.concat([closing(league, s) for s in df.season.unique()], ignore_index=True) if len(df) else pd.DataFrame()
    df = df.merge(cl, on="game_id", how="left") if not cl.empty else df.assign(close_spread_home=np.nan, close_total=np.nan)
    df["margin_error"] = df.proj_margin_home - df.margin_home
    df["abs_margin_error"] = df.margin_error.abs()
    df["total_error"] = df.proj_total - df.total; df["abs_total_error"] = df.total_error.abs()
    df["winner_correct"] = (np.sign(df.proj_margin_home) == np.sign(df.margin_home)).astype(float)
    df.loc[df.margin_home == 0, "winner_correct"] = np.nan
    df["favorite_side"] = np.where(df.close_spread_home < 0, "HOME", np.where(df.close_spread_home > 0, "AWAY", "PICKEM"))
    df["market_margin"] = -df.close_spread_home
    df["market_abs_error"] = (df.market_margin - df.margin_home).abs()
    df["market_winner_correct"] = (np.sign(df.market_margin) == np.sign(df.margin_home)).astype(float)
    # ATS: model side = home if proj_margin_home > market_margin; result vs closing spread
    df["model_side_home"] = df.proj_margin_home > df.market_margin
    cover_home = df.margin_home + df.close_spread_home
    df["model_ats_result"] = np.where(cover_home == 0, "PUSH", np.where((cover_home > 0) == df.model_side_home, "WIN", "LOSS"))
    df.loc[df.close_spread_home.isna(), "model_ats_result"] = None
    df["model_ou_side_over"] = df.proj_total > df.close_total
    ou = df.total - df.close_total
    df["model_ou_result"] = np.where(ou == 0, "PUSH", np.where((ou > 0) == df.model_ou_side_over, "WIN", "LOSS"))
    df.loc[df.close_total.isna(), "model_ou_result"] = None
    df["edge_vs_market"] = (df.proj_margin_home - df.market_margin).abs()
    df["baseline_ratings_abs_error"] = (df.baseline_ratings_margin - df.margin_home).abs()
    df["baseline_hfa_abs_error"] = (df.baseline_hfa_margin - df.margin_home).abs()
    df["win_prob_bin"] = (df.win_prob_home * 10).clip(0, 9).astype(int)
    df["home_won"] = df.margin_home > 0
    return df


def summarize(ev: pd.DataFrame, league: str) -> dict:
    def block(d: pd.DataFrame) -> dict:
        if d.empty:
            return {}
        out = {"n": int(len(d)), "mae_margin": round(float(d.abs_margin_error.mean()), 2), "rmse_margin": round(float(np.sqrt((d.margin_error ** 2).mean())), 2),
               "winner_acc": round(float(d.winner_correct.mean()), 3), "mae_total": round(float(d.abs_total_error.mean()), 2),
               "baseline_ratings_mae": round(float(d.baseline_ratings_abs_error.mean()), 2), "baseline_hfa_mae": round(float(d.baseline_hfa_abs_error.mean()), 2)}
        m = d[d.close_spread_home.notna()]
        if len(m):
            ats = m[m.model_ats_result.isin(["WIN", "LOSS"])]
            big = ats[ats.edge_vs_market >= config.ATS_EDGE_THRESHOLD[league]]
            ou = m[m.model_ou_result.isin(["WIN", "LOSS"])]
            out.update({"n_with_close": int(len(m)), "market_mae_margin": round(float(m.market_abs_error.mean()), 2),
                        "market_winner_acc": round(float(m.market_winner_correct.mean()), 3),
                        "model_mae_on_same_games": round(float(m.abs_margin_error.mean()), 2),
                        "ats_all_pct": round(float((ats.model_ats_result == "WIN").mean()), 3) if len(ats) else None, "ats_all_n": int(len(ats)),
                        f"ats_edge{config.ATS_EDGE_THRESHOLD[league]}_pct": round(float((big.model_ats_result == "WIN").mean()), 3) if len(big) else None, "ats_edge_n": int(len(big)),
                        "ou_pct": round(float((ou.model_ou_result == "WIN").mean()), 3) if len(ou) else None,
                        "corr_model_vs_close": round(float(np.corrcoef(m.proj_margin_home, m.market_margin)[0, 1]), 3) if len(m) > 3 else None})
        return out
    s = {"overall": block(ev), "by_season": {int(k): block(g) for k, g in ev.groupby("season")},
         "by_week_bucket": {b: block(g) for b, g in ev.groupby(pd.cut(ev.week, [0, 3, 8, 30], labels=["W1-3", "W4-8", "W9+"]), observed=True)},
         "home_games": block(ev[ev.home_field == 1.0]), "neutral_games": block(ev[ev.home_field == 0.0]),
         "favorites": block(ev[(ev.favorite_side == "HOME") & (ev.proj_margin_home > 0) | (ev.favorite_side == "AWAY") & (ev.proj_margin_home < 0)]),
         "calibration": [{"bin": f"{b/10:.1f}-{(b+1)/10:.1f}", "n": int(len(g)), "pred_mean": round(float(g.win_prob_home.mean()), 3), "actual_home_win": round(float(g.home_won.mean()), 3)}
                         for b, g in ev.groupby("win_prob_bin")]}
    return s


def report_md(league: str, mv: str, summ: dict, coefs: dict, sigma: float, warn_season: int | None) -> str:
    o = summ["overall"]
    L = [f"# Backtest report — {league} — {mv}", "", f"Generated {datetime.now(timezone.utc).isoformat(timespec='minutes')}. Walk-forward by season: each season predicted by a model fit only on earlier seasons."]
    if warn_season:
        L.append(f"**{warn_season} has no earlier data; it was predicted by a model fit on the later seasons and is NOT out-of-sample. Treat it as a smoke test only.**")
    L += ["", "## Overall (out-of-sample seasons)", f"- games: {o.get('n')}", f"- margin MAE: **{o.get('mae_margin')}** (RMSE {o.get('rmse_margin')})", f"- winner accuracy: **{o.get('winner_acc')}**", f"- total MAE: {o.get('mae_total')}", f"- residual SD used for win probability: {sigma:.2f}",
          "", "Baselines on the same games (lower MAE is better):", f"- home-field only: {o.get('baseline_hfa_mae')}", f"- opponent-adjusted rating diff + HFA: {o.get('baseline_ratings_mae')}", f"- full model: {o.get('mae_margin')}"]
    if o.get("n_with_close"):
        L += ["", f"## Versus the closing line ({o['n_with_close']} games with a closing spread)",
              f"- market MAE on those games: **{o['market_mae_margin']}** vs model MAE **{o['model_mae_on_same_games']}**",
              f"- market winner accuracy: {o['market_winner_acc']} vs model {o.get('winner_acc')}",
              f"- model side vs closing spread, all games: {o['ats_all_pct']} ({o['ats_all_n']} decided)",
              f"- model side vs closing spread when model differs by >= {config.ATS_EDGE_THRESHOLD[league]} pts: {o.get(f'ats_edge{config.ATS_EDGE_THRESHOLD[league]}_pct')} ({o['ats_edge_n']} decided)",
              f"- over/under: {o['ou_pct']}", f"- correlation model margin vs market margin: {o['corr_model_vs_close']}",
              "", "Break-even against -110 pricing is 52.4%. Anything below that is not an edge; anything above it on a few hundred games is not proof either."]
    L += ["", "## By season", "| season | n | MAE | winner acc | market MAE | ATS all | ATS edge |", "|---|---|---|---|---|---|---|"]
    for s, b in summ["by_season"].items():
        L.append(f"| {s}{' (in-sample)' if s == warn_season else ''} | {b.get('n')} | {b.get('mae_margin')} | {b.get('winner_acc')} | {b.get('market_mae_margin', '—')} | {b.get('ats_all_pct', '—')} | {b.get(f'ats_edge{config.ATS_EDGE_THRESHOLD[league]}_pct', '—')} |")
    L += ["", "## By week bucket", "| weeks | n | MAE | winner acc |", "|---|---|---|---|"]
    for k, b in summ["by_week_bucket"].items():
        L.append(f"| {k} | {b.get('n')} | {b.get('mae_margin')} | {b.get('winner_acc')} |")
    L += ["", "## Calibration (home win probability)", "| bin | n | predicted | actual |", "|---|---|---|---|"]
    for c in summ["calibration"]:
        L.append(f"| {c['bin']} | {c['n']} | {c['pred_mean']} | {c['actual_home_win']} |")
    L += ["", "## Fitted weights (points per raw unit of each edge; the matchup engine displays these)", "| feature | points/unit |", "|---|---|"]
    for f, c in sorted(coefs.items(), key=lambda x: -abs(x[1])):
        L.append(f"| {f} | {c:+.3f} |")
    return "\n".join(L)


def run(league: str, activate: bool, job: JobRun) -> None:
    seasons = [s for s in config.BACKTEST_SEASONS]
    feats = {s: M.season_features(league, s) for s in seasons}
    feats = {s: f for s, f in feats.items() if not f.empty}
    if len(feats) < 2:
        job.status = "SKIPPED"; job.message = f"need >=2 seasons of matchup edges; found {sorted(feats)}"; return
    mv = f"{league}_v1.0"
    preds, warn_season = [], None
    for s in sorted(feats):
        train_seasons = [t for t in feats if t < s]
        if not train_seasons:
            train_seasons = [t for t in feats if t != s]; warn_season = s
        train = pd.concat([feats[t] for t in train_seasons], ignore_index=True)
        models = M.fit_models(train, league)
        p = M.predict_rows(models, feats[s], mv, is_backtest=True)
        tr = train[train.margin_home.notna()]
        base_r = M.Ridge(["rating_diff_blend", "home_field"], 1.0).fit(tr, "margin_home")
        base_h = M.Ridge(["home_field"], 1.0).fit(tr, "margin_home")
        p["baseline_ratings_margin"] = base_r.predict(feats[s]); p["baseline_hfa_margin"] = base_h.predict(feats[s])
        p["train_seasons"] = ",".join(map(str, models["train_seasons"])); p["in_sample_warning"] = (s == warn_season)
        preds.append(p)
        print(f"{league} {s}: predicted {len(p)} games with model trained on {models['train_seasons']} (lambda margin={models['lam_margin']}, total={models['lam_total']}, sigma={models['sigma_margin']:.1f})")
    pred = pd.concat(preds, ignore_index=True)
    allf = pd.concat(feats.values(), ignore_index=True)
    ev = evaluate(pred, allf, league)
    oos = ev[~ev.game_id.isin(pred[pred.in_sample_warning].game_id)]
    summ = summarize(oos if len(oos) else ev, league)
    final = M.fit_models(allf, league)
    BT.mkdir(parents=True, exist_ok=True)
    (BT / league).mkdir(parents=True, exist_ok=True)
    pred.to_csv(BT / league / f"predictions_{mv}.csv", index=False)
    ev.to_csv(BT / league / f"evaluation_{mv}.csv", index=False)
    md = report_md(league, mv, summ, final["margin"].coef_per_raw_unit(), final["sigma_margin"], warn_season)
    (BT / league / f"report_{mv}.md").write_text(md)
    M.save_model(final, league, mv, summ, is_active=activate)
    job.rows_written = len(pred) + len(ev)
    o = summ["overall"]
    print(f"{league} OOS: n={o.get('n')} MAE={o.get('mae_margin')} winner={o.get('winner_acc')} | market MAE={o.get('market_mae_margin')} ATS all={o.get('ats_all_pct')} ATS edge={o.get(f'ats_edge{config.ATS_EDGE_THRESHOLD[league]}_pct')} (n={o.get('ats_edge_n')})")
    print(f"model {mv} saved; active={activate}; trained on {final['train_seasons']}")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--activate", action="store_true")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    with JobRun(f"{a.league}_BACKTEST", a.league, a.trigger) as job:
        run(a.league, a.activate, job)


if __name__ == "__main__":
    main()
