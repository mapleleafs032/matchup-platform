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


def _pct(s: str) -> float | None:
    m = _PCT.match(s)
    return float(m.group(1)) / 100.0 if m else None


def _num(s: str) -> float | None:
    s = s.replace("PK", "0").replace("pk", "0")
    return float(s) if _NUMBER.match(s) else None


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
        vals = []
        for c in cells:
            p = _pct(c)
            if p is not None:
                vals.append(("pct", p)); continue
            n = _num(c)
            if n is not None:
                vals.append(("num", n))
        # expect num, pct, pct, num, pct, pct, num, pct, pct  (leading rotation numbers are tolerated)
        pattern = [t for t, _ in vals]
        start = None
        for i in range(len(pattern) - 8):
            if pattern[i:i + 9] == ["num", "pct", "pct", "num", "pct", "pct", "num", "pct", "pct"]:
                start = i
                break
        if start is None:
            problems.append({"slug": link[0], "team": link[1], "why": "row did not contain the expected 9 value cells", "cells": cells[:12]})
            continue
        v = [x for _, x in vals[start:start + 9]]
        rows.append(TeamRow(slug=link[0], name=link[1], spread=v[0], spread_handle=v[1], spread_bets=v[2],
                            total=v[3], total_handle=v[4], total_bets=v[5], moneyline=v[6], ml_handle=v[7], ml_bets=v[8]))
    if not rows:
        problems.append({"why": "no team rows found on the page"})
    return rows, problems


def slug_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def seed_aliases(rows: list[TeamRow], league: str, resolver: ids.AliasResolver, teams: pd.DataFrame) -> tuple[int, list[str]]:
    """
    Map VSiN team slugs to our team_ids by exact normalized match against display name, school+mascot and
    school alone. Ambiguous or unmatched slugs are returned for manual mapping — never fuzzy-matched.
    """
    if teams.empty:
        return 0, [r.slug for r in rows]
    t = teams[teams.league == league]
    lookup: dict[str, set] = {}
    for _, x in t.iterrows():
        cands = {x.display_name, x.school_or_city}
        if pd.notna(x.mascot):
            cands.add(f"{x.school_or_city} {x.mascot}")
        for c in cands:
            if isinstance(c, str) and c.strip():
                lookup.setdefault(slug_key(c), set()).add(x.team_id)
    known = set(resolver.aliases[resolver.aliases.provider == "vsin"].alias)
    added, unmatched = [], []
    for r in rows:
        if r.slug in known:
            continue
        parts = r.slug.split("-")
        hit = None
        for cut in range(len(parts), 0, -1):          # full slug first, then drop mascot words one at a time
            cand = lookup.get(slug_key("".join(parts[:cut])))
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
    return len(added), sorted(set(unmatched))


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

    i = 0
    while i < len(rows) - 1:
        a, b = rows[i], rows[i + 1]
        i += 2                     # ALWAYS advance a full pair: advancing by one desynchronizes every later game
        ta, tb = resolve(a.slug), resolve(b.slug)
        if ta is None or tb is None:
            problems.append({"why": f"unmapped VSiN slug: {a.slug if ta is None else b.slug}"})
            continue
        cands = by_pair.get(frozenset((ta, tb)), [])
        future = [c for c in cands if pd.notna(c._kick) and c._kick >= snap - pd.Timedelta(hours=6)]
        pool = sorted(future or cands, key=lambda c: (pd.Timestamp.max.tz_localize("UTC") if pd.isna(c._kick) else c._kick))
        if not pool:
            problems.append({"why": f"no scheduled game for {ta} vs {tb}"})
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
                if side is None or other is None:
                    continue
                if not (0.95 <= side + other <= 1.05):
                    problems.append({"why": f"{game.game_id} {market} {metric}: {side:.2f} + {other:.2f} does not sum to 1"})
                    continue
                rec[f"{market}_{metric}_pct_home"] = round(side, 4)
        rec["line_spread_home"] = home_row.spread
        rec["line_total"] = home_row.total
        # structural sanity: a genuine pair carries both teams' lines and most of the six percentages.
        # A mis-paired row fails these, so partial junk can never reach a real game.
        n_pct = sum(1 for k in rec if k.endswith("_pct_home"))
        if home_row.spread is None or away_row.spread is None:
            problems.append({"why": f"{game.game_id}: pair is missing a spread; treated as mis-paired"})
        elif home_row.spread != -away_row.spread and abs(home_row.spread + away_row.spread) > 0.01:
            problems.append({"why": f"{game.game_id}: spreads {home_row.spread} / {away_row.spread} are not opposites; treated as mis-paired"})
        elif home_row.total != away_row.total:
            problems.append({"why": f"{game.game_id}: totals {home_row.total} / {away_row.total} differ; treated as mis-paired"})
        elif n_pct < 4:
            problems.append({"why": f"{game.game_id}: only {n_pct} of 6 percentages usable; dropped"})
        else:
            out.append(rec)
    return out, problems
