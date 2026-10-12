# Connecting your VSiN subscription

## Why this is needed

VSiN's **public** splits page shows only the *current day's* card, wrapped in a paywall teaser. Measured
on 2026-10-05, signed out:

| page | team rows | header |
|---|---|---|
| `data.vsin.com/betting-splits/` | 2 | `NFL - Monday, Oct 5` |
| `betting-splits/?view=week` | 0 | — |
| `data.vsin.com/nfl/games/` | 0 | `8:15 PM ET (ESPN)` |
| `nfl/vegas-odds-linetracker/` | 0 | `Mon, Oct 5 · Circa · Boomers · BetMGM · Caesars` |

All four carried `inline-lock` / `template-shown` paywall markup. The parser was never broken — the page
simply did not contain the week. That is why the site showed "1 of 16 games have splits" for days.

A VSiN Pro subscription includes the DraftKings and Circa splits, so the fix is to present **your own
signed-in session**. Nothing here defeats the paywall; it uses the access you pay for.

## Step 1 — get your session cookie

1. Sign in at <https://vsin.com> in Chrome. **Tick "Remember me"** — without it the session expires in
   about two days instead of about two weeks.
2. Go to <https://data.vsin.com/betting-splits/>. Confirm you can see the full week of games.
3. Press **F12** → **Network** tab → reload the page.
4. Click the first `betting-splits` row (the one whose Type is `document`).
5. Under **Headers → Request Headers**, find the line beginning `cookie:` and copy **the whole value**
   after `cookie:` — it will be a long `name=value; name=value; …` string.

## Step 2 — store it as a repository secret

GitHub → your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value |
|---|---|
| `VSIN_COOKIE` | the string you copied |

Do not commit it, do not paste it into a chat, and do not put it in a workflow file. Actions secrets are
encrypted, are masked in logs, and are not passed to workflows triggered from forks.

**Alternative (self-renewing, more fragile):** set `VSIN_EMAIL` and `VSIN_PASSWORD` instead and the job
logs in each run. It stops working the day VSiN adds a CAPTCHA to the login form; the cookie does not.
If both are set, the cookie wins and the password is never sent.

## Step 3 — add the secrets to the workflow

Workflow files cannot ship inside a code bundle, so add this to `.github/workflows/splits.yml`, in the
step that runs `ingest_splits` (alongside the existing `env:` entries):

```yaml
      - name: Betting splits
        env:
          CFBD_API_KEY: ${{ secrets.CFBD_API_KEY }}
          VSIN_COOKIE:  ${{ secrets.VSIN_COOKIE }}
          VSIN_EMAIL:   ${{ secrets.VSIN_EMAIL }}
          VSIN_PASSWORD: ${{ secrets.VSIN_PASSWORD }}
        run: python -m pipeline.jobs.ingest_splits --league BOTH --trigger ${{ inputs.trigger || 'schedule' }}
```

An unset secret becomes an empty string, so leaving `VSIN_EMAIL` / `VSIN_PASSWORD` in place costs
nothing if you only use the cookie.

## Step 4 — check it worked

Run the splits workflow with `--explain auth`:

```
python -m pipeline.jobs.ingest_splits --league NFL --explain auth
```

What you want to see:

```
VSiN NFL: credentials configured = True (mode cookie)
    VSiN session: cookie auth, signed in, no paywall markup
    day page as this session sees it: 32 team rows, game days [...]
    scope survey (minimum wanted: 16 team rows):
      {'label': 'splits week', 'rows': 32, 'days': 3, 'gated': False}
    best page: 'splits week'
```

If it says **NOT signed in**, the cookie did not take. Most likely causes, in order: the string was
truncated on copy, the sign-in did not have "Remember me" ticked, or the session has already expired.

## What changes once it is signed in

* **Splits arrive for the whole board**, not just today's card. Market evidence is 50% of why a play is
  picked, and under the confirmation rule a game with no splits can only ever be a *lean* — so this
  was capping the pick slate, not only the charts.
* **The collector's state is reported.** Every pull records whether it was signed in; the Odds tab says
  so, and an expired cookie now **fails loudly and writes nothing** rather than storing today's one
  game as if it were the slate.
* **The page scope is learned, not assumed.** The job tries the week view first and remembers which page
  carried the most games, re-checking daily and immediately whenever the page shrinks.

## Still to pin down: the linetracker

The linetracker header names **Circa, Boomers, BetMGM and Caesars** — the first multi-book pricing this
platform has had, which is what line shopping and a trustworthy closing number need. The cell format
cannot be read from a paywalled page, so nothing guesses it. Once the cookie is in place, run:

```
python -m pipeline.jobs.ingest_splits --league NFL --explain linetracker
```

and send the output. It dumps the header, the books it found and the first team row's raw cells; the
parser gets written from that and `config.VSIN_LINETRACKER["enabled"]` is flipped on.

## One thing to check yourself

A subscription grants *you* access; it does not by itself grant programmatic access. VSiN's terms were
not reachable to confirm either way. This collector runs at a handful of requests per hour against pages
you pay for, stores the data privately, and republishes none of it — but if you want to be sure, ask
VSiN support whether automated personal collection is acceptable on your plan. Setting
`config.VSIN["enabled"] = False` stops it immediately.
