# Betting splits — manual input

Drop a plain-text file here holding a splits table you have access to. One file per snapshot.

**File name:** `{LEAGUE}_{PERIOD}_{anything}.txt` — for example
`NFL_FULL_wed-am.txt`, `NFL_1H_thu.txt`, `CFB_FULL_fri.txt`.
`PERIOD` is `FULL` (whole game) or `1H` (first half). Omit it and FULL is assumed.

**Contents:** paste the table as-is. The parser reads each line looking for a team name followed by
percentages, and pairs consecutive lines into games. Column order is assumed to be
spread bets %, spread handle %, total bets %, total handle %, moneyline bets %, moneyline handle %.
Anything it cannot read is reported in the job log and skipped — it never guesses.

**Timestamp:** the file's modified time is used, unless the first line contains an ISO timestamp
(`2026-09-10T14:30`), in which case that wins. Put one there if you are entering a paste after the fact.

Rows for games that have already kicked off are ignored, so a late paste can never alter a locked game.

Every row loaded this way is stamped `manual_paste` and is badged as manual in the app.
