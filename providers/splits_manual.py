"""
Betting splits (ticket % and money %) providers.

There is no free, licensed, structured feed for public betting splits. Two supported inputs, both legitimate:

  MANUAL PASTE  data/manual/splits_paste/<league>_<period>_<anything>.txt
                You copy a splits table you have access to and drop it in as text. The parser below is
                deliberately tolerant: it finds team names and percentages on each line, resolves teams
                through team_aliases, and REJECTS anything it cannot read rather than guessing. Every row
                it produces is stamped source="manual_paste" and shows a "manual" badge in the UI.

  LICENSED FEED providers/splits_feed.py adapter interface (Action Network, SportsDataIO, OddsJam, ...).
                Implement fetch() against whatever you license; the rest of the pipeline is unchanged.

Nothing here scrapes a site. The parser only reads files you place in the repo.

Percentages are stored as fractions 0..1, always from the HOME side (spread, moneyline) or the OVER side
(total), so one number per market per metric. The complement is implied and never stored twice.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

import pandas as pd

from pipeline import ids

MARKETS = ("SPREAD", "TOTAL", "MONEYLINE")
PERIODS = ("FULL", "1H")

_PCT = re.compile(r"(\d{1,3}(?:\.\d)?)\s*%")
_NUMS = re.compile(r"(?<![\w.])[-+]?\d{1,3}(?:\.\d)?(?![\w%])")


@dataclass
class ParsedRow:
    team_raw: str
    team_id: str | None
    percents: list[float]
    line_no: int
    raw: str


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip()


_TS_ONLY = re.compile(r"^\s*\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?\s*(Z|[+-]\d{2}:?\d{2})?\s*$")


def parse_paste(text: str, league: str, resolver: ids.AliasResolver, providers=("vsin", "espn", "odds_api", "cfbd", "nflverse")) -> tuple[list[ParsedRow], list[dict]]:
    """
    Split a pasted table into (rows we could read, problems). A row is usable when it names exactly one
    known team and carries at least two percentages. Team resolution tries each provider alias namespace.
    """
    rows, problems = [], []
    for i, line in enumerate(text.splitlines(), start=1):
        ln = _clean(line)
        if not ln or len(ln) < 4 or _TS_ONLY.match(ln):
            continue     # blank lines and a deliberate timestamp header are not problems
        pcts = [float(p) for p in _PCT.findall(ln)]
        if len(pcts) < 2:
            bare = [float(n) for n in _NUMS.findall(ln)]
            pcts = [b for b in bare if 0 <= b <= 100]
            if len(pcts) < 2:
                continue
        # the team name is the longest run of letters at the start of the line
        m = re.match(r"^[^A-Za-z]*([A-Za-z][A-Za-z .'&-]{2,40})", ln)
        if not m:
            problems.append({"line": i, "raw": ln[:120], "why": "no team name found"}); continue
        team_raw = _clean(m.group(1))
        tid = None
        for prov in providers:
            try:
                tid = resolver.resolve(prov, alias=team_raw)
                break
            except ids.UnmatchedAlias:
                resolver.unmatched.pop()
                continue
        if tid is None or not tid.startswith(league):
            problems.append({"line": i, "raw": ln[:120], "team": team_raw, "why": "team not recognized"}); continue
        rows.append(ParsedRow(team_raw=team_raw, team_id=tid, percents=pcts, line_no=i, raw=ln[:160]))
    return rows, problems


def pair_rows(rows: list[ParsedRow], games: pd.DataFrame, period: str, book: str, retrieved_at, source: str) -> tuple[list[dict], list[dict]]:
    """
    Pair consecutive parsed rows into games (a splits table always lists the two teams of a game together)
    and emit one record per market. Division rivals meet twice a season, so a pair is resolved to the next
    SCHEDULED kickoff at or after the snapshot time -- never simply "a game these teams played".
    A pair that matches no such game is reported, not guessed.
    """
    out, problems = [], []
    snap = pd.Timestamp(retrieved_at)
    if snap.tzinfo is None:
        snap = snap.tz_localize("UTC")
    g = games.copy()
    g["_kick"] = pd.to_datetime(g.kickoff_utc, utc=True, errors="coerce")
    by_pair: dict[tuple, list] = {}
    for _, row in g.iterrows():
        by_pair.setdefault(frozenset((row.away_team_id, row.home_team_id)), []).append(row)

    def match(a_id, b_id):
        cands = by_pair.get(frozenset((a_id, b_id)), [])
        if not cands:
            return None, "no game between these teams"
        future = [c for c in cands if pd.notna(c._kick) and c._kick >= snap - pd.Timedelta(hours=6)]
        pool = future or cands
        if len(pool) > 1:
            pool = sorted(pool, key=lambda c: (pd.Timestamp.max.tz_localize("UTC") if pd.isna(c._kick) else c._kick))
        chosen = pool[0]
        if not future:
            return chosen, "no upcoming meeting; nearest past game used"
        return chosen, None

    i = 0
    while i < len(rows) - 1:
        a, b = rows[i], rows[i + 1]
        chosen, why = match(a.team_id, b.team_id)
        if chosen is None:
            problems.append({"line": a.line_no, "raw": a.raw, "why": f"{why}: {a.team_id} vs {b.team_id}"})
            i += 1
            continue
        if why:
            problems.append({"line": a.line_no, "raw": a.raw, "why": why})
        home_first = (a.team_id == chosen.home_team_id)
        home_row, away_row = (a, b) if home_first else (b, a)
        hp, ap = home_row.percents, away_row.percents
        n = min(len(hp), len(ap))
        rec = {"game_id": chosen.game_id, "retrieved_at": snap.isoformat(), "book": book, "period": period, "source": source,
               "home_team_id": chosen.home_team_id, "away_team_id": chosen.away_team_id}
        markets_found = []
        for mi, market in enumerate(MARKETS):
            j = mi * 2
            if j + 1 >= n:
                continue
            got = False
            for k, metric in ((j, "ticket"), (j + 1, "money")):
                h, aw = hp[k], ap[k]
                if h is None or aw is None:
                    continue
                if not (95 <= h + aw <= 105):
                    problems.append({"line": home_row.line_no, "raw": home_row.raw, "why": f"{market} {metric}: {h} + {aw} does not sum to 100"})
                    continue
                rec[f"{market.lower()}_{metric}_pct_home"] = round(h / 100.0, 4)
                got = True
            if got:
                markets_found.append(market)
        if any(k.endswith("_pct_home") for k in rec):
            rec["markets"] = ",".join(markets_found)
            out.append(rec)
        else:
            problems.append({"line": a.line_no, "raw": a.raw, "why": "no usable market percentages in the pair"})
        i += 2
    return out, problems


def read_csv(path, league: str) -> pd.DataFrame:
    """
    Explicit CSV input for anyone who prefers columns to pasting. Required columns:
      game_id, period, market, metric, pct_home_or_over  (plus optional book, retrieved_at, line)
    """
    df = pd.read_csv(path)
    need = {"game_id", "period", "market", "metric", "pct_home_or_over"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    df["period"] = df.period.astype(str).str.upper()
    df["market"] = df.market.astype(str).str.upper()
    df["metric"] = df.metric.astype(str).str.lower()
    return df
