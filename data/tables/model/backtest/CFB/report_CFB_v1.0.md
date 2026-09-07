# Backtest report — CFB — CFB_v1.0

Generated 2026-09-07T15:08+00:00. Walk-forward by season: each season predicted by a model fit only on earlier seasons.
**2021 has no earlier data; it was predicted by a model fit on the later seasons and is NOT out-of-sample. Treat it as a smoke test only.**

## Overall (out-of-sample seasons)
- games: 3014
- margin MAE: **12.71** (RMSE 16.09)
- winner accuracy: **0.715**
- total MAE: 12.96
- residual SD used for win probability: 16.10

Baselines on the same games (lower MAE is better):
- home-field only: 15.79
- opponent-adjusted rating diff + HFA: 13.0
- full model: 12.71

## Versus the closing line (2215 games with a closing spread)
- market MAE on those games: **12.04** vs model MAE **12.89**
- market winner accuracy: 0.73 vs model 0.715
- model side vs closing spread, all games: 0.502 (2178 decided)
- model side vs closing spread when model differs by >= 3.0 pts: 0.495 (1184 decided)
- over/under: 0.51
- correlation model margin vs market margin: 0.91

Break-even against -110 pricing is 52.4%. Anything below that is not an edge; anything above it on a few hundred games is not proof either.

## By season
| season | n | MAE | winner acc | market MAE | ATS all | ATS edge |
|---|---|---|---|---|---|---|
| 2022 | 741 | 13.43 | 0.698 | 12.05 | 0.471 | 0.479 |
| 2023 | 754 | 12.34 | 0.735 | 11.5 | 0.345 | 0.391 |
| 2024 | 756 | 12.95 | 0.705 | 12.22 | 0.497 | 0.482 |
| 2025 | 763 | 12.14 | 0.723 | 11.87 | 0.546 | 0.541 |

## By week bucket
| weeks | n | MAE | winner acc |
|---|---|---|---|
| W1-3 | 602 | 13.7 | 0.752 |
| W4-8 | 1087 | 12.42 | 0.693 |
| W9+ | 1325 | 12.49 | 0.717 |

## Calibration (home win probability)
| bin | n | predicted | actual |
|---|---|---|---|
| 0.0-0.1 | 47 | 0.072 | 0.085 |
| 0.1-0.2 | 150 | 0.152 | 0.2 |
| 0.2-0.3 | 260 | 0.253 | 0.262 |
| 0.3-0.4 | 304 | 0.351 | 0.329 |
| 0.4-0.5 | 344 | 0.453 | 0.451 |
| 0.5-0.6 | 394 | 0.55 | 0.546 |
| 0.6-0.7 | 420 | 0.649 | 0.652 |
| 0.7-0.8 | 419 | 0.749 | 0.759 |
| 0.8-0.9 | 389 | 0.85 | 0.861 |
| 0.9-1.0 | 287 | 0.942 | 0.944 |

## Fitted weights (points per raw unit of each edge; the matchup engine displays these)
| feature | points/unit |
|---|---|
| TALENT | +7.582 |
| WEATHER | -4.672 |
| home_field | +3.117 |
| OVERALL_OFF | +2.057 |
| PASS_OFF | -1.442 |
| RETURNING_PROD | +1.347 |
| OVERALL_DEF | +1.256 |
| SUCCESS | +0.714 |
| DEFENSIVE_FRONT | +0.652 |
| REST | +0.646 |
| EXPLOSIVE | +0.637 |
| rating_diff_blend | +0.628 |
| OFFENSIVE_LINE | +0.628 |
| SOS | +0.613 |
| RUSH_OFF | +0.585 |
| QB | +0.430 |
| RECENT_FORM | -0.398 |
| RED_ZONE | -0.297 |
| TURNOVER | +0.156 |
| PASS_DEF | -0.134 |
| STYLE_FIT | +0.084 |
| RUSH_DEF | +0.033 |
| THIRD_DOWN | +0.002 |
| SPECIAL_TEAMS | +0.000 |
| COACHING | +0.000 |
| INJURY | +0.000 |