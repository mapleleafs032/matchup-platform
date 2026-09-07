# Backtest report — NFL — NFL_v1.0

Generated 2026-09-07T06:56+00:00. Walk-forward by season: each season predicted by a model fit only on earlier seasons.
**2021 has no earlier data; it was predicted by a model fit on the later seasons and is NOT out-of-sample. Treat it as a smoke test only.**

## Overall (out-of-sample seasons)
- games: 1087
- margin MAE: **9.92** (RMSE 12.84)
- winner accuracy: **0.634**
- total MAE: 10.66
- residual SD used for win probability: 13.00

Baselines on the same games (lower MAE is better):
- home-field only: 10.76
- opponent-adjusted rating diff + HFA: 10.07
- full model: 9.92

## Versus the closing line (1087 games with a closing spread)
- market MAE on those games: **9.49** vs model MAE **9.92**
- market winner accuracy: 0.675 vs model 0.634
- model side vs closing spread, all games: 0.504 (1058 decided)
- model side vs closing spread when model differs by >= 2.0 pts: 0.514 (552 decided)
- over/under: 0.495
- correlation model margin vs market margin: 0.831

Break-even against -110 pricing is 52.4%. Anything below that is not an edge; anything above it on a few hundred games is not proof either.

## By season
| season | n | MAE | winner acc | market MAE | ATS all | ATS edge |
|---|---|---|---|---|---|---|
| 2022 | 271 | 8.98 | 0.613 | 8.74 | 0.548 | 0.531 |
| 2023 | 272 | 10.41 | 0.621 | 9.9 | 0.465 | 0.523 |
| 2024 | 272 | 10.06 | 0.676 | 9.61 | 0.496 | 0.525 |
| 2025 | 272 | 10.21 | 0.624 | 9.72 | 0.506 | 0.478 |

## By week bucket
| weeks | n | MAE | winner acc |
|---|---|---|---|
| W1-3 | 192 | 10.04 | 0.623 |
| W4-8 | 297 | 10.41 | 0.605 |
| W9+ | 598 | 9.63 | 0.652 |

## Calibration (home win probability)
| bin | n | predicted | actual |
|---|---|---|---|
| 0.2-0.3 | 19 | 0.261 | 0.316 |
| 0.3-0.4 | 85 | 0.36 | 0.318 |
| 0.4-0.5 | 251 | 0.457 | 0.382 |
| 0.5-0.6 | 346 | 0.549 | 0.52 |
| 0.6-0.7 | 253 | 0.648 | 0.715 |
| 0.7-0.8 | 116 | 0.743 | 0.741 |
| 0.8-0.9 | 17 | 0.832 | 1.0 |

## Fitted weights (points per raw unit of each edge; the matchup engine displays these)
| feature | points/unit |
|---|---|
| INJURY | +1.031 |
| RETURNING_PROD | +0.929 |
| WEATHER | -0.752 |
| TURNOVER | +0.621 |
| REST | +0.588 |
| QB | +0.588 |
| COACHING | +0.573 |
| STYLE_FIT | -0.510 |
| OFFENSIVE_LINE | +0.506 |
| RUSH_OFF | +0.428 |
| SUCCESS | +0.418 |
| DEFENSIVE_FRONT | +0.379 |
| OVERALL_OFF | +0.313 |
| PASS_DEF | +0.303 |
| RUSH_DEF | +0.235 |
| OVERALL_DEF | +0.228 |
| SOS | -0.176 |
| rating_diff_blend | +0.168 |
| EXPLOSIVE | +0.158 |
| RED_ZONE | +0.130 |
| RECENT_FORM | -0.130 |
| PASS_OFF | +0.091 |
| home_field | +0.047 |
| THIRD_DOWN | +0.017 |
| SPECIAL_TEAMS | +0.000 |
| TALENT | +0.000 |