"""
Internal identifiers and alias resolution.

  team_id : {LEAGUE}_{ABBR}            e.g. CFB_NEB, NFL_KC
  game_id : {season}_{LEAGUE}_{W|P}{week:02d}_{AWAY}_{HOME}

Alias resolution is strict: an unknown provider string raises UnmatchedAlias. Callers collect these
into validation_log and halt the job. Nothing is fuzzy-matched at write time.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

import pandas as pd

import config
from pipeline import storage

ALIASES_PATH = config.TABLES / "ref" / "team_aliases.csv"


class UnmatchedAlias(KeyError):
    pass


def make_team_id(league: str, abbr: str) -> str:
    clean = re.sub(r"[^A-Z0-9]", "", abbr.upper())
    if not clean:
        raise ValueError(f"empty abbreviation for {league}")
    return f"{league}_{clean}"


def make_game_id(season: int, league: str, season_type: str, week: int, away_team_id: str, home_team_id: str) -> str:
    if season < config.MIN_ALLOWED_SEASON:
        raise ValueError(f"season {season} predates MIN_ALLOWED_SEASON {config.MIN_ALLOWED_SEASON}")
    if season_type not in ("REG", "POST"):
        raise ValueError(f"bad season_type {season_type}")
    prefix = "W" if season_type == "REG" else "P"
    away = away_team_id.split("_", 1)[1]
    home = home_team_id.split("_", 1)[1]
    if away == home:
        raise ValueError("away and home identical")
    return f"{season}_{league}_{prefix}{week:02d}_{away}_{home}"


def parse_game_id(game_id: str) -> dict:
    m = re.fullmatch(r"(\d{4})_(CFB|NFL)_([WP])(\d{2})_([A-Z0-9]+)_([A-Z0-9]+)", game_id)
    if not m:
        raise ValueError(f"unparseable game_id {game_id}")
    season, league, wp, week, away, home = m.groups()
    return {"season": int(season), "league": league, "season_type": "REG" if wp == "W" else "POST",
            "week": int(week), "away_team_id": f"{league}_{away}", "home_team_id": f"{league}_{home}"}


@dataclass
class AliasResolver:
    aliases: pd.DataFrame
    unmatched: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls) -> "AliasResolver":
        df = storage.read_table(ALIASES_PATH)
        if df.empty:
            df = pd.DataFrame(columns=["provider", "alias", "provider_id", "team_id", "season_from", "season_to"])
        df["provider_id"] = df["provider_id"].astype("string")
        return cls(df)

    def resolve(self, provider: str, alias: str | None = None, provider_id: str | int | None = None,
                season: int | None = None) -> str:
        d = self.aliases[self.aliases.provider == provider]
        hit = pd.DataFrame()
        if provider_id is not None and not d.empty:
            hit = d[d.provider_id == str(provider_id)]
        if hit.empty and alias is not None and not d.empty:
            hit = d[d.alias == alias]
        if season is not None and not hit.empty:
            hit = hit[(hit.season_from.isna() | (hit.season_from <= season)) &
                      (hit.season_to.isna() | (hit.season_to >= season))]
        if hit.empty:
            self.unmatched.append({"provider": provider, "alias": alias, "provider_id": provider_id, "season": season})
            raise UnmatchedAlias(f"{provider}: no alias for {alias!r} / id {provider_id!r}")
        if hit.team_id.nunique() > 1:
            raise UnmatchedAlias(f"{provider}: alias {alias!r} maps to multiple team_ids {hit.team_id.unique().tolist()}")
        return str(hit.team_id.iloc[0])

    def add(self, rows: list[dict]) -> None:
        new = pd.DataFrame(rows)
        self.aliases = pd.concat([self.aliases, new], ignore_index=True).drop_duplicates(["provider", "alias"])

    def save(self) -> None:
        ALIASES_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.aliases.sort_values(["provider", "team_id", "alias"]).to_csv(ALIASES_PATH, index=False)
