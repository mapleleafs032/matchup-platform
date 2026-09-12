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


def fetch(rm: RequestManager, season: int) -> tuple[object, str, list[str]]:
    """Returns (payload, which endpoint answered, notes). Raises only when every endpoint fails."""
    notes = []
    # 1) the endpoint behind the web page: returns LABELLED columns, which is what we want
    for params in (
        {"region": "us", "lang": "en", "contentorigin": "espn", "limit": 400, "page": 1,
         "sort": "resume.avgsosrank:asc", "season": season},
        {"region": "us", "lang": "en", "contentorigin": "espn", "limit": 400, "season": season},
    ):
        try:
            res = rm.get(FITT, params=params, headers={"User-Agent": UA, "Accept": "application/json",
                                                       "Referer": "https://www.espn.com/college-football/fpi/"},
                         timeout=45)
            return res.payload, "fitt", notes
        except ProviderError as e:
            notes.append(f"fitt ({'sorted' if 'sort' in params else 'plain'}): {str(e)[:150]}")
    # 2) the documented core endpoint: UNLABELLED parallel arrays, usable only once the column is pinned
    try:
        res = rm.get(CORE.format(season=season), params={"limit": 400},
                     headers={"User-Agent": UA, "Accept": "application/json"}, timeout=45)
        return res.payload, "core", notes
    except ProviderError as e:
        notes.append(f"core: {str(e)[:150]}")
        raise ProviderError("ESPN FPI unavailable. " + " | ".join(notes))


def _collect(obj, out: dict, depth: int = 0):
    """
    Collect {normalized_name::kind: value} where kind is "rank" or "value".

    ESPN stores power-index numbers as PARALLEL ARRAYS inside each category:
        {"name": "resume", "names": ["sor","fpi","avgwp","sos", ...],
         "values": [...], "ranks": [...]}
    so the numbers carry no inline labels. Named-object and flat-scalar forms are handled too, since
    these endpoints are unofficial and have used all three.
    """
    if depth > 7:
        return
    if isinstance(obj, dict):
        names = obj.get("names") or obj.get("labels") or obj.get("displayNames")
        if isinstance(names, list) and names:
            for arr_key, kind in (("ranks", "rank"), ("values", "value"), ("totals", "value")):
                arr = obj.get(arr_key)
                if isinstance(arr, list) and len(arr) == len(names):
                    for n, v in zip(names, arr):
                        try:
                            out.setdefault(f"{_norm(n)}::{kind}", float(str(v).replace(",", "")))
                        except (TypeError, ValueError):
                            pass
        nm = obj.get("name") or obj.get("shortDisplayName") or obj.get("abbreviation")
        if nm:
            for vkey, kind in (("rank", "rank"), ("value", "value"), ("displayValue", "value")):
                if vkey in obj:
                    try:
                        out.setdefault(f"{_norm(nm)}::{kind}", float(str(obj[vkey]).replace(",", "")))
                    except (TypeError, ValueError):
                        pass
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.setdefault(f"{_norm(k)}::value", float(v))
                out.setdefault(f"{_norm(k)}::rank", float(v))
            else:
                _collect(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, out, depth + 1)


def _pick(nums: dict, keys: set[str], prefer: str = "rank") -> float | None:
    """A rank if one exists for any spelling of the field, otherwise the raw value."""
    for kind in (prefer, "value" if prefer == "rank" else "rank"):
        for k in keys:
            v = nums.get(f"{k}::{kind}")
            if v is not None:
                return v
    return None


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


def _resume_by_index(entry: dict, idx: int | None) -> float | None:
    """
    Read the resume category by position. The core endpoint returns six bare numbers with no labels,
    so the position of strength of schedule must be PINNED (config.ESPN_SOS_RESUME_INDEX) after being
    checked against a team whose rank is known. Unpinned, this returns nothing rather than a guess.
    """
    if idx is None:
        return None
    for c in (entry.get("categories") or []):
        if not isinstance(c, dict) or _norm(c.get("name") or "") != "resume":
            continue
        vals = c.get("values")
        if isinstance(vals, list) and 0 <= idx < len(vals):
            try:
                return float(vals[idx])
            except (TypeError, ValueError):
                return None
    return None


def resume_values(payload) -> list[tuple[str, list]]:
    """(team name, resume values) for every team — used to pin the column against a known rank."""
    out = []
    for e in _entries(payload):
        nm = _team_name(e)
        for c in (e.get("categories") or []):
            if isinstance(c, dict) and _norm(c.get("name") or "") == "resume":
                out.append((nm, c.get("values") or []))
                break
    return out


def verify_sos_column(payload) -> tuple[bool, str]:
    """
    The response is requested sorted by average strength-of-schedule rank, so each team's position in
    the list must equal its rank. Checking that against the pinned column proves the column is the one
    we think it is, on every pull, rather than trusting a position that could silently move.
    """
    import config
    idx = config.ESPN_SOS_RESUME_INDEX
    if idx is None or not config.ESPN_SOS_SORTED_REQUEST:
        return False, "column not pinned"
    vals = resume_values(payload)
    checked = mismatched = 0
    for pos, (_, v) in enumerate(vals, start=1):
        if not isinstance(v, list) or idx >= len(v):
            continue
        try:
            got = int(float(v[idx]))
        except (TypeError, ValueError):
            continue
        checked += 1
        if got != pos:
            mismatched += 1
    if checked < 10:
        return False, f"only {checked} teams had a readable value at position {idx}"
    if mismatched > max(2, checked * 0.05):
        return False, (f"position {idx} disagreed with sort order on {mismatched} of {checked} teams; "
                       f"ESPN may have changed the column layout, so nothing was stored")
    return True, f"position {idx} matched the sorted order on {checked - mismatched} of {checked} teams"


def normalize(payload, season: int, resolver: ids.AliasResolver, ts, unmatched: set[str]) -> tuple[pd.DataFrame, list[str]]:
    """Returns (rows, notes). Notes describe anything that could not be read, for the job log."""
    entries = _entries(payload)
    notes: list[str] = []
    if not entries:
        top = list(payload.keys())[:12] if isinstance(payload, dict) else type(payload).__name__
        return pd.DataFrame(), [f"no team entries found; top-level keys were {top}"]
    ok, why = verify_sos_column(payload)
    notes.append(("strength-of-schedule column verified: " if ok else "strength-of-schedule column NOT verified: ") + why)
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
        _collect(e, nums)
        sos = _pick(nums, _SOS_NORM, "rank")
        if sos is None and ok:
            sos = _resume_by_index(e, __import__("config").ESPN_SOS_RESUME_INDEX)
        if sos is None:
            no_sos += 1
        rows.append({"team_id": tid, "season": season, "espn_team": name,
                     "sos_rank_espn": None if sos is None else int(sos),
                     "remaining_sos_rank_espn": (lambda v: None if v is None else int(v))(_pick(nums, _REM_NORM, "rank")),
                     "strength_of_record_rank": (lambda v: None if v is None else int(v))(_pick(nums, _SOR_NORM, "rank")),
                     "fpi": _pick(nums, {"fpi"}, "value"), "source": "espn_fpi", "retrieved_at": ts.isoformat()})
    if rows and no_sos == len(rows) and __import__("config").ESPN_SOS_RESUME_INDEX is None:
        notes.append("the response carries no field labels; set config.ESPN_SOS_RESUME_INDEX once the "
                     "strength-of-schedule position is confirmed against a team whose rank you can read")
    if rows and no_sos == len(rows):
        sample = sorted(set(list(nums.keys())))[:40] if nums else []
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
        _collect(e, nums)
        out.append(f"numeric fields on first entry: {sorted(nums.keys())[:60]}")
        out.append(f"team name resolved to: {_team_name(e)!r}")
        cats = e.get("categories")
        if isinstance(cats, list):
            out.append(f"categories: {len(cats)}")
            for c in cats[:6]:
                if not isinstance(c, dict):
                    continue
                nm = c.get("name") or c.get("displayName")
                keys = sorted(c.keys())
                names = c.get("names") or c.get("labels") or c.get("displayNames")
                out.append(f"  category {nm!r} keys={keys} names={str(names)[:220]}")
                for a in ("ranks", "values", "totals"):
                    if isinstance(c.get(a), list):
                        out.append(f"    {a}[:12] = {c[a][:12]}")
    elif isinstance(payload, dict):
        out.append(f"top-level keys: {sorted(payload.keys())[:20]}")
    return "\n    ".join(out)
