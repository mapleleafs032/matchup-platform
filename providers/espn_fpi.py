"""
ESPN Football Power Index — strength of schedule.

Source: the same data behind
    https://www.espn.com/college-football/fpi/_/view/resume/sort/resume.avgsosrank/dir/asc

That page is rendered in the browser, so the HTML holds no numbers. Two ESPN endpoints serve the
underlying JSON and both are tried, newest-style first:

  1. site.web.api.espn.com .../fitt/v3/... /powerindex     one call, returns the page's table as shown
  2. sports.core.api.espn.com .../seasons/{year}/powerindex  the documented core endpoint

Unofficial endpoints, so this is written to survive shape changes: every field is looked up by name
across a few plausible spellings, and anything unrecognised is reported rather than guessed. Run
`--explain` on the splits/context job to dump the raw response when a field moves.

Rank semantics: a LOWER strength-of-schedule rank means a tougher schedule faced.
"""
from __future__ import annotations
import json
import re

import pandas as pd

from pipeline import ids
from providers.base import RequestManager, ProviderError

FITT = "https://site.web.api.espn.com/apis/fitt/v3/sports/football/college-football/powerindex"
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football/seasons/{season}/powerindex"
UA = "matchup-platform/1.0 (personal football research project)"

# field names that have been used for the resume strength-of-schedule rank
SOS_KEYS = ("avgsosrank", "avgSOSRank", "sosrank", "sosRank", "strengthofschedule", "strengthOfSchedule",
            "scheduleStrength", "sos")
REM_SOS_KEYS = ("remainingsosrank", "remainingSOSRank", "remainingstrengthofschedule", "remainingStrengthOfSchedule")
SOR_KEYS = ("strengthofrecord", "strengthOfRecord", "sorrank", "sorRank")


def _norm(k: str) -> str:
    return re.sub(r"[^a-z]", "", str(k).lower())


_SOS_NORM = {_norm(k) for k in SOS_KEYS}
_REM_NORM = {_norm(k) for k in REM_SOS_KEYS}
_SOR_NORM = {_norm(k) for k in SOR_KEYS}


def fetch(rm: RequestManager, season: int) -> tuple[object, str]:
    """Returns (payload, which endpoint answered). Raises only when both fail."""
    last = None
    try:
        res = rm.get(FITT, params={"region": "us", "lang": "en", "contentorigin": "espn",
                                   "limit": 400, "season": season, "sort": "resume.avgsosrank:asc"},
                     headers={"User-Agent": UA, "Accept": "application/json"}, timeout=45)
        return res.payload, "fitt"
    except ProviderError as e:
        last = f"fitt: {str(e)[:140]}"
    try:
        res = rm.get(CORE.format(season=season), params={"limit": 400},
                     headers={"User-Agent": UA, "Accept": "application/json"}, timeout=45)
        return res.payload, "core"
    except ProviderError as e:
        raise ProviderError(f"ESPN FPI unavailable. {last} | core: {str(e)[:140]}")


def _walk_numbers(obj, out: dict, depth: int = 0):
    """Collect {normalized_name: numeric_value} from any nesting ESPN happens to use."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("shortDisplayName") or obj.get("abbreviation")
        for vkey in ("rank", "value", "displayValue"):
            if name and vkey in obj:
                try:
                    out.setdefault(_norm(name), float(str(obj[vkey]).replace(",", "")))
                except (TypeError, ValueError):
                    pass
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.setdefault(_norm(k), float(v))
            else:
                _walk_numbers(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk_numbers(v, out, depth + 1)


def _team_name(entry: dict) -> str | None:
    for path in (("team", "displayName"), ("team", "name"), ("team", "location"), ("displayName",), ("name",)):
        cur = entry
        for p in path:
            cur = cur.get(p) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    return None


def _entries(payload) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("teams", "items", "entries", "powerIndexes", "ratings"):
        v = payload.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    for v in payload.values():          # one level deeper, e.g. {"powerindex": {"teams": [...]}}
        if isinstance(v, dict):
            found = _entries(v)
            if found:
                return found
    return []


def normalize(payload, season: int, resolver: ids.AliasResolver, ts, unmatched: set[str]) -> tuple[pd.DataFrame, list[str]]:
    """Returns (rows, notes). Notes describe anything that could not be read, for the job log."""
    entries = _entries(payload)
    notes: list[str] = []
    if not entries:
        top = list(payload.keys())[:12] if isinstance(payload, dict) else type(payload).__name__
        return pd.DataFrame(), [f"no team entries found; top-level keys were {top}"]
    rows, no_sos = [], 0
    for e in entries:
        name = _team_name(e)
        if not name:
            continue
        try:
            tid = resolver.resolve("espn", alias=name)
        except ids.UnmatchedAlias:
            resolver.unmatched.pop(); unmatched.add(name); continue
        nums: dict = {}
        _walk_numbers(e, nums)
        def pick(keys):
            for k in keys:
                if k in nums:
                    return nums[k]
            return None
        sos = pick(_SOS_NORM)
        if sos is None:
            no_sos += 1
        rows.append({"team_id": tid, "season": season, "espn_team": name,
                     "sos_rank_espn": None if sos is None else int(sos),
                     "remaining_sos_rank_espn": (lambda v: None if v is None else int(v))(pick(_REM_NORM)),
                     "strength_of_record_rank": (lambda v: None if v is None else int(v))(pick(_SOR_NORM)),
                     "fpi": nums.get("fpi"), "source": "espn_fpi", "retrieved_at": ts.isoformat()})
    if rows and no_sos == len(rows):
        sample = sorted(set(list(nums.keys())))[:25] if nums else []
        notes.append(f"no strength-of-schedule field recognised on any of {len(rows)} teams; "
                     f"fields seen on the last team were {sample}")
    elif no_sos:
        notes.append(f"{no_sos} of {len(rows)} teams had no strength-of-schedule value")
    return pd.DataFrame(rows), notes


def describe(payload) -> str:
    """Compact dump of the response shape, for when a field has moved."""
    entries = _entries(payload)
    out = [f"entries found: {len(entries)}"]
    if entries:
        e = entries[0]
        out.append(f"first entry keys: {sorted(e.keys())[:20]}")
        nums: dict = {}
        _walk_numbers(e, nums)
        out.append(f"numeric fields on first entry: {sorted(nums.keys())[:40]}")
        out.append(f"team name resolved to: {_team_name(e)!r}")
    elif isinstance(payload, dict):
        out.append(f"top-level keys: {sorted(payload.keys())[:20]}")
    return "\n    ".join(out)
