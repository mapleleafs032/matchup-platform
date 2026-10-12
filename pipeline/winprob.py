"""
In-game win probability, play by play, from the home team's side.

NFL   nflfastR's pre-snap win probability, stored on every play as wp_pre. It is from the POSSESSION
      team's side, so it is flipped to the home team whenever the away team has the ball. It accounts
      for field position, down and distance and timeouts, so it is the better of the two sources.

CFB   CFBD's play feed carries no win probability, so it is computed with Stern's model (Stern, 1991,
      "On the probability of winning a football game"): the final margin is treated as normally
      distributed around the current score plus the share of the pregame spread still to be played,
      with variance shrinking as the clock runs down. It starts where the spread puts it -- the
      favourite above 50% -- and converges to 0 or 1 at the final whistle. It uses score, clock and
      spread only, so it is coarser than nflfastR's: it cannot see that a team is on the goal line.

Both curves are drawn the same way so the chart reads identically whichever league it is.
"""
from __future__ import annotations
import math

import pandas as pd

GAME_SECONDS = 3600
# Standard deviation of the final margin about the spread, in points. Stern's NFL estimate is 13.86;
# college margins scatter further. These match the residual spread the backtest measures.
SIGMA = {"NFL": 13.86, "CFB": 16.0}


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def stern_home_wp(home_diff: float, secs_left: float, spread_home: float | None, league: str) -> float:
    """
    Home win probability from score margin, time remaining and the pregame spread.
    spread_home is the bookmaker's number from the home side (negative means home favoured).
    """
    sigma = SIGMA.get(league, 14.0)
    frac = max(0.0, min(1.0, secs_left / GAME_SECONDS))
    expected_rest = (-(spread_home or 0.0)) * frac          # home's expected margin over the remaining time
    mean = home_diff + expected_rest
    if frac <= 1e-6:                                          # clock has run out: the score decides it
        return 1.0 if home_diff > 0 else 0.0 if home_diff < 0 else 0.5
    return _phi(mean / (sigma * math.sqrt(frac)))


def series(plays: pd.DataFrame, league: str, home_team: str, away_team: str,
           spread_home: float | None, home_final: int | None, away_final: int | None) -> list[dict]:
    """
    One point per play: seconds elapsed, home win probability, and the score at that moment.
    The curve opens at the pregame value and closes at the result.
    """
    if plays is None or plays.empty:
        return []
    p = plays.copy()
    p = p[p.game_sec_remaining.notna()].sort_values(["game_sec_remaining"], ascending=False)
    if p.empty:
        return []
    out = [{"t": 0, "wp": round(stern_home_wp(0.0, GAME_SECONDS, spread_home, league), 4),
            "home_diff": 0, "period": 1, "label": "kickoff"}]
    for _, r in p.iterrows():
        secs_left = float(r.game_sec_remaining)
        home_has_ball = r.offense_team_id == home_team
        diff = r.score_diff_pre
        if pd.isna(diff):
            continue
        home_diff = float(diff) if home_has_ball else -float(diff)     # both sources store the possession team's margin
        wp = None
        if league == "NFL" and pd.notna(r.get("wp_pre")):
            wp = float(r.wp_pre) if home_has_ball else 1.0 - float(r.wp_pre)
        if wp is None:
            wp = stern_home_wp(home_diff, secs_left, spread_home, league)
        out.append({"t": int(GAME_SECONDS - secs_left), "wp": round(max(0.0, min(1.0, wp)), 4),
                    "home_diff": int(home_diff), "period": int(r.period) if pd.notna(r.period) else None})
    if home_final is not None and away_final is not None:
        final = 1.0 if home_final > away_final else 0.0 if home_final < away_final else 0.5
        out.append({"t": max(GAME_SECONDS, out[-1]["t"]), "wp": final,
                    "home_diff": int(home_final - away_final), "period": out[-1].get("period"), "label": "final"})
    # thin to at most ~240 points so the page stays light; keep every scoring change
    if len(out) > 240:
        step = len(out) / 240.0
        keep = {int(i * step) for i in range(240)} | {0, len(out) - 1}
        keep |= {i for i in range(1, len(out)) if out[i]["home_diff"] != out[i - 1]["home_diff"]}
        out = [out[i] for i in sorted(keep)]
    return out


def summary(pts: list[dict]) -> dict:
    """Headline facts for the chart: biggest swing and how long each side was favoured."""
    if len(pts) < 2:
        return {}
    swings = [(abs(pts[i]["wp"] - pts[i - 1]["wp"]), i) for i in range(1, len(pts))]
    big, idx = max(swings)
    home_share = sum(1 for p in pts if p["wp"] > 0.5) / len(pts)
    return {"biggest_swing": round(big, 4), "biggest_swing_at": pts[idx]["t"],
            "home_favoured_share": round(home_share, 3),
            "min_home_wp": round(min(p["wp"] for p in pts), 4), "max_home_wp": round(max(p["wp"] for p in pts), 4)}
