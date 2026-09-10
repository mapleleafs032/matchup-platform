"""
All table reads/writes go through here so immutability and partitioning rules live in one place.

  write_parquet(path, df)                       REBUILDABLE tables: full-file replace
  append_csv(path, df, key_cols, on_duplicate)  APPEND-ONLY tables: insert only
  read_table(path)                              parquet or csv, returns empty frame if missing
  upsert_games(df)                              games table with lock-field protection
"""
from __future__ import annotations
from pathlib import Path
from typing import Literal

import pandas as pd

import config


def repair_csv(path: Path, expected_cols: list[str]) -> int:
    """
    Repair an append-only CSV whose rows gained fields without the header being rewritten (a bug fixed in
    append_csv, but files written by the older code are already malformed). Rows are re-aligned against
    `expected_cols`: short rows gain empty cells, and the header is rewritten. No recorded value changes.
    Returns the number of rows recovered, or 0 if the file was already sound.
    """
    import csv
    if not path.exists():
        return 0
    with path.open(newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return 0
    header, data = rows[0], rows[1:]
    widest = max((len(r) for r in data), default=len(header))
    if widest <= len(header):
        return 0                                    # already sound
    if len(expected_cols) < widest:
        expected_cols = expected_cols + [f"extra_{i}" for i in range(widest - len(expected_cols))]
    new_header = expected_cols[:widest]
    fixed = [r + [""] * (widest - len(r)) for r in data]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(new_header)
        w.writerows(fixed)
    return len(fixed)


def read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def write_parquet(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def append_csv(path: Path, df: pd.DataFrame, key_cols: list[str],
               on_duplicate: Literal["skip", "reject"] = "reject") -> int:
    """
    Append rows to an append-only CSV. Existing rows are never touched.
    Returns number of rows written. Duplicate keys either skip or raise.
    """
    if df.empty:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_table(path)
    if not existing.empty:
        merged_keys = existing[key_cols].astype(str).agg("|".join, axis=1)
        new_keys = df[key_cols].astype(str).agg("|".join, axis=1)
        dup_mask = new_keys.isin(set(merged_keys))
        if dup_mask.any():
            if on_duplicate == "reject":
                raise ValueError(f"append_csv: {int(dup_mask.sum())} duplicate key(s) for {path.name}: "
                                 f"{new_keys[dup_mask].head(3).tolist()}")
            df = df[~dup_mask]
            if df.empty:
                return 0
        # Column union, preserving existing order. When the schema has GROWN (a metric added after this
        # file was first written), the header must be rewritten or every appended row carries more fields
        # than the header declares and the file becomes unreadable. Existing values are never altered:
        # old rows simply gain empty cells for the new columns.
        new_cols = [c for c in df.columns if c not in existing.columns]
        cols = list(existing.columns) + new_cols
        df = df.reindex(columns=cols)
        if new_cols:
            existing.reindex(columns=cols).to_csv(path, index=False)
            df.to_csv(path, mode="a", header=False, index=False)
        else:
            df.to_csv(path, mode="a", header=False, index=False)
    else:
        df.to_csv(path, index=False)
    return len(df)


# ---- games -------------------------------------------------------------------
LOCK_PROTECTED = ["away_team_id", "home_team_id", "kickoff_utc", "venue_id", "neutral_site", "status", "locked_at"]


def games_path(league: str, season: int) -> Path:
    return config.TABLES / "games" / league / f"{season}.parquet"


def upsert_games(league: str, season: int, incoming: pd.DataFrame) -> dict:
    """
    Games are REBUILDABLE except lock fields. Rules:
      * new game_id -> insert with status SCHEDULED (unless incoming says FINAL/CANCELLED from backfill)
      * existing SCHEDULED -> all provider fields may update
      * existing LOCKED/FINAL -> LOCK_PROTECTED columns are never changed; a change attempt is returned as a warning
    Returns counts + warnings for the job log.
    """
    path = games_path(league, season)
    existing = read_table(path)
    warnings: list[str] = []
    if existing.empty:
        out = incoming.copy()
        write_parquet(path, out)
        return {"inserted": len(out), "updated": 0, "warnings": warnings}

    existing = existing.set_index("game_id")
    incoming = incoming.set_index("game_id")
    inserted = 0
    updated = 0
    for gid, row in incoming.iterrows():
        if gid not in existing.index:
            existing.loc[gid] = row
            inserted += 1
            continue
        cur = existing.loc[gid]
        if cur["status"] in ("LOCKED", "FINAL"):
            for col in LOCK_PROTECTED:
                if col in ("status", "locked_at"):
                    continue  # providers always resend SCHEDULED; silently ignored on locked rows
                if col in row and pd.notna(row[col]) and str(row[col]) != str(cur[col]):
                    warnings.append(f"{gid}: attempted change to locked field {col} ({cur[col]} -> {row[col]}) ignored")
            for col in row.index:
                if col not in LOCK_PROTECTED:
                    existing.loc[gid, col] = row[col]
        else:
            for col in row.index:
                if col in ("status", "locked_at"):
                    continue
                existing.loc[gid, col] = row[col]
        updated += 1
    out = existing.reset_index()
    write_parquet(path, out)
    return {"inserted": inserted, "updated": updated, "warnings": warnings}
