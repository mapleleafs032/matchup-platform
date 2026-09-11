"""
VSiN betting-splits provider (DraftKings action, public splits page).

    https://data.vsin.com/betting-splits/?sport=NFL&source=DK
    https://data.vsin.com/betting-splits/?sport=CFB&source=DK

Table shape observed 2026-09-09. Each game is TWO consecutive rows, away team first, home team second:

    | icon | team link | SPREAD | HANDLE% | BETS% | TOTAL | HANDLE% | BETS% | MONEYLINE | HANDLE% | BETS% |

Note the column order: HANDLE (money) comes BEFORE BETS (tickets) in every group. Date header rows
("NFL - Sunday, Sep 13") separate groups and carry no team link, so they are skipped naturally.

Team identity comes from the team page slug in the row's link (…/nfl/teams/seattle-seahawks), which is a
stable key — far safer than the displayed name ("Wash Commanders"). Slugs resolve through the `vsin`
alias namespace; an unknown slug is reported, never guessed.

Robustness: if the column count or the header labels stop matching what is documented above, the parser
returns nothing and says why, so a silent layout change can never write wrong numbers. Set
config.VSIN["enabled"] = False to turn the source off entirely.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

import pandas as pd

import config
from pipeline import ids
from providers.base import RequestManager

BASE = "https://data.vsin.com/betting-splits/"
SPORT = {"NFL": "NFL", "CFB": "CFB"}
UA = "matchup-platform/1.0 (personal football research project; contact via GitHub)"
# expected header labels, in order, after the team column
EXPECTED = ["spread", "handle", "bets", "total", "handle", "bets", "money", "handle", "bets"]
_TEAM_HREF = re.compile(r"/(?:nfl|college-football)/teams/([a-z0-9\-]+)", re.I)
_PCT = re.compile(r"^\s*(\d{1,3})\s*%\s*$")
_NUMBER = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*$")


@dataclass
class TeamRow:
    slug: str
    name: str
    spread: float | None
    spread_handle: float | None
    spread_bets: float | None
    total: float | None
    total_handle: float | None
    total_bets: float | None
    moneyline: float | None
    ml_handle: float | None
    ml_bets: float | None


def fetch(rm: RequestManager, league: str) -> tuple[str, object]:
    res = rm.get(BASE, params={"sport": SPORT[league], "source": "DK"}, headers={"User-Agent": UA, "Accept": "text/html"},
                 expect_json=False, timeout=45)
    return res.payload, res.retrieved_at


def _cell_text(td) -> str:
    return re.sub(r"\s+", " ", td.get_text(" ", strip=True))


_BLANK = {"", "-", "--", "n/a", "na", "off", "off the board", "pk", "even"}


def _pct(s: str) -> float | None:
    if s.strip().lower() in _BLANK:
        return None
    m = _PCT.match(s)
    return float(m.group(1)) / 100.0 if m else None


def _num(s: str) -> float | None:
    t = s.strip()
    if t.lower() in ("pk", "even", "pick"):
        return 0.0
    if t.lower() in _BLANK:
        return None
    t = t.replace(",", "").replace("+", "")
    return float(t) if _NUMBER.match(t.replace("-", "-", 1)) else None


def parse(html: str) -> tuple[list[TeamRow], list[dict]]:
    """Returns (team rows in page order, problems). Header mismatch aborts with a problem, not bad data."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return [], [{"why": "beautifulsoup4 is not installed; add it to requirements.txt"}]
    soup = BeautifulSoup(html, "html.parser")
    problems: list[dict] = []
    # verify the header still matches the documented layout
    header_ok = False
    for tr in soup.find_all("tr"):
        labels = [_cell_text(c).lower() for c in tr.find_all(["th", "td"])]
        joined = " ".join(labels)
        if "spread" in joined and "handle" in joined and "bets" in joined and "money" in joined:
            order = [w for w in re.findall(r"spread|handle|bets|total|money", joined)]
            if order[:9] == EXPECTED:
                header_ok = True
                break
    if not header_ok:
        problems.append({"why": "VSiN table header no longer matches the documented column order "
                                "(spread, handle, bets, total, handle, bets, money, handle, bets); parsing aborted"})
        return [], problems
    rows: list[TeamRow] = []
    for tr in soup.find_all("tr"):
        link = None
        for a in tr.find_all("a", href=True):
            m = _TEAM_HREF.search(a["href"])
            if m:
                link = (m.group(1).lower(), _cell_text(a))
                break
        if link is None:
            continue                                   # date header / control rows
        cells = [_cell_text(td) for td in tr.find_all(["td", "th"])]
        # The nine value columns are always the LAST nine cells of a team row:
        #   spread, handle%, bets%, total, handle%, bets%, moneyline, handle%, bets%
        # Reading by position (not by pattern) matters for college football, where a big favourite
        # frequently has no moneyline posted; a blank cell means unavailable, not "skip this row".
        if len(cells) < 10:
            problems.append({"kind": "row_shape", "slug": link[0], "team": link[1],
                             "why": f"row for {link[0]} had {len(cells)} cells, expected at least 10", "cells": cells[:12]})
            continue
        v = cells[-9:]
        vals = [_num(v[0]), _pct(v[1]), _pct(v[2]), _num(v[3]), _pct(v[4]), _pct(v[5]), _num(v[6]), _pct(v[7]), _pct(v[8])]
        if all(x is None for x in vals):
            problems.append({"kind": "row_empty", "slug": link[0], "team": link[1],
                             "why": f"row for {link[0]} had no readable values", "cells": cells[-9:]})
            continue
        rows.append(TeamRow(slug=link[0], name=link[1], spread=vals[0], spread_handle=vals[1], spread_bets=vals[2],
                            total=vals[3], total_handle=vals[4], total_bets=vals[5],
                            moneyline=vals[6], ml_handle=vals[7], ml_bets=vals[8]))
    if not rows:
        problems.append({"why": "no team rows found on the page"})
    return rows, problems


def slug_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# VSiN shortens school names; expand each token to its full form before matching.
_TOKEN_EXPANSIONS = {
    "st": ["state"], "e": ["eastern", "east"], "w": ["western", "west"], "n": ["northern", "north"],
    "s": ["southern", "south"], "c": ["central"], "fl": ["florida"], "la": ["louisiana"], "miss": ["mississippi"],
    "tenn": ["tennessee"], "conn": ["connecticut"], "caro": ["carolina"], "mich": ["michigan"], "ill": ["illinois"],
    "ky": ["kentucky"], "col": ["colorado"], "wash": ["washington"], "intl": ["international"], "u": ["university"],
    "ut": ["utah"], "az": ["arizona"], "ark": ["arkansas"], "ga": ["georgia"], "ala": ["alabama"],
}


# Tokens that CHANGE which school is meant. Dropping one turns Florida Atlantic into Florida and
# Miami (OH) into Miami, so a shorter match is never accepted past one of these.
_QUALIFIERS = {"st", "state", "oh", "fl", "la", "ny", "nc", "sc", "am", "atlantic", "tech", "international", "intl",
               "north", "northern", "south", "southern", "east", "eastern", "west", "western", "central",
               "n", "s", "e", "w", "c", "coastal", "gulf", "valley", "dominion", "commonwealth", "poly", "polytechnic",
               "a&m", "aandm", "christian", "wesleyan", "southeastern", "northwestern", "southwestern", "northeastern"}


def _slug_variants(slug: str) -> list[str]:
    """Candidate normalized spellings for a VSiN slug, longest first (full name, then dropping mascot words)."""
    parts = [p for p in slug.split("-") if p]
    expanded: list[list[str]] = [[]]
    for tok in parts:
        opts = [tok] + _TOKEN_EXPANSIONS.get(tok, [])
        expanded = [prev + [o] for prev in expanded for o in opts][:32]
    out = []
    for toks in expanded:
        # never cut past a qualifier: "florida atlantic owls" may shorten to "florida atlantic", never "florida"
        last_q = max((i for i, t in enumerate(toks) if t in _QUALIFIERS), default=-1)
        floor = max(last_q + 1, 1)
        for cut in range(len(toks), floor - 1, -1):
            if cut >= floor:
                out.append(slug_key("".join(toks[:cut])))
    seen, uniq = set(), []
    for k in out:
        if k and k not in seen:
            seen.add(k); uniq.append(k)
    return uniq


def seed_aliases(rows: list[TeamRow], league: str, resolver: ids.AliasResolver, teams: pd.DataFrame,
                 fix_conflicts: bool = True) -> tuple[int, list[str], list[dict]]:
    """
    Map VSiN team slugs to our team_ids by exact normalized match against display name, school+mascot and
    school alone. Ambiguous or unmatched slugs are returned for manual mapping — never fuzzy-matched.
    """
    if teams.empty:
        return 0, [r.slug for r in rows], []
    t = teams[teams.league == league]
    lookup: dict[str, set] = {}
    for _, x in t.iterrows():
        cands = {x.display_name, x.school_or_city}
        if pd.notna(x.mascot):
            cands.add(f"{x.school_or_city} {x.mascot}")
        for c in cands:
            if isinstance(c, str) and c.strip():
                lookup.setdefault(slug_key(c), set()).add(x.team_id)
    existing = dict(zip(resolver.aliases[resolver.aliases.provider == "vsin"].alias,
                        resolver.aliases[resolver.aliases.provider == "vsin"].team_id))
    known = set(existing)
    added, unmatched, conflicts = [], [], []
    for r in rows:
        if r.slug in known:
            # an alias written by an earlier matcher can be wrong and would never be revisited; re-check it
            best = None
            for key in _slug_variants(r.slug):
                cand = lookup.get(key)
                if cand and len(cand) == 1:
                    best = next(iter(cand))
                    break
            if best and best != existing[r.slug]:
                conflicts.append({"slug": r.slug, "stored": existing[r.slug], "expected": best})
            continue
        hit = None
        for key in _slug_variants(r.slug):
            cand = lookup.get(key)
            if cand and len(cand) == 1:
                hit = cand
                break
        if hit and len(hit) == 1:
            added.append({"provider": "vsin", "alias": r.slug, "provider_id": None, "team_id": next(iter(hit)), "season_from": None, "season_to": None})
            known.add(r.slug)
        else:
            unmatched.append(r.slug)
    if added:
        resolver.add(added); resolver.save()
    if conflicts and fix_conflicts:
        keep = resolver.aliases[~((resolver.aliases.provider == "vsin") & (resolver.aliases.alias.isin([c["slug"] for c in conflicts])))]
        resolver.aliases = keep
        resolver.add([{"provider": "vsin", "alias": c["slug"], "provider_id": None, "team_id": c["expected"],
                       "season_from": None, "season_to": None} for c in conflicts])
        resolver.save()
    return len(added), sorted(set(unmatched)), conflicts


def to_records(rows: list[TeamRow], league: str, games: pd.DataFrame, resolver: ids.AliasResolver,
               retrieved_at, book: str = "draftkings") -> tuple[list[dict], list[dict]]:
    """
    Pair consecutive rows into games (away first, home second) and emit records in the pipeline's shape.
    Percentages are stored from the HOME side (spread, moneyline) and the OVER side (total).
    VSiN lists the total's handle/bets for the OVER on the away row and the UNDER on the home row, so the
    over share is taken from the AWAY row, which the sum-to-100 check confirms.
    """
    out, problems = [], []
    snap = pd.Timestamp(retrieved_at)
    if snap.tzinfo is None:
        snap = snap.tz_localize("UTC")
    g = games.copy()
    g["_kick"] = pd.to_datetime(g.kickoff_utc, utc=True, errors="coerce")
    by_pair: dict = {}
    for _, row in g.iterrows():
        by_pair.setdefault(frozenset((row.away_team_id, row.home_team_id)), []).append(row)

    def resolve(slug):
        try:
            return resolver.resolve("vsin", alias=slug)
        except ids.UnmatchedAlias:
            resolver.unmatched.pop()
            return None

    matched_slugs: set = set()
    seen_slugs: set = set()
    i = 0
    while i < len(rows) - 1:
        a, b = rows[i], rows[i + 1]
        seen_slugs.update({a.slug, b.slug})
        i += 2                     # ALWAYS advance a full pair: advancing by one desynchronizes every later game
        ta, tb = resolve(a.slug), resolve(b.slug)
        if ta is None or tb is None:
            problems.append({"kind": "unmapped_slug", "why": f"unmapped VSiN slug: {a.slug if ta is None else b.slug}"})
            continue
        cands = by_pair.get(frozenset((ta, tb)), [])
        future = [c for c in cands if pd.notna(c._kick) and c._kick >= snap - pd.Timedelta(hours=6)]
        pool = sorted(future or cands, key=lambda c: (pd.Timestamp.max.tz_localize("UTC") if pd.isna(c._kick) else c._kick))
        if not pool:
            problems.append({"kind": "no_scheduled_game",
                             "why": f"no scheduled game for {ta} vs {tb} (VSiN slugs: {a.slug} / {b.slug})"})
            continue
        game = pool[0]
        home_is_b = (tb == game.home_team_id)
        home_row, away_row = (b, a) if home_is_b else (a, b)
        rec = {"game_id": game.game_id, "retrieved_at": snap.isoformat(), "book": book, "period": "FULL", "source": "vsin_dk",
               "home_team_id": game.home_team_id, "away_team_id": game.away_team_id}
        pairs = (("spread", home_row.spread_bets, away_row.spread_bets, home_row.spread_handle, away_row.spread_handle),
                 ("moneyline", home_row.ml_bets, away_row.ml_bets, home_row.ml_handle, away_row.ml_handle),
                 ("total", away_row.total_bets, home_row.total_bets, away_row.total_handle, home_row.total_handle))
        for market, t_side, t_other, m_side, m_other in pairs:
            for metric, side, other in (("ticket", t_side, t_other), ("money", m_side, m_other)):
                if side is None and other is None:
                    continue                      # neither cell published
                if side == 0 and other == 0:
                    continue                      # market not posted (common on big college favourites)
                if side is not None and other is not None:
                    if not (0.95 <= side + other <= 1.05):
                        problems.append({"kind": "sum_not_100", "why": f"{game.game_id} {market} {metric}: {side:.2f} + {other:.2f} does not sum to 1"})
                        continue
                    value = side
                else:
                    # The two sides of a market are complements, so one published cell determines both.
                    # VSiN leaves a cell blank from time to time; recovering it beats showing nothing,
                    # though it loses the sum-to-100 cross-check, which is recorded as a warning.
                    value = side if side is not None else 1 - other
                    if not (0.0 <= value <= 1.0):
                        problems.append({"kind": "pct_out_of_range", "why": f"{game.game_id} {market} {metric}: derived {value:.2f}"})
                        continue
                    problems.append({"kind": "single_sided_pct",
                                     "why": f"{game.game_id} {market} {metric}: only one side published; complement used, no cross-check"})
                rec[f"{market}_{metric}_pct_home"] = round(value, 4)
        rec["line_spread_home"] = home_row.spread
        rec["line_total"] = home_row.total
        rec["line_ml_home"] = home_row.moneyline
        rec["line_ml_away"] = away_row.moneyline
        # structural sanity: a genuine pair carries both teams' lines and most of the six percentages.
        # A mis-paired row fails these, so partial junk can never reach a real game.
        n_pct = sum(1 for k in rec if k.endswith("_pct_home"))
        if home_row.spread is None or away_row.spread is None:
            problems.append({"kind": "missing_spread", "why": f"{game.game_id}: pair is missing a spread"})
        elif home_row.spread != -away_row.spread and abs(home_row.spread + away_row.spread) > 0.01:
            problems.append({"kind": "spreads_not_opposite", "why": f"{game.game_id}: spreads {home_row.spread} / {away_row.spread} are not opposites (mis-paired)"})
        elif home_row.total != away_row.total:
            problems.append({"kind": "totals_differ", "why": f"{game.game_id}: totals {home_row.total} / {away_row.total} differ (mis-paired)"})
        elif n_pct < 4:
            problems.append({"kind": "too_few_percentages", "why": f"{game.game_id}: only {n_pct} of 6 percentages usable"})
        else:
            matched_slugs.update({a.slug, b.slug})
            out.append(rec)
    never = sorted(seen_slugs - matched_slugs)
    if never:
        mapped = []
        for sl in never:
            try:
                mapped.append(f"{sl} -> {resolver.resolve('vsin', alias=sl)}")
            except ids.UnmatchedAlias:
                resolver.unmatched.pop()
        if mapped:
            problems.append({"kind": "mapped_but_never_matched",
                             "why": "these slugs resolve to a team but never matched a scheduled game, which is what a wrong "
                                    "mapping looks like: " + "; ".join(mapped[:12])})
    return out, problems
