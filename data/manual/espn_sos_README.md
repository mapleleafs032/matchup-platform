# ESPN strength of schedule — manual entry

ESPN's public power-index endpoint does **not** carry the SOS rank. Its `resume` array is unlabelled,
and checked against two known ranks (Texas State 1st, Clemson 5th) no column matches both: Clemson's
real rank of 5 appears nowhere in its array. So there is nothing to read automatically, and guessing a
column would put a confidently wrong number on every matchup page.

If you want ESPN's numbers rather than this platform's own, create `espn_sos.csv` beside this file:

```
team,sos_rank
Texas State,1
Clemson,5
Alabama,18
```

`team` may be the ESPN team name, one of our team_ids (`CFB_CLEM`), or any alias already known to the
platform. Anything unrecognised is skipped and reported rather than guessed. Teams not listed fall back
to the platform's own opponent-rating SOS, and the matchup page always names which source it used.

Without this file, the SOS row shows this platform's own strength of schedule: the mean opponent rating
faced, from the same ridge fit that drives the rest of the model, expressed as a rank.
