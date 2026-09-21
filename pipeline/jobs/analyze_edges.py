"""
python -m pipeline.jobs.analyze_edges --league BOTH

Where, if anywhere, is the model right against the spread?

Overall ATS sits near 50%. Real edges tend to be narrow pockets, so this slices the graded backtest by
situation. Slicing is dangerous: cut the data fifty ways and several slices will look like edges by
chance alone. So every slice is judged twice, on seasons it was never allowed to see:

    DISCOVERY     2022-2024   where a slice has to look good first
    CONFIRMATION  2025        held back, untouched, where it has to look good again

A slice only "holds" if it clears break-even in both, on enough games in each. Everything else is
reported with the reason it fails. The report also states how many slices were tested and how many
would be expected to clear the bar by luck, so a lone survivor can be judged for what it is.

2021 is excluded: the walk-forward backtest predicts it with a model trained on later seasons.
"""
from __future__ import annotations
import argparse
import json

import numpy as np
import pandas as pd

import config
from pipeline import storage
from pipeline.log import JobRun

BREAK_EVEN = 0.5238
DISCOVERY = (2022, 2023, 2024)
CONFIRM = (2025,)
MIN_DISCOVERY_N = 100
MIN_CONFIRM_N = 30
KEY_NUMBERS = {3.0, 7.0, 10.0, 14.0}


def load(league: str) -> pd.DataFrame:
    path = config.TABLES / "model" / "backtest" / league / f"evaluation_{league}_v1.0.csv"
    if not path.exists():
        return pd.DataFrame()
    ev = pd.read_csv(path)
    games = []
    for s in sorted(ev.season.dropna().unique()):
        g = storage.read_table(storage.games_path(league, int(s)))
        if not g.empty:
            games.append(g[["game_id", "conference_game", "neutral_site", "kickoff_utc"]])
    if games:
        ev = ev.merge(pd.concat(games, ignore_index=True), on="game_id", how="left")
    # the line the model's side was actually getting or laying
    ev["line_for_side"] = np.where(ev.model_side_home, ev.close_spread_home, -ev.close_spread_home)
    ev["model_on_favourite"] = ev.line_for_side < 0
    return ev


def _rate(d: pd.DataFrame, col: str = "model_ats_result") -> tuple[float | None, int]:
    dec = d[d[col].isin(["WIN", "LOSS"])]
    return (float((dec[col] == "WIN").mean()) if len(dec) else None, int(len(dec)))


def slices(ev: pd.DataFrame, league: str) -> dict:
    """Named boolean masks. Every one is a pre-registered, plausible football hypothesis."""
    s = {}
    wk = ev.week
    s["weeks 1-4"] = wk <= 4
    s["weeks 5-9"] = (wk >= 5) & (wk <= 9)
    s["weeks 10+"] = wk >= 10
    a = ev.close_spread_home.abs()
    s["spread under 3"] = a < 3
    s["spread 3-7"] = (a >= 3) & (a < 7)
    s["spread 7-14"] = (a >= 7) & (a < 14)
    s["spread 14+"] = a >= 14
    s["model on favourite"] = ev.model_on_favourite
    s["model on underdog"] = ~ev.model_on_favourite
    s["model on home side"] = ev.model_side_home.astype(bool)
    s["model on road side"] = ~ev.model_side_home.astype(bool)
    s["home underdog pick"] = ev.model_side_home.astype(bool) & ~ev.model_on_favourite
    s["road favourite pick"] = ~ev.model_side_home.astype(bool) & ev.model_on_favourite
    if "conference_game" in ev.columns:
        cg = ev.conference_game.fillna(False).astype(bool)
        s["conference game"] = cg
        s["non-conference game"] = ~cg
    if "neutral_site" in ev.columns:
        s["neutral site"] = ev.neutral_site.fillna(False).astype(bool)
    for t in (1, 2, 3, 4, 5):
        s[f"model edge >= {t} pts"] = ev.edge_vs_market >= t
    if league == "NFL":
        lfs = ev.line_for_side
        s["getting the hook (+3.5 / +7.5)"] = lfs.isin([3.5, 7.5])
        s["laying the hook (-3.5 / -7.5)"] = lfs.isin([-3.5, -7.5])
        s["on a key number exactly"] = lfs.abs().isin(KEY_NUMBERS)
    if "kickoff_utc" in ev.columns:
        hr = pd.to_datetime(ev.kickoff_utc, utc=True, errors="coerce").dt.tz_convert("America/New_York").dt.hour
        s["primetime (7pm ET or later)"] = hr >= 19
    return s


SIGNIFICANCE = 0.05


def p_above_break_even(rate, n) -> float:
    """One-sided p-value that a record this good arose from a side that really wins only at break-even."""
    import math
    if rate is None or not n:
        return 1.0
    se = math.sqrt(BREAK_EVEN * (1 - BREAK_EVEN) / n)
    z = (rate - BREAK_EVEN) / se
    return 0.5 * math.erfc(z / math.sqrt(2))


def verdict(dr, dn, cr, cn, alpha: float = SIGNIFICANCE) -> str:
    """
    Clearing break-even in two separate periods is NOT enough: tested on a league where one slice had a
    real edge and every other slice was a coin flip, three coin-flip slices passed that bar by luck.
    A slice must also be significantly above break-even on its combined record.
    """
    if dn < MIN_DISCOVERY_N:
        return "too few discovery games"
    if dr is None or dr <= BREAK_EVEN:
        return "no edge"
    if cn < MIN_CONFIRM_N:
        return "promising, too few confirmation games"
    if cr is None or cr <= BREAK_EVEN:
        return "did not confirm"
    pooled_n = dn + cn
    pooled = (dr * dn + cr * cn) / pooled_n
    if p_above_break_even(pooled, pooled_n) >= alpha:
        return "consistent, but within luck"
    return "HOLDS"


def analyze(league: str) -> dict:
    ev = load(league)
    if ev.empty:
        print(f"{league}: no backtest evaluation found; run the backtest first")
        return {}
    disc, conf = ev[ev.season.isin(DISCOVERY)], ev[ev.season.isin(CONFIRM)]
    rows = []
    for market, col in (("side", "model_ats_result"), ("total", "model_ou_result")):
        if col not in ev.columns:
            continue
        masks = slices(disc, league) if market == "side" else {"all totals": disc.close_total.notna(),
                                                                **{f"total edge >= {t} pts": (disc.proj_total - disc.close_total).abs() >= t
                                                                   for t in (2, 3, 4, 5)}}
        cmasks = slices(conf, league) if market == "side" else {"all totals": conf.close_total.notna(),
                                                                 **{f"total edge >= {t} pts": (conf.proj_total - conf.close_total).abs() >= t
                                                                    for t in (2, 3, 4, 5)}}
        for name, m in masks.items():
            dr, dn = _rate(disc[m], col)
            cm = cmasks.get(name)
            cr, cn = _rate(conf[cm], col) if cm is not None else (None, 0)
            rows.append({"market": market, "slice": name, "disc_rate": dr, "disc_n": dn,
                         "conf_rate": cr, "conf_n": cn})
    df = pd.DataFrame(rows)
    tested = int((df.disc_n >= MIN_DISCOVERY_N).sum())
    # Every extra slice is another chance to find an edge by luck, so the bar tightens with the number
    # tried (Bonferroni). Uncorrected, 25 coin-flip slices let about 1.5 through per run.
    alpha = SIGNIFICANCE / max(1, tested)
    df["verdict"] = [verdict(r.disc_rate, r.disc_n, r.conf_rate, r.conf_n, alpha) for r in df.itertuples()]
    passed_disc = int(((df.disc_n >= MIN_DISCOVERY_N) & (df.disc_rate > BREAK_EVEN)).sum())
    # how many slices a fair coin would push past break-even, given each slice's own size
    import math
    def p_luck(n):
        return 0.5 * math.erfc(((BREAK_EVEN * n - 0.5 * n) / math.sqrt(0.25 * n)) / math.sqrt(2)) if n else 0.0
    expected_by_luck = round(sum(p_luck(n) for n in df.loc[df.disc_n >= MIN_DISCOVERY_N, "disc_n"]), 1)
    held = df[df.verdict == "HOLDS"]
    print(f"\n{league}: {tested} slices had enough discovery games; {passed_disc} beat break-even in discovery "
          f"(about {expected_by_luck} would by luck alone); {len(held)} held on the untouched 2025 season and "
          f"survived the correction for testing {tested} slices at once.")
    print(f"  A slice needs its combined record to clear break-even at p < {alpha:.4f}. That is strict on purpose: "
          f"it keeps chance findings out, at the cost of missing some real but small edges.\n")
    print(f"{'market':6} {'slice':34} {'disc':>7} {'n':>5}  {'2025':>7} {'n':>4}  verdict")
    order = {"HOLDS": 0, "consistent, but within luck": 1, "promising, too few confirmation games": 2,
             "did not confirm": 3, "no edge": 4, "too few discovery games": 5}
    for _, r in df.sort_values(by=["verdict", "disc_rate"], key=lambda c: c.map(order) if c.name == "verdict" else -c.fillna(0)).iterrows():
        dr = f"{r.disc_rate*100:.1f}%" if r.disc_rate is not None and not pd.isna(r.disc_rate) else "  -"
        cr = f"{r.conf_rate*100:.1f}%" if r.conf_rate is not None and not pd.isna(r.conf_rate) else "  -"
        print(f"{r.market:6} {r.slice:34} {dr:>7} {r.disc_n:>5}  {cr:>7} {r.conf_n:>4}  {r.verdict}")
    out = config.TABLES / "model" / "backtest" / league / "edge_slices.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tested": tested, "passed_discovery": passed_disc, "expected_by_luck": expected_by_luck,
                               "held": held.slice.tolist(), "rows": df.to_dict("records")}, indent=1, default=str))
    return {"held": held.slice.tolist(), "tested": tested}


def live_clv(league: str) -> dict:
    """
    Closing-line value on graded live picks, overall and by how early each pick was made.

    This answers two questions at once. Do we beat the close at all (the fastest real test of skill)?
    And does the answer depend on timing: if the line keeps moving toward our side after we pick, picking
    earlier is worth money; if it moves away, the market is telling us something we missed.
    """
    ev = storage.read_table(config.TABLES / "model" / "picks_evaluation" / league / f"{config.SEASON}.csv")
    if ev.empty or "beat_close" not in ev.columns:
        print(f"{league}: no closing-line value yet — it is recorded as picks are graded from now on")
        return {}
    ev = ev[ev.beat_close.notna()]
    if ev.empty:
        print(f"{league}: graded picks have no closing number to compare against yet")
        return {}
    beat = ev.beat_close.astype(bool)
    pts = pd.to_numeric(ev.get("clv_points"), errors="coerce")
    print(f"\n{league} closing-line value ({len(ev)} graded picks with a closing number):")
    print(f"  beat the close on {beat.mean()*100:.0f}% of picks; average {pts.mean():+.2f} points on sides and totals"
          if pts.notna().any() else f"  beat the close on {beat.mean()*100:.0f}% of picks")
    if "hours_before_kick" in ev.columns:
        h = pd.to_numeric(ev.hours_before_kick, errors="coerce")
        for label, m in (("72+ hours out", h >= 72), ("24-72 hours out", (h >= 24) & (h < 72)),
                         ("6-24 hours out", (h >= 6) & (h < 24)), ("under 6 hours", h < 6)):
            sub = ev[m.fillna(False)]
            if len(sub):
                sp = pd.to_numeric(sub.get("clv_points"), errors="coerce")
                avg = f", avg {sp.mean():+.2f} pts" if sp.notna().any() else ""
                print(f"  {label:16} {len(sub):>4} picks, beat close {sub.beat_close.astype(bool).mean()*100:.0f}%{avg}")
    print("  Consistently beating the close is the strongest early evidence of skill; around half is what chance gives.")
    return {"n": int(len(ev)), "beat_rate": float(beat.mean())}


def live_signals(league: str) -> None:
    """The forward log: how graded live picks have done by the market signal behind them."""
    ev = storage.read_table(config.TABLES / "model" / "picks_evaluation" / league / f"{config.SEASON}.csv")
    if ev.empty:
        print(f"{league}: no graded live picks yet — the signal log fills in as games finish")
        return
    ev = ev[ev.result.isin(["WIN", "LOSS"])]
    print(f"\n{league} live picks by market signal (this season, {len(ev)} decided):")
    sigs = {}
    for _, r in ev.iterrows():
        for sgl in [x for x in str(r.get("signals") or "").split(",") if x] or ["no signal"]:
            sigs.setdefault(sgl, []).append(r.result == "WIN")
    for sgl, res in sorted(sigs.items(), key=lambda kv: -len(kv[1])):
        w = sum(res)
        print(f"  {sgl:16} {w}-{len(res) - w}  ({w / len(res) * 100:.0f}%)")
    print("  One season of live picks is a small sample; read these as a log building up, not a verdict.")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="BOTH", choices=["NFL", "CFB", "BOTH"])
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    with JobRun("ANALYZE_EDGES", a.league, a.trigger) as job:
        for lg in (["NFL", "CFB"] if a.league == "BOTH" else [a.league]):
            analyze(lg)
            live_clv(lg)
            live_signals(lg)
            job.rows_written += 1


if __name__ == "__main__":
    main()
