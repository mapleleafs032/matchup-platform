"""
Licensed betting-splits feed adapter (interface only).

If you license a feed that permits programmatic access and redistribution (Action Network, SportsDataIO,
OddsJam and others sell one), implement fetch() here. Everything downstream — storage, validation, history,
divergence, the Odds tab — already works and needs no change.

Contract: return a list of dicts with
    game_id, retrieved_at (ISO), book, period ('FULL'|'1H'),
    spread_ticket_pct_home, spread_money_pct_home,
    total_ticket_pct_home (share on the OVER), total_money_pct_home,
    moneyline_ticket_pct_home, moneyline_money_pct_home,
    line_spread_home, line_total, source
Percentages are fractions 0..1 from the HOME (or OVER) side. Omit a market you do not receive; never
substitute a different market's number for a missing one.
"""
from __future__ import annotations

import config
from providers.base import RequestManager


class NotConfigured(RuntimeError):
    pass


def fetch(rm: RequestManager, league: str, season: int, week: int) -> list[dict]:
    if not config.SPLITS_FEED.get("enabled"):
        raise NotConfigured("No licensed splits feed configured (config.SPLITS_FEED). Manual paste input is used instead.")
    raise NotImplementedError(
        "Implement fetch() against your licensed provider and return the contract documented above. "
        "Set config.SPLITS_FEED = {'enabled': True, 'provider': '<name>', 'base_url': '...'} and add the key as a repo secret."
    )
