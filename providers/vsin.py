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
from providers.base import RequestManager, ProviderError

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


def fetch(rm: RequestManager, league: str, params: dict | None = None) -> tuple[str, object]:
    res = rm.get(BASE, params=params or {"sport": SPORT[league], "source": "DK"},
                 headers={"User-Agent": UA, "Accept": "text/html"},
                 expect_json=False, timeout=45)
    return res.payload, res.retrieved_at


# ---- page scope -------------------------------------------------------------------------------
# The day-only page is what an anonymous fetch gets. Signed in, a week view may exist. Rather than
# assume which page carries the week, the scope that actually produced the most team rows is learned
# and remembered, so routine runs spend one request instead of surveying every time.
SCOPE_CACHE = config.TABLES / "ops" / "vsin_scope.json"
SCOPE_RESURVEY_HOURS = 20

_DATE_HEADER = re.compile(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s+[A-Z][a-z]{2}\.?\s+\d{1,2}")


def date_headers(html: str) -> list[str]:
    """Distinct game-day headers on the page. One means the page is a single day's card."""
    seen, out = set(), []
    for m in _DATE_HEADER.findall(html or ""):
        k = re.sub(r"\s+", " ", m)
        if k not in seen:
            seen.add(k); out.append(k)
    return out


def _load_scope() -> dict:
    import json
    if SCOPE_CACHE.exists():
        try:
            return json.loads(SCOPE_CACHE.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def _save_scope(cache: dict) -> None:
    import json
    SCOPE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SCOPE_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _scope_params(entry, league: str) -> tuple[str, dict]:
    _, url, params = entry
    u = url.format(path=PATH[league], sport=SPORT[league])
    ps = {k: (v.format(sport=SPORT[league], path=PATH[league]) if isinstance(v, str) else v)
          for k, v in params.items()}
    return u, ps


def fetch_best_scope(rm: RequestManager, league: str, minimum: int,
                     force_survey: bool = False) -> tuple[str, object, str, list[dict]]:
    """
    Fetch the page that carries the most games, learning which one that is.

    Returns (html, retrieved_at, label, notes). A cached winner is used directly; it is re-surveyed
    once a day, or immediately whenever it comes back with fewer rows than the slate needs -- which is
    also what an expired subscription cookie looks like, so a shrinking page always triggers a recheck
    rather than being written as if it were the whole board.
    """
    import pandas as _pd
    cache = _load_scope()
    rec = cache.get(league) or {}
    stale = True
    if rec.get("checked_at"):
        age_h = (_pd.Timestamp.now(tz="UTC") - _pd.Timestamp(rec["checked_at"])).total_seconds() / 3600
        stale = age_h >= SCOPE_RESURVEY_HOURS
    notes: list[dict] = []

    def try_entry(entry):
        url, ps = _scope_params(entry, league)
        res = rm.get(url, params=ps, headers={"User-Agent": UA, "Accept": "text/html"},
                     expect_json=False, timeout=45)
        rows, _ = parse(res.payload)
        return res.payload, res.retrieved_at, len(rows), url, ps

    # fast path: the remembered winner, used alone while it still covers the board
    if rec.get("label") and not stale and not force_survey:
        entry = next((e for e in config.VSIN_SCOPE_PAGES if e[0] == rec["label"]), None)
        if entry is not None:
            html, ts, n, url, ps = try_entry(entry)
            notes.append({"label": entry[0], "rows": n, "days": len(date_headers(html)), "cached": True})
            if n >= minimum:
                return html, ts, entry[0], notes
            notes.append({"why": f"remembered page {entry[0]!r} returned {n} rows, below {minimum}; re-surveying"})

    best = None
    for entry in config.VSIN_SCOPE_PAGES:
        try:
            html, ts, n, url, ps = try_entry(entry)
        except Exception as e:                       # one dead page must not sink the others
            notes.append({"label": entry[0], "why": f"fetch failed — {str(e)[:100]}"})
            continue
        notes.append({"label": entry[0], "rows": n, "days": len(date_headers(html)),
                      "gated": any(m in html for m in ("inline-lock", "template-shown"))})
        if best is None or n > best[2]:
            best = (html, ts, n, entry[0])
        if n >= minimum:
            break                                   # good enough; do not spend further requests
    if best is None:
        raise ProviderError("every VSiN page fetch failed; see notes")
    html, ts, n, label = best
    cache[league] = {"label": label, "rows": n, "checked_at": _pd.Timestamp.now(tz="UTC").isoformat()}
    _save_scope(cache)
    return html, ts, label, notes


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


# Other public pages VSiN links from the splits page. The splits page shows only the current day's
# card, so the full week's games never appear on it; these are the candidates that might carry more.
CANDIDATE_PAGES = {
    "splits (current)":   ("https://data.vsin.com/betting-splits/", {"sport": "{sport}", "source": "DK"}),
    "splits + view=week": ("https://data.vsin.com/betting-splits/", {"sport": "{sport}", "source": "DK", "view": "week"}),
    "games":              ("https://data.vsin.com/{path}/games/", {}),
    "linetracker":        ("https://data.vsin.com/{path}/vegas-odds-linetracker/", {}),
}
PATH = {"NFL": "nfl", "CFB": "college-football"}


def survey_pages(rm, league: str) -> str:
    """
    Try the public pages VSiN links to and report how many games each one actually exposes.

    The splits page returns only the day's card, so a Sunday slate is invisible midweek. This says
    which page, if any, carries the full week -- measured rather than assumed.
    """
    out = []
    for label, (url, params) in CANDIDATE_PAGES.items():
        u = url.format(path=PATH[league])
        ps = {k: v.format(sport=SPORT[league]) if isinstance(v, str) else v for k, v in params.items()}
        try:
            res = rm.get(u, params=ps, headers={"User-Agent": UA, "Accept": "text/html"},
                         expect_json=False, timeout=45)
        except Exception as e:
            out.append(f"{label}: fetch failed — {str(e)[:90]}")
            continue
        rows, _ = parse(res.payload)
        teams = len({r.slug for r in rows})
        out.append(f"{label}: {u} -> {len(rows)} team rows ({teams} distinct teams)")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(res.payload, "html.parser")
        heads = [_cell_text(tr) for t in soup.find_all("table")[:2] for tr in t.find_all("tr")[:1]]
        if heads:
            out.append(f"    first table header: {heads[0][:120]}")
        if "inline-lock" in res.payload or "template-shown" in res.payload:
            out.append("    NOTE: this page carries content-gate markup (inline-lock / template-shown)")
    return "\n    ".join(out)


def describe_page(html: str) -> str:
    """
    What the fetched page actually contains, table by table.

    Used when parsing returns far fewer rows than there are games: it shows whether the table is still
    there, how many rows carry a team link, and what those links look like, which is enough to repair
    the selector without guessing.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return "beautifulsoup4 is not installed"
    soup = BeautifulSoup(html, "html.parser")
    out = [f"page length {len(html)} chars; {len(soup.find_all('table'))} table(s); "
           f"{len(soup.find_all('tr'))} row(s) overall"]
    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    matching = [h for h in hrefs if _TEAM_HREF.search(h)]
    out.append(f"links: {len(hrefs)} total, {len(matching)} match the team-link pattern {_TEAM_HREF.pattern!r}")
    out.append(f"  first links on the page: {hrefs[:6]}")
    if matching:
        out.append(f"  first matching links:    {matching[:6]}")
    for i, tbl in enumerate(soup.find_all("table")[:4]):
        trs = tbl.find_all("tr")
        widths: dict = {}
        linked = 0
        for tr in trs:
            n = len(tr.find_all(["td", "th"]))
            widths[n] = widths.get(n, 0) + 1
            if any(_TEAM_HREF.search(a.get("href", "")) for a in tr.find_all("a", href=True)):
                linked += 1
        out.append(f"  table {i}: {len(trs)} rows, {linked} with a team link, cell counts {dict(sorted(widths.items()))}")
        for tr in trs[:3]:
            cells = [_cell_text(c) for c in tr.find_all(["td", "th"])]
            out.append(f"    sample row ({len(cells)} cells): {cells[:12]}")
    if "<script" in html and len(soup.find_all("tr")) < 10:
        out.append("  NOTE: very few rows and scripts present — the table may now be built in the browser, "
                   "which this fetch cannot run.")
    return "\n    ".join(out)


# ---- linetracker: multi-book pricing ----------------------------------------------------------
# The anonymous linetracker header read "Mon, Oct 5 | Circa | Boomers | BetMGM | Caesars" with zero
# team rows -- the books were named but the prices were behind the gate. Signed in, this is the first
# multi-book pricing the platform has had, which is what line shopping and a real closing number need.
#
# NOTHING here guesses the cell format. parse_linetracker() reads the books out of the header row and
# refuses when it cannot, and describe_linetracker() dumps the raw cells so the value parser can be
# written from a signed-in sample instead of from imagination.
_HALF = {"½": ".5", "¼": ".25", "¾": ".75"}
_KNOWN_BOOKS = {"circa": "circa", "boomers": "boomers", "betmgm": "betmgm", "mgm": "betmgm",
                "caesars": "caesars", "draftkings": "draftkings", "dk": "draftkings",
                "fanduel": "fanduel", "westgate": "westgate", "southpoint": "southpoint",
                "south point": "southpoint", "wynn": "wynn", "stations": "stations",
                "station": "stations", "bet365": "bet365", "espnbet": "espnbet"}
_PRICE = re.compile(r"([-+]\d{3,4})")
_SIGNED = re.compile(r"([-+]\d{1,2}(?:\.\d+)?)(?![\d])")
_TOTAL_IN_CELL = re.compile(r"(?:^|[^\d.])(\d{2,3}(?:\.\d+)?)(?![\d])")


def _cell_text_str(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip()


def _defrac(s: str) -> str:
    out = str(s)
    for ch, rep in _HALF.items():
        out = out.replace(ch, rep)
    return out


def _books_from_header(cells: list[str]) -> dict[int, str]:
    """{column index: canonical book name} for the header cells that name a sportsbook."""
    found = {}
    for i, c in enumerate(cells):
        key = re.sub(r"[^a-z ]", "", c.lower()).strip()
        if key in _KNOWN_BOOKS:
            found[i] = _KNOWN_BOOKS[key]
            continue
        for name, canon in _KNOWN_BOOKS.items():
            if name and name in key and len(name) > 3:
                found[i] = canon
                break
    return found


def describe_linetracker(html: str) -> str:
    """
    Raw structure of the linetracker, printed in FULL -- no cell truncation.

    An earlier version printed only the first twelve cells of each row, which hid the fact that a data
    row carries all eight books in one 25-cell row. Truncating a diagnostic is how a layout gets
    guessed, so this prints every cell and then prints the layout the parser derived from the page's
    own header, which is the thing to check.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return "beautifulsoup4 is not installed"
    soup = BeautifulSoup(html, "html.parser")
    out = [f"page length {len(html)} chars; {len(soup.find_all('table'))} table(s); "
           f"{len(soup.find_all('tr'))} row(s) overall",
           f"date headers on the page: {date_headers(html) or 'none found'}"]
    for ti, tbl in enumerate(soup.find_all("table")[:2]):
        trs = tbl.find_all("tr")
        out.append(f"  table {ti}: {len(trs)} rows")
        for ri, tr in enumerate(trs[:8]):
            cells = [_cell_text(c) for c in tr.find_all(["td", "th"])]
            out.append(f"    row {ri} ({len(cells)} cells): {cells}")
        cols, probs = _linetracker_layout(trs)
        out.append(f"    DERIVED LAYOUT: {len(cols)} market columns")
        out.append(f"      {[f'{i+1}:{b}/{m}' for i, (b, m) in enumerate(cols)]}")
        for p in probs:
            out.append(f"      layout problem: {p.get('why')}")
    rows, probs = parse_linetracker(html)
    out.append(f"  parse: {len(rows)} priced rows, {len(probs)} problems")
    for r in rows[:6]:
        out.append(f"    {r}")
    for p in probs[:6]:
        out.append(f"    problem: {p.get('kind', 'other')}: {p.get('why', '')[:160]}")
    return "\n    ".join(out)


# ---- cell parsers, one per market -------------------------------------------------------------
# Market-aware on purpose. A single guess-the-market parser read "53" as a spread as readily as a
# total; now the header says which column is which, so each cell is parsed as the one thing it is.
# Samples observed on a signed-in page, 2026-10-12:
#   SPR  '+2 -110'   '+3.5 -115'   '-3.5 -120'
#   ML   '+120'      '+155'        '-180'
#   TOT  '53'        '54.5 o -110'
_AMERICAN = re.compile(r"([-+]\d{2,4})\s*$")
_LEAD_NUM = re.compile(r"^\s*([-+]?\d{1,3}(?:\.\d+)?)")
_PK = ("pk", "pick", "ev", "even")
# VSiN marks a number that has just MOVED with an up/down arrow glyph, e.g. '+145 \u25bc'. The price
# regex is end-anchored, so the arrow silently blocked it -- and the numbers carrying an arrow are the
# ones that just moved, i.e. the most informative prices on the page. The glyph is stripped before
# parsing and kept as a direction, which is free market-movement signal per book.
_ARROWS = {"\u25b2": "up", "\u25bc": "down", "\u25b4": "up", "\u25be": "down",
           "\u2191": "up", "\u2193": "down", "\u25b3": "up", "\u25bd": "down"}


def _strip_arrows(t: str) -> tuple[str, str | None]:
    """('+145 \u25bc') -> ('+145', 'down'). Direction is kept, never discarded with the glyph."""
    moved = None
    for glyph, direction in _ARROWS.items():
        if glyph in t:
            moved = direction
            t = t.replace(glyph, " ")
    return re.sub(r"\s+", " ", t).strip(), moved


def _spr_cell(text: str) -> dict:
    """'+3.5 -115' -> {'spread': 3.5, 'spread_price': -115}. The number first, the price last."""
    t, moved = _strip_arrows(_defrac(_cell_text_str(text)))
    if not t or t.lower() in _BLANK:
        return {}
    out: dict = {}
    if moved:
        out["spread_moved"] = moved
    m = _AMERICAN.search(t)
    head = t
    if m:
        out["spread_price"] = int(m.group(1))
        head = t[:m.start()].strip()
    if head.lower() in _PK:
        out["spread"] = 0.0
        return out
    n = _LEAD_NUM.match(head)
    if n:
        v = float(n.group(1))
        if -60.0 <= v <= 60.0:
            out["spread"] = v
    return out if "spread" in out else {}


def _ml_cell(text: str) -> dict:
    """'+155' -> {'ml_price': 155}. A moneyline cell is a price and nothing else."""
    t, moved = _strip_arrows(_defrac(_cell_text_str(text)))
    if not t or t.lower() in _BLANK:
        return {}
    m = _AMERICAN.search(t)
    if not m:
        return {}
    v = int(m.group(1))
    # a real moneyline is never inside -100..100; that range is a spread's vig, not a price
    if abs(v) < 100:
        return {}
    out = {"ml_price": v}
    if moved:
        out["ml_moved"] = moved
    return out


def _tot_cell(text: str) -> dict:
    """'54.5 o -110' -> {'total': 54.5, 'over_price': -110}; '53' -> {'total': 53.0}."""
    t, moved = _strip_arrows(_defrac(_cell_text_str(text)))
    if not t or t.lower() in _BLANK:
        return {}
    out: dict = {}
    if moved:
        out["total_moved"] = moved
    m = _AMERICAN.search(t)
    head = t
    if m:
        head = t[:m.start()].strip()
    n = _LEAD_NUM.match(head)
    if not n:
        return {}
    v = float(n.group(1))
    if not (20.0 <= v <= 120.0):
        return {}
    out["total"] = v
    if m:
        side = head.rstrip().lower()[-1:]
        key = "under_price" if side == "u" else "over_price"
        out[key] = int(m.group(1))
    return out


_CELL_PARSER = {"spread": _spr_cell, "moneyline": _ml_cell, "total": _tot_cell}
_MARKET_LABEL = {"spr": "spread", "sprd": "spread", "spread": "spread",
                 "ml": "moneyline", "money": "moneyline",
                 "tot": "total", "total": "total", "ou": "total", "o/u": "total"}


def _linetracker_layout(trs) -> tuple[list[tuple[str, str]], list[dict]]:
    """
    Read the column layout off the page's own two-tier header instead of assuming it.

    Shape confirmed on a signed-in page, 2026-10-12: the eight books are split across TWO header rows
    (Circa/Boomers/BetMGM/Caesars, then Westgate/Stations/South Point/Wynn), each followed by a
    SPR / ML / TOT sub-header, and a data row carries every book flat -- one label cell plus 24
    columns, 25 in all.

    The layout BLOCK REPEATS for every date on the page. An earlier version accumulated columns from
    all of them and derived 168 columns for a 25-cell row, so nothing parsed at all. Collection
    therefore stops at the end of the first block, detected by a book appearing a second time.

    Returns (columns, problems) where columns[i] is the (book, market) of data cell i+1.
    """
    cols: list[tuple[str, str]] = []
    problems: list[dict] = []
    rows = [[_cell_text(c) for c in tr.find_all(["td", "th"])] for tr in trs]
    seen_books: set[str] = set()
    i = 0
    while i < len(rows) - 1:
        books = _books_from_header(rows[i])
        if len(books) < 2:
            if cols:
                break                     # the first block has ended; the rest of the page repeats it
            i += 1
            continue
        names = [books[k] for k in sorted(books)]
        if seen_books & set(names):
            break                         # a book listed twice means the next date block has started
        labels = [_MARKET_LABEL.get(re.sub(r"[^a-z/]", "", c.lower())) for c in rows[i + 1][1:]]
        labels = [x for x in labels if x]
        if not labels:
            problems.append({"why": f"book header {names} was not followed by a market sub-header"})
            i += 1
            continue
        if len(labels) % len(names):
            problems.append({"why": f"{len(labels)} market labels do not divide evenly among "
                                    f"{len(names)} books {names}"})
            i += 2
            continue
        per = len(labels) // len(names)
        for bi, book in enumerate(names):
            for market in labels[bi * per:(bi + 1) * per]:
                cols.append((book, market))
        seen_books.update(names)
        i += 2
    if not cols:
        problems.append({"why": "no book header followed by a market sub-header was found; the "
                                "linetracker layout has changed"})
    return cols, problems


_OPEN_ROW = re.compile(r"\bOPEN\b", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _date_gap_days(a: str, b: str) -> int | None:
    """Whole days between two 'Mon, Oct 12' headers. None when either cannot be read."""
    import datetime as _dt

    def parse(x):
        m = re.search(r"([A-Z][a-z]{2})\.?\s+(\d{1,2})", str(x))
        if not m or m.group(1) not in _MONTHS:
            return None
        return _dt.date(2000, _MONTHS[m.group(1)], int(m.group(2)))

    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        return None
    return abs((pb - pa).days)


def parse_linetracker(html: str) -> tuple[list[dict], list[dict]]:
    """
    Multi-book prices from the linetracker, one record per (row, book).

    Each record: {label, is_open, book, market, row_index} plus the values for that market --
    spread + spread_price, ml_price, or total + over_price/under_price. The keys are namespaced by
    market on purpose: a shared "price" key let a moneyline overwrite the spread's vig for the same
    book, which silently destroyed half the pricing.
    Team identity is NOT resolved here -- the linetracker shows plain display names with no team
    link, so naming is the caller's job against the teams table, and a name this function cannot see
    is reported rather than invented.

    Strictness: a data row must carry exactly 1 + len(columns) cells. If it does not, the row is
    skipped with a reason. A shifted row would misprice every book on it, so refusing is the only
    safe behaviour, and the failure mode is missing data rather than wrong data.

    NOTE on the paywall markers: `inline-lock` / `template-shown` are present on this page even when
    signed in with a full week of prices visible, so they do NOT indicate a gate. An earlier version
    treated them as one and threw away a perfectly good page. Whether content arrived is judged by
    the content.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return [], [{"why": "beautifulsoup4 is not installed"}]
    soup = BeautifulSoup(html, "html.parser")
    problems: list[dict] = []
    out: list[dict] = []
    empty: list[dict] = []
    for tbl in soup.find_all("table"):
        trs = tbl.find_all("tr")
        cols, lprobs = _linetracker_layout(trs)
        problems += lprobs
        if not cols:
            continue
        want = len(cols) + 1
        current_date = None
        for ri, tr in enumerate(trs):
            cells = [_cell_text(c) for c in tr.find_all(["td", "th"])]
            if len(cells) != want:
                # The book-header rows carry the game date in their first cell ("Mon, Oct 12"), which
                # is the only thing that tells two identical-looking games apart. The page spans
                # several days and can cross two NFL weeks, so a team legitimately appears twice --
                # what is NOT legitimate is the same pair twice on the same date.
                if cells and _DATE_HEADER.search(cells[0]):
                    current_date = re.sub(r"\s+", " ", _DATE_HEADER.search(cells[0]).group(0))
                continue                      # header, sub-header and spacer rows all land here
            label = cells[0]
            is_open = bool(_OPEN_ROW.search(label))
            got = 0
            for ci, (book, market) in enumerate(cols, start=1):
                v = _CELL_PARSER[market](cells[ci])
                if not v:
                    continue
                got += 1
                out.append({"label": label, "is_open": is_open, "book": book, "date": current_date,
                            "market": market, "row_index": ri, **v})
            if got == 0:
                # Recorded with the label so the caller can tell the two very different causes apart:
                # a game VSiN has not priced yet (normal, nothing to collect), versus a duplicate of a
                # row that DID price elsewhere on the page (also harmless), versus a genuine parse
                # failure on a priced row (not harmless at all).
                empty.append({"row_index": ri, "label": label})
    if empty:
        priced = {r["label"] for r in out}
        dupes = [e for e in empty if e["label"] in priced]
        fresh = [e for e in empty if e["label"] not in priced]
        if dupes:
            problems.append({"kind": "unpriced_duplicate",
                             "why": f"{len(dupes)} row(s) held no number but the same label is priced "
                                    f"elsewhere on the page, so nothing is lost: "
                                    + ", ".join(sorted({e['label'][:24] for e in dupes})[:6])})
        if fresh:
            problems.append({"kind": "unpriced_row",
                             "why": f"{len(fresh)} row(s) held no number and appear NOWHERE priced -- "
                                    f"either VSiN has not posted these games yet or they are being "
                                    f"missed: " + ", ".join(f"{e['label'][:24]}@{e['row_index']}"
                                                            for e in fresh[:8])})
    if not out:
        problems.append({"kind": "no_prices",
                         "why": "no priced row matched the derived column count; run "
                                "--explain linetracker and compare DERIVED LAYOUT with the raw rows"})
    return out, problems


def linetracker_games(rows: list[dict]) -> list[dict]:
    """
    Group the flat records into games: an OPEN row starts a game, the two rows after it are the
    teams, away first (the same order the splits page uses).

    Returns [{"open": {book: {...}}, "teams": [(label, {book: {...}}), ...]}]. A group that does not
    have exactly two team rows is returned anyway, with "n_teams" set, so the caller can refuse it
    rather than this function silently dropping half a game.
    """
    by_row: dict[int, dict] = {}
    order: list[int] = []
    for r in rows:
        if r["row_index"] not in by_row:
            by_row[r["row_index"]] = {"label": r["label"], "is_open": r["is_open"],
                                      "date": r.get("date"), "books": {}}
            order.append(r["row_index"])
        slot = by_row[r["row_index"]]["books"].setdefault(r["book"], {})
        for k, v in r.items():
            if k not in ("label", "is_open", "book", "row_index", "market", "date"):
                slot[k] = v
    games: list[dict] = []
    cur: dict | None = None
    for ri in sorted(order):
        row = by_row[ri]
        if row["is_open"]:
            if cur is not None:
                games.append(cur)
            cur = {"open": row["books"], "open_label": row["label"],
                   "date": row.get("date"), "teams": []}
            continue
        if cur is None:
            continue                          # prices before any game header; nothing to attach to
        cur["teams"].append((row["label"], row["books"]))
    if cur is not None:
        games.append(cur)
    for g in games:
        g["n_teams"] = len(g["teams"])
    return games


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


def linetracker_check(games: list[dict]) -> list[dict]:
    """
    Structural cross-checks on each grouped game, per book.

    The two sides of a game must carry OPPOSITE spreads and the SAME total at any one book. That is
    true of the real page (Buffalo +3.5 / Rams -3.5, both 54.5 at Circa) and it is exactly what stops
    being true if a column shifts by one. Relying on reading a diagnostic to notice that is how the
    layout got mis-derived twice, so the check runs on every parse instead.

    Also checks that no team appears on more than one game. The page groups by an OPEN row, and a
    team listed twice means either the slate is being double-counted (which would write two
    snapshots per game and double every market signal) or the OPEN-row grouping is splitting one
    game in two. Either way it must be known BEFORE anything is written.

    Returns problems; an empty list means every game was internally consistent.
    """
    problems: list[dict] = []
    # A team appearing twice is only wrong WITHIN A DATE. The page spans several days and can cross
    # two NFL weeks, so the Bills playing on Oct 12 and again on Oct 19 is correct, not a fault --
    # an earlier version flagged 23 such teams and would have had us chasing a non-problem.
    seen_side: dict[tuple, int] = {}
    for gi, g in enumerate(games):
        for label, _ in g.get("teams", []):
            key = (g.get("date"), label)
            if key in seen_side:
                problems.append({"kind": "duplicate_side", "game": gi, "book": "",
                                 "why": f"{label!r} appears twice on {g.get('date')}: "
                                        f"game {seen_side[key]} and game {gi}"})
            else:
                seen_side[key] = gi
    # A team on two dates fewer than four days apart is a scheduling impossibility in either league,
    # so it is proof that a date was mis-attributed -- which matters because the date is what dedupe
    # uses for identity. current_date carries forward from the previous block, so an unrecognised date
    # header would silently stamp a game with the wrong day and hide a duplicate.
    by_team: dict = {}
    for gi, g in enumerate(games):
        for label, _ in g.get("teams", []):
            by_team.setdefault(label, []).append((g.get("date"), gi))
    for label, seen in by_team.items():
        days = [(d, gi) for d, gi in seen if d]
        for i in range(len(days)):
            for j in range(i + 1, len(days)):
                gap = _date_gap_days(days[i][0], days[j][0])
                if gap is not None and gap < 4:
                    problems.append({"kind": "impossible_gap", "game": days[j][1], "book": "",
                                     "why": f"{label!r} appears on {days[i][0]} and {days[j][0]} "
                                            f"({gap}d apart) -- one of those dates is wrong"})
    for gi, g in enumerate(games):
        if g.get("n_teams") != 2:
            problems.append({"kind": "not_two_teams", "game": gi,
                             "why": f"{g.get('open_label', '?')[:40]!r} grouped {g.get('n_teams')} "
                                    f"team rows, expected 2"})
            continue
        (la, ba), (lb, bb) = g["teams"]
        for book in sorted(set(ba) | set(bb)):
            a, b = ba.get(book) or {}, bb.get(book) or {}
            sa, sb = a.get("spread"), b.get("spread")
            if sa is not None and sb is not None and abs(sa + sb) > 0.01:
                problems.append({"kind": "spreads_not_opposite", "game": gi, "book": book,
                                 "why": f"{book}: {la} {sa:+g} / {lb} {sb:+g} are not opposites"})
            ta, tb = a.get("total"), b.get("total")
            if ta is not None and tb is not None and abs(ta - tb) > 0.01:
                problems.append({"kind": "totals_differ", "game": gi, "book": book,
                                 "why": f"{book}: {la} total {ta} / {lb} total {tb} differ"})
    return problems


def linetracker_best(games: list[dict]) -> list[dict]:
    """
    The best number available on each side of each game, across every book -- line shopping.

    For a side, "best" is the largest spread (most points taken / fewest laid) and, at equal spreads,
    the better price. Totals are left per-book because better depends on which way you are betting.
    """
    out = []
    for g in games:
        for label, books in g.get("teams", []):
            best_book, best = None, None
            for book, v in books.items():
                if v.get("spread") is None:
                    continue
                key = (v["spread"], v.get("spread_price") or -10000)
                if best is None or key > best:
                    best, best_book = key, book
            if best_book is None:
                continue
            ml_book, ml = None, None
            for book, v in books.items():
                if v.get("ml_price") is None:
                    continue
                if ml is None or v["ml_price"] > ml:
                    ml, ml_book = v["ml_price"], book
            out.append({"label": label, "spread": best[0], "spread_price": books[best_book].get("spread_price"),
                        "spread_book": best_book, "ml_price": ml, "ml_book": ml_book,
                        "books_quoting": len([1 for v in books.values() if v.get("spread") is not None])})
    return out


def linetracker_dedupe(games: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Collapse the same matchup listed twice on the same date.

    The signed-in page renders the board more than once: measured 2026-10-12, 27 groups covered only
    28 distinct teams, with games 0/15, 1/16, 2/17 and 3/18 sharing BOTH sides at a constant offset.
    Writing all 27 would have stored two snapshots per game and doubled every market signal built on
    top of them.

    Identity is (date, the pair of team labels) -- not the team alone, because the page spans several
    days and can cross two NFL weeks, so one team legitimately appears on two different games.

    The copy kept is the one quoting the most books; ties keep the first, which is the top of the
    page. Returns (deduplicated games, notes describing what was dropped).
    """
    def quoted(g):
        return sum(len(b) for _, b in g.get("teams", [])) + len(g.get("open", {}))

    best: dict[tuple, dict] = {}
    order: list[tuple] = []
    notes: list[dict] = []
    for gi, g in enumerate(games):
        labels = tuple(sorted(lbl for lbl, _ in g.get("teams", [])))
        if len(labels) != 2:
            key = ("__unpaired__", gi)          # never merge a group we could not pair
        else:
            key = (g.get("date"), labels)
        g = {**g, "_index": gi}
        if key not in best:
            best[key] = g
            order.append(key)
            continue
        keep, drop = (g, best[key]) if quoted(g) > quoted(best[key]) else (best[key], g)
        best[key] = keep
        notes.append({"kind": "collapsed_duplicate",
                      "why": f"{' vs '.join(labels)} on {g.get('date')} was listed twice "
                             f"(groups {best[key]['_index']} and {drop['_index']}); kept the copy "
                             f"quoting {quoted(keep)} book-markets, dropped {quoted(drop)}"})
    return [best[k] for k in order], notes
