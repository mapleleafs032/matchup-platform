"""
Metric registry. Every metric the model or UI can reference is defined here, once.

agg (how per-game values roll up over a window):
  mean                      simple mean of per-game values
  wmean:<col>               mean weighted by a per-game denominator column (e.g. off_plays)
  ratio:<num>/<den>         sum(num) / sum(den) over the window
  per_game:<col>            sum(col) / games
Direction (higher_is_better) is from the perspective of the team the metric describes.
Sources match the Phase 2 audit. `leagues` lists where the metric can exist; if the underlying column is
NULL for every game in a window, the metric is NULL ("Unavailable"), never 0.
"""
from __future__ import annotations

import pandas as pd

import config

_M: list[dict] = []


def _add(key, label, desc, unit, hib, side, agg, leagues=("CFB", "NFL"), source="derived", lo=None, hi=None, min_n=1, group="OTHER"):
    _M.append({"metric_key": key, "label": label, "description": desc, "unit": unit, "higher_is_better": hib, "side": side,
               "agg": agg, "leagues": ",".join(leagues), "primary_source": source, "is_derived": source == "derived",
               "min_valid": lo, "max_valid": hi, "min_sample_n": min_n, "group": group})


# ---- SCORING ----------------------------------------------------------------------
_add("points_per_game", "Points/Game", "Average points scored per game.", "points", True, "OFF", "mean", lo=0, hi=90, group="SCORING")
_add("points_allowed_per_game", "Points Allowed/Game", "Average points allowed per game.", "points", False, "DEF", "mean", lo=0, hi=90, group="SCORING")
_add("scoring_margin", "Scoring Margin", "Points scored minus points allowed, per game.", "points", True, "OFF", "mean", lo=-80, hi=80, group="SCORING")
# ---- EFFICIENCY (box) ---------------------------------------------------------------
_add("yards_per_play_off", "Yards/Play (Off)", "Offensive yards per scrimmage play.", "yards", True, "OFF", "ratio:total_yards/plays", lo=0, hi=15, group="EFFICIENCY")
_add("yards_per_play_def", "Yards/Play (Def)", "Yards allowed per opponent scrimmage play.", "yards", False, "DEF", "ratio:d_total_yards/d_plays", lo=0, hi=15, group="EFFICIENCY")
_add("net_yards_per_play", "Net Yards/Play", "Offensive yards per play minus defensive yards per play allowed.", "yards", True, "OFF", "mean", lo=-10, hi=10, group="EFFICIENCY")
# ---- RUSHING / PASSING ----------------------------------------------------------------
_add("rush_yds_per_game", "Rush Yds/Game", "Rushing yards per game.", "yards", True, "OFF", "mean", lo=0, hi=500, group="RUSHING")
_add("opp_rush_yds_per_game", "Opp Rush Yds/Game", "Rushing yards allowed per game.", "yards", False, "DEF", "mean", lo=0, hi=500, group="RUSHING")
_add("yards_per_rush", "Yards/Rush", "Rushing yards per rushing attempt.", "yards", True, "OFF", "ratio:rush_yds/rush_att", lo=-2, hi=12, group="RUSHING")
_add("opp_yards_per_rush", "Opp Yards/Rush", "Rushing yards allowed per opponent attempt.", "yards", False, "DEF", "ratio:d_rush_yds/d_rush_att", lo=-2, hi=12, group="RUSHING")
_add("pass_yds_per_game", "Pass Yds/Game", "Net passing yards per game.", "yards", True, "OFF", "mean", lo=0, hi=700, group="PASSING")
_add("opp_pass_yds_per_game", "Opp Pass Yds/Game", "Net passing yards allowed per game.", "yards", False, "DEF", "mean", lo=0, hi=700, group="PASSING")
_add("yards_per_pass", "Yards/Pass Att", "Net passing yards per pass attempt.", "yards", True, "OFF", "ratio:pass_yds/pass_att", lo=0, hi=20, group="PASSING")
_add("opp_yards_per_pass", "Opp Yards/Pass Att", "Net passing yards allowed per opponent attempt.", "yards", False, "DEF", "ratio:d_pass_yds/d_pass_att", lo=0, hi=20, group="PASSING")
_add("completion_pct", "Completion %", "Completions divided by attempts.", "pct", True, "OFF", "ratio:pass_cmp/pass_att", lo=0, hi=1, group="PASSING")
# ---- PROTECTION / RUSH -----------------------------------------------------------------
_add("sack_rate_allowed", "Sack Rate Allowed", "Sacks taken per dropback.", "pct", False, "OFF", "ratio:sacks_taken/dropbacks", lo=0, hi=0.3, group="TRENCHES")
_add("sack_rate", "Sack Rate", "Sacks generated per opponent dropback.", "pct", True, "DEF", "ratio:sacks_made/d_dropbacks", lo=0, hi=0.3, group="TRENCHES")
_add("off_pressure_rate_allowed", "Pressure Rate Allowed", "Opponent pressures per dropback (PFR).", "pct", False, "OFF", "wmean:dropbacks", leagues=("NFL",), source="nflverse_pfr", lo=0, hi=0.8, group="TRENCHES")
_add("def_pressure_rate", "Pressure Rate", "Individual defender pressures per opponent dropback (PFR); can exceed play-level rate.", "pct", True, "DEF", "wmean:d_dropbacks", leagues=("NFL",), source="nflverse_pfr", lo=0, hi=1.5, group="TRENCHES")
_add("off_havoc_allowed", "Havoc Allowed", "Share of offensive plays with a TFL, sack, forced fumble, interception or pass breakup against.", "pct", False, "OFF", "wmean:off_plays", source="cfbd|nflverse", lo=0, hi=0.5, group="TRENCHES")
_add("def_havoc", "Havoc Rate", "Share of defensive plays producing a TFL, sack, forced fumble, interception or pass breakup.", "pct", True, "DEF", "wmean:def_plays", source="cfbd|nflverse", lo=0, hi=0.5, group="TRENCHES")
_add("def_havoc_front", "Front-Seven Havoc", "Havoc plays generated by the front seven, per defensive play.", "pct", True, "DEF", "wmean:def_plays", source="cfbd|nflverse", lo=0, hi=0.4, group="TRENCHES")
_add("def_havoc_db", "Secondary Havoc", "Havoc plays generated by defensive backs, per defensive play.", "pct", True, "DEF", "wmean:def_plays", source="cfbd|nflverse", lo=0, hi=0.4, group="TRENCHES")
_add("off_line_yards", "Line Yards", "Rushing yards credited to the offensive line per rush (Football Outsiders formula).", "yards", True, "OFF", "wmean:off_plays", source="cfbd|derived", lo=0, hi=6, group="TRENCHES")
_add("def_line_yards_allowed", "Line Yards Allowed", "Line yards allowed per opponent rush.", "yards", False, "DEF", "wmean:def_plays", source="cfbd|derived", lo=0, hi=6, group="TRENCHES")
_add("off_opportunity_rate", "Opportunity Rate", "Share of rushes gaining at least 4 yards.", "pct", True, "OFF", "wmean:off_plays", lo=0, hi=1, group="TRENCHES")
_add("off_stuff_rate_allowed", "Stuff Rate Allowed", "Share of rushes stopped at or behind the line.", "pct", False, "OFF", "wmean:off_plays", source="cfbd|derived", lo=0, hi=1, group="TRENCHES")
_add("def_stuff_rate", "Stuff Rate", "Share of opponent rushes stopped at or behind the line.", "pct", True, "DEF", "wmean:def_plays", source="cfbd|derived", lo=0, hi=1, group="TRENCHES")
_add("off_power_success", "Power Success", "Conversion rate on 3rd/4th down rushes needing 2 yards or fewer.", "pct", True, "OFF", "wmean:off_plays", source="cfbd|derived", lo=0, hi=1, min_n=3, group="TRENCHES")
# ---- SITUATIONAL ----------------------------------------------------------------------
_add("third_down_pct_off", "3rd Down % (Off)", "Third-down conversion rate.", "pct", True, "OFF", "ratio:third_down_conv/third_down_att", lo=0, hi=1, group="SITUATIONAL")
_add("third_down_pct_def", "3rd Down % (Def)", "Third-down conversion rate allowed.", "pct", False, "DEF", "ratio:d_third_down_conv/d_third_down_att", lo=0, hi=1, group="SITUATIONAL")
_add("fourth_down_pct_off", "4th Down % (Off)", "Fourth-down conversion rate.", "pct", True, "OFF", "ratio:fourth_down_conv/fourth_down_att", lo=0, hi=1, min_n=3, group="SITUATIONAL")
_add("off_rz_td_rate", "Red Zone TD % (Off)", "Touchdowns per red-zone trip.", "pct", True, "OFF", "wmean:off_rz_trips", lo=0, hi=1, min_n=3, group="SITUATIONAL")
_add("def_rz_td_rate_allowed", "Red Zone TD % (Def)", "Touchdowns allowed per opponent red-zone trip.", "pct", False, "DEF", "wmean:def_plays", lo=0, hi=1, min_n=3, group="SITUATIONAL")
# ---- TURNOVERS ------------------------------------------------------------------------
_add("turnover_margin", "Turnover Margin", "Takeaways minus giveaways, per game.", "count", True, "OFF", "mean", lo=-6, hi=6, group="TURNOVERS")
_add("giveaways_per_game", "Giveaways/Game", "Turnovers committed per game.", "count", False, "OFF", "mean", lo=0, hi=8, group="TURNOVERS")
_add("takeaways_per_game", "Takeaways/Game", "Turnovers forced per game.", "count", True, "DEF", "mean", lo=0, hi=8, group="TURNOVERS")
_add("int_rate", "INT Rate", "Interceptions thrown per pass attempt.", "pct", False, "OFF", "ratio:pass_int/pass_att", lo=0, hi=0.2, group="TURNOVERS")
_add("fumble_rate", "Fumble Rate", "Fumbles lost per offensive play.", "pct", False, "OFF", "ratio:fumbles_lost/plays", lo=0, hi=0.1, group="TURNOVERS")
# ---- EPA / PPA ------------------------------------------------------------------------
for pre, side, hib, wt in (("off", "OFF", True, "off_plays"), ("def", "DEF", False, "def_plays")):
    who = "Offensive" if pre == "off" else "Defensive"
    tail = "" if pre == "off" else " allowed"
    _add(f"{pre}_ppa_play", f"{who} EPA/Play", f"Expected points added per play{tail} (CFB: CFBD PPA; NFL: nflfastR EPA).", "points", hib, side, f"wmean:{wt}", source="cfbd|nflverse", lo=-1.5, hi=1.5, group="EPA")
    _add(f"{pre}_ppa_pass", f"{who} Passing EPA/Play", f"EPA per pass play{tail}.", "points", hib, side, f"wmean:{wt}", source="cfbd|nflverse", lo=-2, hi=2, group="EPA")
    _add(f"{pre}_ppa_rush", f"{who} Rushing EPA/Play", f"Expected points added per rushing play{tail}.", "points", hib, side, f"wmean:{wt}", source="cfbd|nflverse", lo=-2, hi=2, group="EPA")
    _add(f"{pre}_ppa_early", f"{who} Early-Down EPA", f"EPA per play on 1st and 2nd down{tail}.", "points", hib, side, f"wmean:{wt}", source="derived", lo=-2, hi=2, group="EPA")
    _add(f"{pre}_ppa_late", f"{who} Late-Down EPA", f"EPA per play on 3rd and 4th down{tail}.", "points", hib, side, f"wmean:{wt}", source="derived", lo=-3, hi=3, group="EPA")
    _add(f"{pre}_success_rate", f"{who} Success Rate", f"Share of plays{tail} meeting the success yardage threshold (NFL: EPA>0).", "pct", hib, side, f"wmean:{wt}", source="cfbd|nflverse", lo=0, hi=1, group="SUCCESS")
    _add(f"{pre}_success_pass", f"{who} Passing Success", f"Success rate on pass plays{tail}.", "pct", hib, side, f"wmean:{wt}", source="cfbd|nflverse", lo=0, hi=1, group="SUCCESS")
    _add(f"{pre}_success_rush", f"{who} Rushing Success", f"Success rate on rushes{tail}.", "pct", hib, side, f"wmean:{wt}", source="cfbd|nflverse", lo=0, hi=1, group="SUCCESS")
    _add(f"{pre}_success_std_downs", f"{who} Standard-Down Success", f"Success rate on standard downs{tail}.", "pct", hib, side, f"wmean:{wt}", source="cfbd|derived", lo=0, hi=1, group="SUCCESS")
    _add(f"{pre}_success_pass_downs", f"{who} Passing-Down Success", f"Success rate on passing downs{tail}.", "pct", hib, side, f"wmean:{wt}", source="cfbd|derived", lo=0, hi=1, group="SUCCESS")
    _add(f"{pre}_explosiveness", f"{who} Explosiveness", f"Average EPA on successful plays{tail} (IsoPPP-style).", "points", hib, side, f"wmean:{wt}", source="cfbd|derived", lo=0, hi=4, group="EXPLOSIVE")
_add("off_explosive_play_rate", "Explosive Play Rate", "Share of plays gaining 10+ rushing or 20+ passing yards.", "pct", True, "OFF", "wmean:off_plays", lo=0, hi=0.5, group="EXPLOSIVE")
_add("def_explosive_play_rate_allowed", "Explosive Plays Allowed", "Share of opponent plays gaining 10+ rushing or 20+ passing yards.", "pct", False, "DEF", "wmean:def_plays", lo=0, hi=0.5, group="EXPLOSIVE")
_add("off_rush_10plus_rate", "10+ Yard Rush Rate", "Share of rushes gaining 10+ yards.", "pct", True, "OFF", "wmean:off_plays", lo=0, hi=0.6, group="EXPLOSIVE")
_add("off_pass_20plus_rate", "20+ Yard Pass Rate", "Share of dropbacks producing a 20+ yard completion.", "pct", True, "OFF", "wmean:off_plays", lo=0, hi=0.6, group="EXPLOSIVE")
# ---- FINISHING / FIELD POSITION / PACE --------------------------------------------------
_add("off_pts_per_scoring_opp", "Points/Scoring Opportunity", "Points per drive that reached the opponent 40.", "points", True, "OFF", "wmean:off_scoring_opps", lo=0, hi=7, min_n=3, group="FINISHING")
_add("def_pts_per_scoring_opp_allowed", "Points Allowed/Scoring Opp", "Points allowed per opponent drive reaching your 40.", "points", False, "DEF", "wmean:def_plays", lo=0, hi=7, min_n=3, group="FINISHING")
_add("off_td_rate_opp_territory", "TD Rate in Opp Territory", "Touchdowns per drive that reached the opponent 40.", "pct", True, "OFF", "wmean:off_scoring_opps", lo=0, hi=1, min_n=3, group="FINISHING")
_add("off_avg_start_yardline", "Avg Starting Field Position", "Average own-yardline where offensive drives start.", "yardline", True, "OFF", "mean", lo=10, hi=60, group="FIELD_POSITION")
_add("def_avg_start_yardline_allowed", "Opp Avg Starting Field Position", "Average own-yardline where opponent drives start.", "yardline", False, "DEF", "mean", lo=10, hi=60, group="FIELD_POSITION")
_add("plays_per_game", "Plays/Game", "Offensive scrimmage plays per game.", "count", True, "OFF", "mean", lo=30, hi=120, group="PACE")
_add("off_sec_per_play", "Seconds/Play", "Average seconds between offensive snaps within a drive.", "seconds", False, "OFF", "wmean:off_plays", lo=10, hi=60, group="PACE")
_add("off_neutral_sec_per_play", "Neutral-Script Seconds/Play", "Seconds per play when the score is within 8 in the first three quarters.", "seconds", False, "OFF", "wmean:off_plays", lo=10, hi=60, group="PACE")
_add("off_pass_rate", "Pass Rate", "Dropbacks per scrimmage play.", "pct", True, "OFF", "wmean:off_plays", lo=0, hi=1, group="STYLE")
_add("off_early_down_pass_rate", "Early-Down Pass Rate", "Dropbacks per play on 1st and 2nd down.", "pct", True, "OFF", "wmean:off_plays", lo=0, hi=1, group="STYLE")
_add("off_neutral_pass_rate", "Neutral-Script Pass Rate", "Dropbacks per play when the game is within 8 points in the first three quarters.", "pct", True, "OFF", "wmean:off_plays", lo=0, hi=1, group="STYLE")
# ---- NFL charting ------------------------------------------------------------------------
for k, lab, d in (("off_play_action_rate", "Play-Action Rate", "Play-action dropbacks per dropback (FTN)."), ("off_rpo_rate", "RPO Rate", "RPO plays per scrimmage play (FTN)."),
                  ("off_shotgun_rate", "Shotgun Rate", "Snaps from shotgun per scrimmage play."), ("off_avg_air_yards", "Avg Air Yards", "Average depth of target on pass attempts."),
                  ("off_deep_pass_rate", "Deep Pass Rate", "Share of attempts thrown 20+ yards downfield.")):
    _add(k, lab, d, "pct" if "rate" in k else "yards", True, "OFF", "wmean:off_plays", leagues=("NFL",), source="nflverse|ftn", lo=0, hi=1 if "rate" in k else 20, group="STYLE")
_add("def_blitz_rate", "Blitz Rate", "Share of opponent dropbacks with 5+ pass rushers (FTN).", "pct", True, "DEF", "wmean:def_plays", leagues=("NFL",), source="ftn", lo=0, hi=1, group="STYLE")
_add("def_pressure_no_blitz_rate", "Pressure Without Blitz", "QB hits or sacks per opponent dropback with 4 or fewer rushers (proxy).", "pct", True, "DEF", "wmean:def_plays", leagues=("NFL",), source="derived", lo=0, hi=1, group="STYLE")

REGISTRY = pd.DataFrame(_M)
KEYS = REGISTRY.metric_key.tolist()


def write_registry() -> int:
    path = config.TABLES / "ref" / "metric_definitions.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.to_csv(path, index=False)
    return len(REGISTRY)
