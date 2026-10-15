"""
Turn parsed VSiN linetracker games into market snapshots.

Two jobs, in order:

  resolve()   match each parsed game to a game_id in OUR schedules table
  snapshots() emit one market_snapshots row per (game, book)

Why resolving by the PAIR and not by team name
----------------------------------------------
The linetracker shows plain display names with no team link, so a name is all there is to go on.
Matching names one at a time is how a wrong row gets written: the existing slug matcher already
resolved "texas-san-antonio-roadrunners" to CFB_TEX (Texas) by trimming tokens until something
matched, which produced "no scheduled game for CFB_NAVY vs CFB_TEX". A single name is simply not
constrained enough.

So nothing here resolves a name on its own. Each label produces a SET of candidate team_ids, and a
parsed game is accepted only when exactly one scheduled game on that calendar date has its two teams
drawn from those two sets. UTSA mis-reading as Texas then costs nothing: no Texas-vs-Navy game exists
on Oct 17, so the pair is rejected and reported instead of stored against the wrong game.

Ambiguity is never broken by preference. Zero matches and several matches are both refused, with the
candidates printed, because a price filed against the wrong game is worse than a missing price.
"""
from __future__ import annotations

import pathlib
import re
from datetime import datetime, timezone

import pandas as pd

import config
from providers import vsin

# The page prints the game's local (Eastern) date, which is NOT the UTC date for a night game:
# a Monday 8:15 PM ET kickoff is 00:15 UTC on Tuesday. Comparing UTC dates would miss every one.
EASTERN = "America/New_York"
# A date header and a kickoff may still disagree by a day across a timezone edge, so a neighbouring
# day is allowed -- the pair of teams is what identifies the game, the date only narrows the field.
DATE_SLACK_DAYS = 1
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def label_month_day(label: str) -> tuple[int, int] | None:
    """'Sat, Oct 17' -> (10, 17). None when the header cannot be read."""
    m = re.search(r"([A-Z][a-z]{2})\.?\s+(\d{1,2})", str(label or ""))
    if not m or m.group(1) not in _MONTHS:
        return None
    return _MONTHS[m.group(1)], int(m.group(2))


def _name_variants(label: str) -> list[str]:
    """
    Candidate normalized spellings for a display name, reusing the slug machinery.

    'Kennesaw ST' -> kennesaw-st -> {kennesawstate, kennesawst, ...}. The qualifier rules in
    vsin._slug_variants stop a cut from turning Miami OH into Miami.
    """
    # "&" becomes the word before anything else is stripped, so "Texas A&M Aggies" turns into
    # texas-a-and-m-aggies and lands on the same key as VSiN's own spelling of the slug.
    cleaned = str(label or "").replace("&", " and ")
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-")
    return vsin._slug_variants(slug) if slug else []


NAMES_CSV = config.DATA / "manual" / "vsin_names.csv"
UNMAPPED_CSV = config.DATA / "manual" / "vsin_unmapped_names_{league}.csv"


def _manual_names(league: str) -> dict[str, str]:
    """
    Hand-recorded label -> team_id, for names no spelling rule can bridge.

    "Texas-San Antonio Roadrunners" against a table that calls them UTSA is not a trimming problem:
    the two names share no tokens, so it has to be written down rather than derived. The file is the
    place to write it down; the job reports every label it could not resolve so the list stays short.
    """
    if not NAMES_CSV.exists():
        return {}
    try:
        df = pd.read_csv(NAMES_CSV)
    except (OSError, ValueError):
        return {}
    df = df[df.league.astype(str).str.upper() == league.upper()]
    return {str(r.label).strip(): str(r.team_id).strip()
            for r in df.itertuples() if str(r.team_id).strip()}


def name_candidates(label: str, league: str, teams: pd.DataFrame) -> set[str]:
    """
    EVERY team_id whose name could be this label -- deliberately a set, never a single best guess.

    Returning several is the point: the pair match below is what narrows it down, and a lone name
    that matched several teams is exactly the case where picking one would be a silent error.
    """
    manual = _manual_names(league).get(str(label).strip())
    if manual:
        return {manual}
    if teams is None or teams.empty:
        return set()
    t = teams[teams.league == league]
    lookup: dict[str, set[str]] = {}
    for _, x in t.iterrows():
        forms = {x.get("display_name"), x.get("school_or_city"), x.get("abbr")}
        if pd.notna(x.get("mascot")):
            forms.add(f"{x.get('school_or_city')} {x.get('mascot')}")
        for f in forms:
            if isinstance(f, str) and f.strip():
                lookup.setdefault(vsin.slug_key(f), set()).add(x.team_id)
    out: set[str] = set()
    for key in _name_variants(label):
        if key in lookup:
            out |= lookup[key]
            break                 # variants run longest-first; the longest match is the specific one
    return out


def resolve(games: list[dict], league: str, season: int, schedule: pd.DataFrame,
            teams: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """
    Attach a game_id (and which side is home) to each parsed game.

    Returns (resolved, problems). A resolved entry carries game_id, home_team_id, away_team_id,
    home_label, away_label and the original books. Anything not matched to exactly one scheduled
    game is left out and explained.
    """
    resolved: list[dict] = []
    problems: list[dict] = []
    unmapped: set[str] = set()
    if schedule is None or schedule.empty:
        return resolved, [{"kind": "no_schedule", "why": f"no games table for {league} {season}"}]
    s = schedule.copy()
    kick = pd.to_datetime(s.kickoff_utc, utc=True, errors="coerce")
    s["_md"] = [None if pd.isna(k) else (k.tz_convert(EASTERN).month, k.tz_convert(EASTERN).day)
                for k in kick]
    for g in games:
        if g.get("n_teams") != 2:
            problems.append({"kind": "unpaired", "why": f"{g.get('open_label', '?')[:40]!r} has "
                                                        f"{g.get('n_teams')} sides, expected 2"})
            continue
        (la, ba), (lb, bb) = g["teams"]
        ca, cb = name_candidates(la, league, teams), name_candidates(lb, league, teams)
        if not ca or not cb:
            for lbl, c in ((la, ca), (lb, cb)):
                if not c:
                    unmapped.add(lbl)
            problems.append({"kind": "unmapped_name",
                             "why": f"no team matches {la!r}" if not ca else f"no team matches {lb!r}"})
            continue
        md = label_month_day(g.get("date"))
        if md is None:
            cand = s
        else:
            keep = []
            for i, x in zip(s.index, s._md):
                if x is None:
                    continue
                if abs((x[0] - md[0]) * 31 + (x[1] - md[1])) <= DATE_SLACK_DAYS:
                    keep.append(i)
            cand = s.loc[keep]
        hits = cand[((cand.away_team_id.isin(ca)) & (cand.home_team_id.isin(cb)))
                    | ((cand.away_team_id.isin(cb)) & (cand.home_team_id.isin(ca)))]
        if hits.empty:
            problems.append({"kind": "no_scheduled_game",
                             "why": f"{la} vs {lb} on {g.get('date')}: no scheduled game "
                                    f"(candidates {sorted(ca)} / {sorted(cb)})"})
            continue
        if len(hits) > 1:
            problems.append({"kind": "ambiguous_game",
                             "why": f"{la} vs {lb} on {g.get('date')} matched "
                                    f"{len(hits)} scheduled games: {list(hits.game_id)[:4]} -- refused"})
            continue
        row = hits.iloc[0]
        home_is_b = row.home_team_id in cb
        resolved.append({**g,
                         "game_id": row.game_id, "week": int(row.week), "status": row.status,
                         "kickoff_utc": row.kickoff_utc,
                         "home_team_id": row.home_team_id, "away_team_id": row.away_team_id,
                         "home_label": lb if home_is_b else la,
                         "away_label": la if home_is_b else lb,
                         "home_books": bb if home_is_b else ba,
                         "away_books": ba if home_is_b else bb})
    if unmapped:
        out = pathlib.Path(str(UNMAPPED_CSV).format(league=league))
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"league": league, "label": sorted(unmapped), "team_id": "", "note": ""}
                     ).to_csv(out, index=False)
        problems.append({"kind": "unmapped_names_written",
                         "why": f"{len(unmapped)} display name(s) matched no team; fill team_id in "
                                f"{out.name} and paste the rows into data/manual/vsin_names.csv: "
                                + ", ".join(sorted(unmapped)[:8])})
    return resolved, problems


def _ml(book: dict | None) -> float | None:
    return None if not book else book.get("ml_price")


def snapshots(resolved: list[dict], retrieved_at: datetime | None = None) -> list[dict]:
    """
    One market_snapshots row per (game, book), in the schema the rest of the pipeline already reads.

    Spread convention: spread_home NEGATIVE means home favoured, matching the whole codebase. The
    page prints each side's own number, so the home row's spread is taken as-is -- the parser has
    already cross-checked that the two sides are opposites at every book.

    The OPEN row travels alongside as provider_open_spread_home / provider_open_total, so the
    opening and current numbers live on the same row and closing-line value has both ends.
    """
    ts = pd.Timestamp(retrieved_at or datetime.now(timezone.utc))
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    out: list[dict] = []
    for g in resolved:
        home, away, opens = g["home_books"], g["away_books"], g.get("open") or {}
        # The open row is one side's view; which side is unknowable from the row itself, so it is
        # stored only where the sign can be trusted -- against the home spread the page itself shows.
        for book in sorted(set(home) | set(away)):
            h, a = home.get(book) or {}, away.get(book) or {}
            spread_home = h.get("spread")
            if spread_home is None and a.get("spread") is not None:
                spread_home = -a["spread"]
            total = h.get("total", a.get("total"))
            if spread_home is None and total is None and _ml(h) is None and _ml(a) is None:
                continue
            op = opens.get(book) or {}
            out.append({
                "snapshot_id": f"{g['game_id']}_{book}_{stamp}",
                "game_id": g["game_id"], "retrieved_at": ts.isoformat(),
                "provider_updated_at": None, "book": book,
                "spread_home": spread_home,
                "spread_home_price": h.get("spread_price"),
                "spread_away_price": a.get("spread_price"),
                "ml_home": _ml(h), "ml_away": _ml(a),
                "total": total,
                "over_price": h.get("over_price", a.get("over_price")),
                "under_price": h.get("under_price", a.get("under_price")),
                "spread_ticket_pct_home": None, "spread_money_pct_home": None,
                "ml_ticket_pct_home": None, "ml_money_pct_home": None,
                "total_ticket_pct_over": None, "total_money_pct_over": None,
                "is_first_snapshot": False,
                "provider_open_spread_home": op.get("spread") if h.get("spread") is not None else None,
                "provider_open_total": op.get("total"),
                "source": "vsin_linetracker", "plan": "subscription",
            })
    return out


def movement(resolved: list[dict]) -> list[dict]:
    """
    The arrows, as rows: which book moved which market, and in which direction, per side.

    VSiN marks a number that just moved. Across eight books that is a far better steam signal than
    comparing our own snapshots, because it is the books' own flag rather than our sampling cadence --
    a move between two of our pulls is invisible, a move the page marked is not.
    """
    out: list[dict] = []
    for g in resolved:
        for side, books in (("home", g["home_books"]), ("away", g["away_books"])):
            for book, v in (books or {}).items():
                for market, key in (("SPREAD", "spread_moved"), ("MONEYLINE", "ml_moved"),
                                    ("TOTAL", "total_moved")):
                    if v.get(key):
                        out.append({"game_id": g["game_id"], "book": book, "side": side,
                                    "market": market, "direction": v[key],
                                    "value": v.get({"SPREAD": "spread", "MONEYLINE": "ml_price",
                                                    "TOTAL": "total"}[market])})
    return out


def write(rows: list[dict], league: str, season: int, schedule: pd.DataFrame) -> int:
    """Append snapshot rows to the per-week market files. Returns rows written."""
    from pipeline import storage
    if not rows:
        return 0
    wk = schedule.set_index("game_id").week.to_dict()
    written = 0
    by_week: dict[int, list[dict]] = {}
    for r in rows:
        w = wk.get(r["game_id"])
        if w is None:
            continue
        by_week.setdefault(int(w), []).append(r)
    for w, part in by_week.items():
        path = config.TABLES / "market" / "snapshots" / league / str(season) / f"W{w:02d}.csv"
        written += storage.append_csv(path, pd.DataFrame(part), ["snapshot_id"], on_duplicate="skip")
    return written
