# NFL baseline backtest - 2026-10-05

Model `nfl-ridge-baseline/hl180-rm5-rt10-sd12.82-12.94`. Full numbers:
`2026-10-05-nfl-baseline-backtest.json` (reproduced exactly by two runs).
Lines are nflverse closing lines (source undocumented). Bets are simulated at
those closing prices with recorded odds; games without odds are skipped.

## Accuracy vs the market (lower is better)

| Split | Games | Margin MAE model / market | Total MAE model / market | ML log loss model / market / home-rate |
| --- | --- | --- | --- | --- |
| Train 2006-17 | 3,204 | 10.70 / 10.52 | 10.75 / 10.60 | 0.623 / 0.610 / 0.684 |
| Validation 2018-22 | 1,372 | 10.13 / 9.86 | 10.98 / 10.60 | 0.636 / 0.610 / 0.690 |
| Test 2023-25 | 855 | 10.32 / 9.79 | 10.39 / 10.12 | 0.640 / 0.608 / 0.688 |

The model beats a naive home-win-rate forecast but trails the closing market
in every split. Its margin is about 0.5 points less accurate than the line on
the test seasons.

## Betting at the closing line, model side with EV >= 0 (one of three thresholds reported)

| Split | Market | Bets | W-L-P | Win rate (95% CI) | ROI (90% CI) |
| --- | --- | --- | --- | --- | --- |
| Train | Spread | 2,754 | 1390-1294-70 | 51.8% (49.9-53.7) | +2.1% (-0.8 to +5.1) |
| Validation | Spread | 1,199 | 599-568-32 | 51.3% (48.5-54.2) | -0.1% (-4.7 to +4.6) |
| Test | Spread | 677 | 310-351-16 | 46.9% (43.1-50.7) | -9.8% (-15.7 to -3.8) |
| Test | Total | 655 | 337-313-5 | 51.8% (48.0-55.7) | -0.8% (-7.1 to +5.4) |

Thresholds +3% and +5% EV show the same pattern (see JSON). Three thresholds
x two markets x three splits were examined; no correction for multiple
comparisons was applied, so isolated "significant" cells should not be trusted.

## Market-informed weight (fitted on validation, n = 1,365)
Logistic regression of home wins on logit(model) and logit(market no-vig):
model coefficient -0.14 (SE 0.18), market coefficient 1.11 (SE 0.14). Given
the market, this model adds no information. The config weight stays `null`,
so under the approved "both must pass" rule this model cannot produce
official plays.

## Conclusion
No evidence of a betting edge; consistent with expectations for a
scores-only model. The pipeline (ingest -> point-in-time features -> model ->
probabilities -> walk-forward evaluation) now works end to end. Improvements
must come from information the scores do not contain: play-level efficiency
(EPA/success rate), quarterback availability, and key-number-aware margins.
