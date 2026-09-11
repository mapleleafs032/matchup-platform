"""
python -m pipeline.jobs.ingest_splits --league NFL
python -m pipeline.jobs.ingest_splits --league BOTH --dry-run     # parse and report, write nothing

Reads every file in data/manual/splits_paste/ whose name starts with the league (NFL_/CFB_) and, if a
licensed feed is configured, that too. Writes APPEND-ONLY rows to
    data/tables/market/splits/{league}/{season}/W{ww}.csv

File naming: {LEAGUE}_{PERIOD}_{label}.txt   e.g. NFL_FULL_wed-morning.txt, CFB_1H_thu.txt
Period defaults to FULL when the name does not say. The file's own modified time is the snapshot time
unless the first line contains an ISO timestamp, so a paste can be backdated deliberately.

Validation (§44): game must be scheduled and unlocked, percentages 0..1, the two sides of a market must
sum to 100, and a market whose numbers do not agree is dropped — never coerced.
"""
from __future__ import annotations
import argparse
import re
from datetime import datetime, timezone

import pandas as pd

import config
from pipeline import ids, storage
from pipeline.log import JobRun, ValidationLog
from providers import splits_manual, splits_feed, vsin

PASTE_DIR = config.DATA / "manual" / "splits_paste"
OUT = config.TABLES / "market" / "splits"
PCT_COLS = [f"{m.lower()}_{k}_pct_home" for m in splits_manual.MARKETS for k in ("ticket", "money")]
SPLIT_COLS = ["split_id", "game_id", "week", "retrieved_at", "book", "period"] + PCT_COLS + \
             ["line_spread_home", "line_total", "line_ml_home", "line_ml_away", "source"]
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _week_of(games: pd.DataFrame, game_id: str) -> int | None:
    r = games[games.game_id == game_id]
    return int(r.week.iloc[0]) if len(r) else None


def _current_lines(league: str, season: int, week: int) -> dict:
    """Latest pre-now line per game, so a splits snapshot records the number people were betting into."""
    p = config.TABLES / "market" / "snapshots" / league / str(season) / f"W{week:02d}.csv"
    if not p.exists():
        return {}
    s = pd.read_csv(p)
    s = s[s.spread_home.notna() | s.total.notna()]
    if s.empty:
        return {}
    s["_pri"] = s.book.map({b: i for i, b in enumerate(config.ODDS_BOOK_PRIORITY)}).fillna(99)
    s = s.sort_values(["game_id", "retrieved_at", "_pri"], ascending=[True, False, True]).drop_duplicates("game_id")
    return {r.game_id: {"line_spread_home": r.spread_home, "line_total": r.total} for _, r in s.iterrows()}


def read_vsin(league: str, season: int, resolver: ids.AliasResolver, games: pd.DataFrame, vlog: ValidationLog) -> tuple[list[dict], list[dict]]:
    from providers.base import RequestManager, BudgetExceeded, ProviderError
    rm = RequestManager("vsin", "splits")
    teams = storage.read_table(config.TABLES / "ref" / "teams.parquet")
    try:
        html, ts = vsin.fetch(rm, league)
    except (ProviderError, BudgetExceeded) as e:
        vlog.warn("PROVIDER_FAIL", "vsin", league, str(e)[:120], "200")
        print(f"  VSiN {league}: fetch failed ({str(e)[:80]})")
        return [], []
    rows, problems = vsin.parse(html)
    if not rows:
        for p in problems:
            vlog.warn("SPLITS_UNREADABLE", "vsin", league, p.get("why", "")[:160], "parsed rows")
        print(f"  VSiN {league}: no rows parsed — {problems[0].get('why') if problems else 'unknown'}")
        return [], problems
    added, unmatched, conflicts = vsin.seed_aliases(rows, league, resolver, teams)
    for u in unmatched:
        vlog.warn("ALIAS_UNMATCHED", u, "vsin_slug", u, "add to team_aliases.csv (provider=vsin)")
    if unmatched:
        out = config.DATA / "manual" / f"vsin_unmapped_{league}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"provider": "vsin", "alias": unmatched, "provider_id": None, "team_id": "", "season_from": None, "season_to": None}).to_csv(out, index=False)
        print(f"    unmapped slugs written to {out.relative_to(config.ROOT)} — fill team_id and paste the rows into data/tables/ref/team_aliases.csv")
        print(f"    {', '.join(unmatched[:20])}" + (" ..." if len(unmatched) > 20 else ""))
    if conflicts:
        print(f"    CORRECTED {len(conflicts)} team alias(es) written by an earlier, looser matcher:")
        for c in conflicts:
            print(f"      {c['slug']}: was {c['stored']} -> now {c['expected']}")
        vlog.warn("ALIAS_CORRECTED", ";".join(c["slug"] for c in conflicts), "vsin_slug",
                  "; ".join(f"{c['slug']} {c['stored']}->{c['expected']}" for c in conflicts), "verified mapping")
    recs, probs = vsin.to_records(rows, league, games, resolver, ts)
    print(f"  VSiN {league}: {len(rows)} team rows ({len(rows)//2} pairs) -> {len(recs)} games"
          + (f"; {added} new team aliases learned" if added else "")
          + (f"; {len(unmatched)} slugs unmapped" if unmatched else ""))
    allp = problems + probs
    if allp:
        by_kind: dict = {}
        for x in allp:
            by_kind.setdefault(x.get("kind", "other"), []).append(x.get("why", ""))
        print(f"    problem breakdown ({len(allp)} total):")
        for kind, whys in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
            print(f"      {kind}: {len(whys)}  e.g. {whys[0][:110]}")
    return recs, problems + probs


def read_pastes(league: str, season: int, resolver: ids.AliasResolver, games: pd.DataFrame, vlog: ValidationLog) -> tuple[list[dict], list[dict]]:
    recs, problems = [], []
    if not PASTE_DIR.exists():
        return recs, problems
    for path in sorted(PASTE_DIR.glob("*.txt")):
        name = path.name.upper()
        if not name.startswith(league):
            continue
        period = "1H" if "_1H" in name else "FULL"
        text = path.read_text(errors="replace")
        first = text.splitlines()[0] if text.splitlines() else ""
        m = _ISO.search(first)
        ts = (pd.Timestamp(m.group(0).replace(" ", "T"), tz="UTC") if m
              else pd.Timestamp(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)))
        rows, probs = splits_manual.parse_paste(text, league, resolver)
        pairs, probs2 = splits_manual.pair_rows(rows, games, period, config.SPLITS_BOOK_DEFAULT, ts.isoformat(), "manual_paste")
        for p in probs + probs2:
            p["file"] = path.name
        problems += probs + probs2
        recs += pairs
        print(f"  {path.name}: {len(rows)} team rows -> {len(pairs)} games ({period}), {len(probs) + len(probs2)} unreadable lines")
    return recs, problems


def validate(recs: list[dict], games: pd.DataFrame, vlog: ValidationLog) -> pd.DataFrame:
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    known = set(games.game_id)
    open_games = set(games[games.status == "SCHEDULED"].game_id)
    keep = []
    for i, r in df.iterrows():
        key = f"{r.game_id}_{r.period}_{r.retrieved_at}"
        if r.game_id not in known:
            vlog.reject("IDENTITY", key, "game_id", r.game_id, "known game"); continue
        if r.game_id not in open_games:
            vlog.warn("GAME_LOCKED", key, "game_id", r.game_id, "splits after kickoff are ignored"); continue
        if r.period not in config.SPLITS_PERIODS:
            vlog.reject("RANGE", key, "period", r.period, str(config.SPLITS_PERIODS)); continue
        bad = False
        for c in PCT_COLS:
            v = r.get(c)
            if pd.notna(v) and not (0.0 <= float(v) <= 1.0):
                vlog.reject("RANGE", key, c, v, "0..1"); bad = True
        if bad or not any(pd.notna(r.get(c)) for c in PCT_COLS):
            continue
        keep.append(i)
    return df.loc[keep].copy()


def repair_splits_files(league: str, season: int, vlog: ValidationLog) -> int:
    """
    Rewrite splits files whose rows do not match their header. Files written before the schema-growth fix
    in storage.append_csv can hold rows with extra trailing fields, which shifts columns when read back.
    Rows that cannot be read cleanly are dropped -- a missing snapshot costs a chart point, a shifted one
    would poison the divergence and reverse-line-movement flags.
    """
    from pipeline.splits_engine import NUMERIC_COLS
    fixed = 0
    d = OUT / league / str(season)
    if not d.exists():
        return 0
    for path in sorted(d.glob("W*.csv")):
        raw = path.read_text().splitlines()
        if len(raw) < 2:
            continue
        n_head = raw[0].count(",")
        bad = [i for i, ln in enumerate(raw[1:], start=1) if ln.count(",") != n_head]
        if not bad:
            continue
        good = pd.read_csv(path, on_bad_lines="skip")
        for c in NUMERIC_COLS:
            if c in good.columns:
                good[c] = pd.to_numeric(good[c], errors="coerce")
        # a row whose source column is not a known source is a shifted row; drop it
        if "source" in good.columns:
            good = good[good.source.astype(str).isin(["vsin_dk", "manual_paste", "manual_csv", "splits_feed"])]
        good.to_csv(path, index=False)
        vlog.warn("SPLITS_REPAIRED", path.name, "rows", f"{len(bad)} ragged row(s) removed", "header field count")
        print(f"  repaired {path.name}: dropped {len(bad)} ragged row(s), {len(good)} kept")
        fixed += len(bad)
    return fixed


def run(league: str, season: int, dry: bool, job: JobRun) -> None:
    games = storage.read_table(storage.games_path(league, season))
    if games.empty:
        job.status = "SKIPPED"; job.message = f"no games table for {league} {season}"; return
    resolver = ids.AliasResolver.load()
    vlog = ValidationLog(job.job_run_id, "betting_splits")
    repaired = repair_splits_files(league, season, vlog)
    if repaired:
        job.message += f" repaired {repaired} ragged splits rows;"
    recs, problems = [], []
    if config.VSIN.get("enabled"):
        r, p = read_vsin(league, season, resolver, games, vlog)
        recs += r; problems += p
    r, p = read_pastes(league, season, resolver, games, vlog)
    recs += r; problems += p
    if config.SPLITS_FEED.get("enabled"):
        from providers.base import RequestManager
        rm = RequestManager("splits_feed", job.job_run_id)
        for wk in sorted(games[games.status == "SCHEDULED"].week.unique())[:2]:
            try:
                recs += splits_feed.fetch(rm, league, season, int(wk))
            except (splits_feed.NotConfigured, NotImplementedError) as e:
                vlog.warn("PROVIDER_FAIL", "splits_feed", "", str(e)[:120], "records")
    clean = validate(recs, games, vlog)
    for p in problems[:40]:
        vlog.warn("SPLITS_UNREADABLE", p.get("file", ""), "line", f"{p.get('line')}: {p.get('why')} | {p.get('raw', '')[:60]}", "team + percentages")
    if clean.empty:
        vlog.flush()
        print(f"{league}: nothing usable to write ({len(problems)} unreadable lines)")
        job.message += f" {len(problems)} unreadable lines;"
        return
    clean["week"] = clean.game_id.map(lambda g: _week_of(games, g))
    written = 0
    for (wk, ), part in clean.groupby(["week"]):
        lines = _current_lines(league, season, int(wk))
        part = part.copy()
        # the source's own line is the number the splits refer to; our snapshot only fills a gap
        for col in ("line_spread_home", "line_total", "line_ml_home", "line_ml_away"):
            fallback = part.game_id.map(lambda g: (lines.get(g) or {}).get(col))
            part[col] = part[col].where(part[col].notna(), fallback) if col in part.columns else fallback
        part["split_id"] = part.game_id + "_" + part.period + "_" + part.book + "_" + part.retrieved_at.astype(str)
        part = part.reindex(columns=SPLIT_COLS)
        if dry:
            print(part.head(8).to_string(index=False)); written += len(part); continue
        out_path = OUT / league / str(season) / f"W{int(wk):02d}.csv"
        n_fixed = storage.repair_csv(out_path, SPLIT_COLS)     # heal files written before a column was added
        if n_fixed:
            print(f"    repaired {out_path.name}: {n_fixed} existing rows re-aligned to the current columns")
        written += storage.append_csv(out_path, part, ["split_id"], on_duplicate="skip")
    vlog.flush()
    job.rows_written = written
    print(f"{league} {season}: {written} splits rows{' (dry run, nothing written)' if dry else ''}; {len(problems)} unreadable lines; "
          f"games covered: {clean.game_id.nunique()}; periods: {sorted(clean.period.unique())}")


def explain(league: str, season: int, needle: str) -> None:
    """
    Print exactly what the source published for the games matching `needle`, and what we stored.
    Answers "why is this cell blank" without guessing from the rendered card.
    """
    from providers.base import RequestManager
    rm = RequestManager("vsin", "explain")
    games = storage.read_table(storage.games_path(league, season))
    resolver = ids.AliasResolver.load()
    html, ts = vsin.fetch(rm, league)
    rows, problems = vsin.parse(html)
    print(f"VSiN {league}: {len(rows)} team rows parsed at {ts.isoformat()}")
    n = needle.lower()
    # raw cell text for the matching rows: shows whether a blank value is the source's or our parser's
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.find_all("tr"):
            links = [a.get("href", "") for a in tr.find_all("a", href=True)]
            if not any(n in h.lower() for h in links):
                continue
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
            print(f"  RAW CELLS ({len(cells)}): {cells}")
            print(f"  last 9 (the value columns): {cells[-9:]}")
    except Exception as e:
        print(f"  (raw cell dump unavailable: {e})")
    hits = [i for i, r in enumerate(rows) if n in r.slug.lower() or n in (r.name or "").lower()]
    if not hits:
        print(f"  no team row matches {needle!r}. Slugs available: " + ", ".join(sorted(r.slug for r in rows)[:25]) + " ...")
        return
    for i in hits:
        pair = [rows[i - 1], rows[i]] if i % 2 else [rows[i], rows[i + 1] if i + 1 < len(rows) else None]
        print(f"\n  --- pair around {rows[i].slug}")
        for r in pair:
            if r is None:
                continue
            print(f"    {r.slug:34s} spread={r.spread} handle={r.spread_handle} bets={r.spread_bets} | "
                  f"total={r.total} handle={r.total_handle} bets={r.total_bets} | "
                  f"ml={r.moneyline} handle={r.ml_handle} bets={r.ml_bets}")
        recs, probs = vsin.to_records([x for x in pair if x is not None], league, games, resolver, ts)
        for rec in recs:
            stored = {k: v for k, v in rec.items() if k.endswith("_pct_home")}
            print(f"    stored for {rec['game_id']}: {stored or 'nothing'}")
        for pr in probs:
            print(f"    note: {pr.get('kind')}: {pr.get('why')}")
    for pr in problems[:5]:
        if n in str(pr).lower():
            print(f"  parse problem: {pr}")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="BOTH", choices=["NFL", "CFB", "BOTH"])
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--explain", help="print what the source published for games matching this team name/slug")
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    leagues = ["NFL", "CFB"] if a.league == "BOTH" else [a.league]
    if a.explain:
        for lg in leagues:
            explain(lg, a.season, a.explain)
        return
    with JobRun("SPLITS", a.league, a.trigger) as job:
        for lg in leagues:
            run(lg, a.season, a.dry_run, job)


if __name__ == "__main__":
    main()
