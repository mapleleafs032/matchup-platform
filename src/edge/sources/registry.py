"""Catalog of every data source the plan relies on, with honest status.

Only adapters marked IMPLEMENTED may be used by jobs. The others document what
is known and what must be verified before integration (spec 20: never invent
endpoints or represent a placeholder as a working source).
"""

from __future__ import annotations

from edge.sources import nflverse, odds_api
from edge.sources.base import IntegrationStatus, ProviderInfo

_UNVERIFIED = "Not yet verified; must be checked against provider docs before integration"

PROVIDERS: dict[str, ProviderInfo] = {
    "odds_api": odds_api.INFO,
    "nflverse": nflverse.INFO,
    "vsin_splits": ProviderInfo(
        name="VSiN public betting splits (DraftKings data)",
        data_types=["bets %", "handle %"],
        historical_coverage="None via API; only what we record",
        update_frequency="Page states refresh every 5 minutes",
        authentication="None for the public page",
        rate_limits="Self-imposed: checkpoint runs plus hourly in game windows",
        licensing="Terms of use and robots.txt must be reviewed before enabling",
        known_limitations=[
            "No API; requires reading the rendered page with a headless browser",
            "Covers DraftKings customers only; not market-wide",
        ],
        error_behavior="Not implemented",
        docs_url="https://data.vsin.com/betting-splits/",
        verified_on=None,
        status=IntegrationStatus.BLOCKED,
    ),
    "nws_weather": ProviderInfo(
        name="National Weather Service API",
        data_types=["forecasts"],
        historical_coverage="Forecasts only",
        update_frequency="Hourly forecasts (to verify)",
        authentication="User-Agent header (to verify)",
        rate_limits=_UNVERIFIED,
        licensing="US government data (to verify)",
        known_limitations=["US locations only", _UNVERIFIED],
        error_behavior="Not implemented",
        docs_url="https://www.weather.gov/documentation/services-web-api",
        verified_on=None,
        status=IntegrationStatus.INCOMPLETE,
    ),
}


def usable(key: str) -> bool:
    info = PROVIDERS.get(key)
    return info is not None and info.status is IntegrationStatus.IMPLEMENTED
