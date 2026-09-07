# Backtest report — CFB — CFB_v1.0

Generated 2026-09-07T04:35+00:00. Walk-forward by season: each season predicted by a model fit only on earlier seasons.
**2021 has no earlier data; it was predicted by a model fit on the later seasons and is NOT out-of-sample. Treat it as a smoke test only.**

## Overall (out-of-sample seasons)
- games: 3014
- margin MAE: **12.94** (RMSE 16.41)
- winner accuracy: **0.712**
- total MAE: 12.95
- residual SD used for win probability: 16.55

Baselines on the same games (lower MAE is better):
- home-field only: 15.79
- opponent-adjusted rating diff + HFA: 13.0
- full model: 12.94

## By season
| season | n | MAE | winner acc | market MAE | ATS all | ATS edge |
|---|---|---|---|---|---|---|
| 2022 | 741 | 13.37 | 0.692 | — | — | — |
| 2023 | 754 | 12.64 | 0.733 | — | — | — |
| 2024 | 756 | 13.27 | 0.696 | — | — | — |
| 2025 | 763 | 12.5 | 0.726 | — | — | — |

## By week bucket
| weeks | n | MAE | winner acc |
|---|---|---|---|
| W1-3 | 602 | 15.15 | 0.719 |
| W4-8 | 1087 | 12.44 | 0.691 |
| W9+ | 1325 | 12.35 | 0.726 |

## Calibration (home win probability)
| bin | n | predicted | actual |
|---|---|---|---|
| 0.0-0.1 | 41 | 0.07 | 0.049 |
| 0.1-0.2 | 107 | 0.161 | 0.187 |
| 0.2-0.3 | 234 | 0.254 | 0.214 |
| 0.3-0.4 | 337 | 0.352 | 0.356 |
| 0.4-0.5 | 351 | 0.451 | 0.442 |
| 0.5-0.6 | 457 | 0.551 | 0.554 |
| 0.6-0.7 | 501 | 0.648 | 0.659 |
| 0.7-0.8 | 437 | 0.75 | 0.778 |
| 0.8-0.9 | 347 | 0.847 | 0.888 |
| 0.9-1.0 | 202 | 0.94 | 0.95 |

## Fitted weights (points per raw unit of each edge; the matchup engine displays these)
| feature | points/unit |
|---|---|
| home_field | +4.157 |
| OVERALL_OFF | +2.553 |
| PASS_OFF | -1.748 |
| OVERALL_DEF | +1.270 |
| rating_diff_blend | +0.911 |
| SOS | +0.836 |
| TURNOVER | -0.792 |
| SUCCESS | +0.763 |
| DEFENSIVE_FRONT | +0.684 |
| OFFENSIVE_LINE | +0.524 |
| RED_ZONE | -0.475 |
| EXPLOSIVE | +0.473 |
| STYLE_FIT | +0.335 |
| REST | +0.333 |
| RUSH_OFF | +0.322 |
| RECENT_FORM | -0.155 |
| PASS_DEF | -0.125 |
| THIRD_DOWN | -0.097 |
| RUSH_DEF | -0.093 |
| QB | +0.000 |
| SPECIAL_TEAMS | +0.000 |
| COACHING | +0.000 |
| TALENT | +0.000 |
| RETURNING_PROD | +0.000 |
| INJURY | +0.000 |
| WEATHER | +0.000 |