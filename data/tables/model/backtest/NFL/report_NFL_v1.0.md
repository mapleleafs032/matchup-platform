# Backtest report — NFL — NFL_v1.0

Generated 2026-09-07T04:35+00:00. Walk-forward by season: each season predicted by a model fit only on earlier seasons.
**2021 has no earlier data; it was predicted by a model fit on the later seasons and is NOT out-of-sample. Treat it as a smoke test only.**

## Overall (out-of-sample seasons)
- games: 1087
- margin MAE: **10.07** (RMSE 13.0)
- winner accuracy: **0.617**
- total MAE: 10.66
- residual SD used for win probability: 13.16

Baselines on the same games (lower MAE is better):
- home-field only: 10.76
- opponent-adjusted rating diff + HFA: 10.07
- full model: 10.07

## Versus the closing line (1087 games with a closing spread)
- market MAE on those games: **9.49** vs model MAE **10.07**
- market winner accuracy: 0.675 vs model 0.617
- model side vs closing spread, all games: 0.488 (1058 decided)
- model side vs closing spread when model differs by >= 2.0 pts: 0.513 (602 decided)
- over/under: 0.489
- correlation model margin vs market margin: 0.784

Break-even against -110 pricing is 52.4%. Anything below that is not an edge; anything above it on a few hundred games is not proof either.

## By season
| season | n | MAE | winner acc | market MAE | ATS all | ATS edge |
|---|---|---|---|---|---|---|
| 2022 | 271 | 9.23 | 0.602 | 8.74 | 0.498 | 0.558 |
| 2023 | 272 | 10.56 | 0.618 | 9.9 | 0.45 | 0.5 |
| 2024 | 272 | 10.24 | 0.625 | 9.61 | 0.504 | 0.51 |
| 2025 | 272 | 10.23 | 0.624 | 9.72 | 0.498 | 0.484 |

## By week bucket
| weeks | n | MAE | winner acc |
|---|---|---|---|
| W1-3 | 192 | 10.26 | 0.597 |
| W4-8 | 297 | 10.66 | 0.571 |
| W9+ | 598 | 9.7 | 0.647 |

## Calibration (home win probability)
| bin | n | predicted | actual |
|---|---|---|---|
| 0.2-0.3 | 18 | 0.265 | 0.333 |
| 0.3-0.4 | 104 | 0.361 | 0.375 |
| 0.4-0.5 | 217 | 0.458 | 0.392 |
| 0.5-0.6 | 351 | 0.548 | 0.501 |
| 0.6-0.7 | 259 | 0.645 | 0.703 |
| 0.7-0.8 | 122 | 0.738 | 0.738 |
| 0.8-0.9 | 16 | 0.825 | 0.938 |

## Fitted weights (points per raw unit of each edge; the matchup engine displays these)
| feature | points/unit |
|---|---|
| TURNOVER | +0.744 |
| REST | +0.592 |
| OFFENSIVE_LINE | +0.582 |
| STYLE_FIT | -0.525 |
| SUCCESS | +0.467 |
| DEFENSIVE_FRONT | +0.451 |
| RUSH_OFF | +0.451 |
| OVERALL_OFF | +0.379 |
| PASS_DEF | +0.373 |
| OVERALL_DEF | +0.290 |
| RUSH_DEF | +0.232 |
| home_field | -0.218 |
| EXPLOSIVE | +0.213 |
| rating_diff_blend | +0.200 |
| RED_ZONE | +0.177 |
| PASS_OFF | +0.153 |
| SOS | -0.109 |
| THIRD_DOWN | +0.067 |
| RECENT_FORM | -0.045 |
| QB | +0.000 |
| SPECIAL_TEAMS | +0.000 |
| COACHING | +0.000 |
| TALENT | +0.000 |
| RETURNING_PROD | +0.000 |
| INJURY | +0.000 |
| WEATHER | +0.000 |