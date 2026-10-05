# Model methodology: nfl-ridge-baseline

**Status:** challenger (shadow). Not validated. Cannot produce official plays.

## What it predicts
Home margin and game total for NFL games; from these, moneyline,
spread-cover and over/under probabilities (with push probability on
whole-number lines) and 90% probability intervals.

## Data
Final scores from nflverse `games.csv` only. No market prices, no player
availability, no play-level data, no weather. Columns describing what actually
happened (starting QBs, game-time temperature and wind) are excluded because
they are not known before kickoff.

## Method
- Weighted ridge regression on past margins: `margin = hfa + r_home - r_away`.
- Separate ridge regression on totals: `total = mu + t_home + t_away`.
- Game weight `0.5 ** (age_days / half_life)`; 800-day lookback, so early
  season predictions lean on the prior season automatically.
- Ratings shrink toward league average (ridge penalty).
- Outcome distribution: Normal(projection, sqrt(game_sd^2 + parameter_se^2))
  with a continuity correction for integer scores.
- Uncertainty: parameter covariance from the ridge fit, propagated to a 90%
  interval on every probability.

## Training and validation (protocol fixed in advance)
| Split | Seasons | Use |
| --- | --- | --- |
| Train | 2006-2017 | Grid search: half-life {120,180,240,365} d, margin ridge {2,5,10,20}, total ridge {5,10,20,40}, selected by out-of-sample RMSE; game SDs from out-of-sample residuals |
| Validation | 2018-2022 | Report only; used to fit the market-informed weight |
| Test | 2023-2025 | Evaluated once |

Chosen: half-life 180 days, margin ridge 5, total ridge 10, margin SD 12.82,
total SD 12.94. Version string: `nfl-ridge-baseline/hl180-rm5-rt10-sd12.82-12.94`.

Every week is predicted from a fit whose cutoff is one minute before that
week's first kickoff, using only results available by then. A replay test
verifies that altering every later result leaves predictions unchanged.

## Calibration
Slope 1.10-1.18 across splits (1.0 is ideal): probabilities are slightly
under-confident. Not recalibrated, because the model is not used for plays.

## Abstains when
Fewer than 128 games in the window, or either team has no game in the window.

## Limitations
- Less accurate than the closing market on every metric (see results).
- Normal margins ignore key numbers (3, 7); spread probabilities near those
  numbers are approximate.
- Purpose: a defensible baseline and a working end-to-end pipeline, not an edge.
