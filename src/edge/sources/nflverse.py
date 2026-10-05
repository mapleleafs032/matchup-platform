"""nflverse schedules adapter (games, results, historical lines).

Verified 2026-10-05 against the nflreadr schedules data dictionary and the
live file https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv:

- `spread_line` is POSITIVE when the HOME team is favored (opposite of
  sportsbook notation). Converted here: home_handicap = -spread_line.
  Empirical check on 1999-2025: corr(spread_line, result) = +0.43 and home
  favorites won 67.5%.
- `gametime` is 24-hour US Eastern time regardless of venue.
- `result` = home_score - away_score; `total` = sum of scores.
- Moneylines exist from 2006 (partial) and 2007 onward; spreads/totals from 1999.
- The source and capture time of the lines are not documented; nflfastR docs
  describe the spread as the closing line. Treated as "closing, unverified
  source" and used only as a benchmark and for backtest betting at the close.
- `home_qb_*`, `temp`, `wind` describe what actually happened at the game.
  They are NOT known pre-game and are deliberately not exported here.
- No license file in the nfldata repository (checked 2026-10-05). Personal,
  non-commercial use only; do not redistribute.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from edge.core.errors import SourceError, ValidationFailure
from edge.core.timeutil import utcnow
from edge.sources.base import FetchResult, IntegrationStatus, ProviderInfo

PROVIDER = "nflverse"
SCHEDULES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
_EASTERN = ZoneInfo("America/New_York")

# nflverse code -> our franchise key (relocations keep one rating history).
FRANCHISE: dict[str, str] = {"STL": "LAR", "LA": "LAR", "SD": "LAC", "OAK": "LV"}
CANONICAL_KEYS = frozenset(
    {
        "ARI",
        "ATL",
        "BAL",
        "BUF",
        "CAR",
        "CHI",
        "CIN",
        "CLE",
        "DAL",
        "DEN",
        "DET",
        "GB",
        "HOU",
        "IND",
        "JAX",
        "KC",
        "LV",
        "LAC",
        "LAR",
        "MIA",
        "MIN",
        "NE",
        "NO",
        "NYG",
        "NYJ",
        "PHI",
        "PIT",
        "SF",
        "SEA",
        "TB",
        "TEN",
        "WAS",
    }
)
REQUIRED_COLUMNS = (
    "game_id",
    "season",
    "game_type",
    "week",
    "gameday",
    "gametime",
    "away_team",
    "away_score",
    "home_team",
    "home_score",
    "location",
    "result",
    "total",
    "overtime",
    "away_moneyline",
    "home_moneyline",
    "spread_line",
    "away_spread_odds",
    "home_spread_odds",
    "total_line",
    "under_odds",
    "over_odds",
    "div_game",
    "roof",
)

INFO = ProviderInfo(
    name="nflverse nfldata (games.csv)",
    data_types=["schedule", "final scores", "closing spread/total/moneyline (source undocumented)"],
    historical_coverage="1999 to current season; moneylines from 2006",
    update_frequency="Updated during the season (cadence undocumented)",
    authentication="None",
    rate_limits="GitHub raw file serving; one download per run",
    licensing="No license file in repository (checked 2026-10-05); personal use only",
    known_limitations=[
        "Line source and capture time undocumented",
        "QB, temperature and wind columns are post-game facts and are excluded",
        "Kickoff times are Eastern; converted to UTC here",
    ],
    error_behavior="Network errors retried twice; missing columns, duplicate ids, "
    "unknown teams or inconsistent scores raise ValidationFailure",
    docs_url="https://nflreadr.nflverse.com/articles/dictionary_schedules.html",
    verified_on="2026-10-05",
    status=IntegrationStatus.IMPLEMENTED,
)


@dataclass(frozen=True, slots=True)
class NflGameRecord:
    game_id: str
    season: int
    game_type: str  # REG, WC, DIV, CON, SB
    week: int
    kickoff: datetime  # UTC; start of the game day (Eastern) when the time is unknown
    kickoff_time_known: bool
    result_available_at: datetime  # conservative: kickoff + 4h, or next day 06:00 ET
    home: str  # franchise key
    away: str
    neutral: bool
    home_score: int | None
    away_score: int | None
    overtime: bool | None
    home_handicap: float | None  # sportsbook convention: home -3.0 = home favored by 3
    total_line: float | None
    home_ml: int | None
    away_ml: int | None
    home_spread_odds: int | None
    away_spread_odds: int | None
    over_odds: int | None
    under_odds: int | None
    div_game: bool
    roof: str | None

    @property
    def completed(self) -> bool:
        return self.home_score is not None and self.away_score is not None

    @property
    def margin(self) -> int | None:
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score - self.away_score


@dataclass(slots=True)
class ScheduleReport:
    games: list[NflGameRecord] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def fetch_schedules(
    client: httpx.Client | None = None, *, url: str = SCHEDULES_URL, retries: int = 2
) -> FetchResult:
    own = client is None
    c = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True)
    try:
        for attempt in range(retries + 1):
            fetched_at = utcnow()
            try:
                resp = c.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == retries:
                    raise SourceError(f"nflverse download failed: {type(exc).__name__}") from exc
                continue
            if resp.status_code >= 500 and attempt < retries:
                continue
            if resp.status_code != 200:
                raise SourceError(f"nflverse download HTTP {resp.status_code}")
            return FetchResult(
                PROVIDER,
                url,
                {},
                200,
                resp.content,
                fetched_at,
                {k.lower(): v for k, v in resp.headers.items()},
            )
        raise SourceError("nflverse download failed")  # pragma: no cover
    finally:
        if own:
            c.close()


def franchise(code: str) -> str:
    key = FRANCHISE.get(code, code)
    if key not in CANONICAL_KEYS:
        raise ValidationFailure(f"unknown nflverse team code {code!r}")
    return key


def _num(row: dict[str, str], col: str) -> float | None:
    v = (row.get(col) or "").strip()
    if v in ("", "NA"):
        return None
    return float(v)


def _int(row: dict[str, str], col: str) -> int | None:
    v = _num(row, col)
    if v is None:
        return None
    if v != int(v):
        raise ValidationFailure(f"{row['game_id']}: {col} is not a whole number: {v}")
    return int(v)


def _kickoff(row: dict[str, str]) -> tuple[datetime, bool, datetime]:
    """Return (kickoff_utc, time_known, result_available_at_utc).

    Missing times are not invented. The kickoff becomes the start of the game
    day and the result becomes available only at 06:00 ET the next day, so an
    unknown time can only delay, never advance, what a prediction may see.
    """
    day = row["gameday"].strip()
    if not day:
        raise ValidationFailure(f"{row['game_id']}: missing gameday")
    time = (row.get("gametime") or "").strip()
    if time and time != "NA":
        kick = datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=_EASTERN)
        return kick.astimezone(UTC), True, (kick + timedelta(hours=4)).astimezone(UTC)
    start = datetime.fromisoformat(f"{day}T00:00").replace(tzinfo=_EASTERN)
    avail = (start + timedelta(days=1, hours=6)).astimezone(UTC)
    return start.astimezone(UTC), False, avail


def normalize_schedules(result: FetchResult) -> ScheduleReport:
    text = result.body.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValidationFailure(f"games.csv missing columns: {missing}")

    report = ScheduleReport()
    seen: set[str] = set()
    for row in reader:
        gid = row["game_id"]
        if gid in seen:
            raise ValidationFailure(f"duplicate game_id {gid}")
        seen.add(gid)
        home, away = franchise(row["home_team"]), franchise(row["away_team"])
        if home == away:
            raise ValidationFailure(f"{gid}: home and away are the same franchise")
        hs, as_ = _int(row, "home_score"), _int(row, "away_score")
        if (hs is None) != (as_ is None):
            raise ValidationFailure(f"{gid}: only one score present")
        if hs is not None and as_ is not None:
            if hs < 0 or as_ < 0:
                raise ValidationFailure(f"{gid}: negative score")
            res, tot = _int(row, "result"), _int(row, "total")
            if res is not None and res != hs - as_:
                raise ValidationFailure(f"{gid}: result {res} != {hs}-{as_}")
            if tot is not None and tot != hs + as_:
                raise ValidationFailure(f"{gid}: total {tot} != {hs}+{as_}")

        spread = _num(row, "spread_line")
        total_line = _num(row, "total_line")
        if spread is not None and abs(spread) > 30:
            report.issues.append(f"{gid}: implausible spread {spread}; line dropped")
            spread = None
        if total_line is not None and not 20 <= total_line <= 80:
            report.issues.append(f"{gid}: implausible total {total_line}; line dropped")
            total_line = None
        ot = _int(row, "overtime")
        kickoff, time_known, avail = _kickoff(row)
        report.games.append(
            NflGameRecord(
                game_id=gid,
                season=int(row["season"]),
                game_type=row["game_type"],
                week=int(row["week"]),
                kickoff=kickoff,
                kickoff_time_known=time_known,
                result_available_at=avail,
                home=home,
                away=away,
                neutral=row["location"].strip().lower() == "neutral",
                home_score=hs,
                away_score=as_,
                overtime=None if ot is None else bool(ot),
                home_handicap=None if spread is None else -spread + 0.0,
                total_line=total_line,
                home_ml=_int(row, "home_moneyline"),
                away_ml=_int(row, "away_moneyline"),
                home_spread_odds=_int(row, "home_spread_odds"),
                away_spread_odds=_int(row, "away_spread_odds"),
                over_odds=_int(row, "over_odds"),
                under_odds=_int(row, "under_odds"),
                div_game=row.get("div_game", "0").strip() == "1",
                roof=(row.get("roof") or "").strip() or None,
            )
        )
    report.games.sort(key=lambda g: (g.kickoff, g.game_id))
    return report
