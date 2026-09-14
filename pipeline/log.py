"""
Structured logs. Both files are append-only.

  job_log.csv         one row per job run
  validation_log.csv  one row per rejected/warned record
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone

import pandas as pd

import config
from pipeline import storage

JOB_LOG = config.TABLES / "ops" / "job_log.csv"
VALIDATION_LOG = config.TABLES / "ops" / "validation_log.csv"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ValidationLog:
    """Collects validation events for a job run.

    Unmatched aliases are deduplicated within a run: a name that appears on 400 roster rows is one
    fact, not 400. Logging every occurrence buried the real problems under tens of thousands of rows.
    """
    def __init__(self, job_run_id: str, table_name: str):
        self._seen_aliases: set = set()
        self._alias_counts: dict = {}
        self.job_run_id = job_run_id
        self.table_name = table_name
        self.rows: list[dict] = []

    def reject(self, rule: str, record_key: str, field: str = "", observed="", expected=""):
        self._add("REJECT", rule, record_key, field, observed, expected)

    def warn(self, rule: str, record_key: str, field: str = "", observed="", expected=""):
        self._add("WARN", rule, record_key, field, observed, expected)

    def _add(self, severity, rule, record_key, field, observed, expected):
        # An unmatched alias is one fact however many rows carry it. Logging every occurrence produced
        # tens of thousands of identical warnings and buried everything else.
        if rule == "ALIAS_UNMATCHED":
            key = str(observed)
            if key in self._seen_aliases:
                self._alias_counts[key] = self._alias_counts.get(key, 1) + 1
                return
            self._seen_aliases.add(key)
            self._alias_counts[key] = 1
        self.rows.append({
            "validation_id": uuid.uuid4().hex[:16], "job_run_id": self.job_run_id, "table_name": self.table_name,
            "rule": rule, "severity": severity, "record_key": str(record_key), "field": field,
            "observed": str(observed)[:200], "expected": str(expected)[:200], "logged_at": now_iso(),
        })

    @property
    def rejects(self) -> int:
        return sum(1 for r in self.rows if r["severity"] == "REJECT")

    def _apply_alias_counts(self):
        """Record how many rows each unmatched alias affected, so deduping loses no information."""
        for r in self.rows:
            if r.get("rule") == "ALIAS_UNMATCHED":
                n = self._alias_counts.get(str(r.get("observed")), 1)
                if n > 1:
                    r["expected"] = f"{r.get('expected','')} (seen on {n} rows)".strip()

    def flush(self) -> None:
        self._apply_alias_counts()
        if self.rows:
            storage.append_csv(VALIDATION_LOG, pd.DataFrame(self.rows), key_cols=["validation_id"], on_duplicate="skip")
            self.rows = []


class JobRun:
    """Context manager: writes a job_log row on exit whether the job succeeded or failed."""

    def __init__(self, job_name: str, league: str | None = None, trigger: str = "manual"):
        self.job_run_id = f"{job_name}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.job_name = job_name
        self.league = league
        self.trigger = trigger
        self.started_at = now_iso()
        self.rows_written = 0
        self.api_calls = 0
        self.status = "SUCCESS"
        self.message = ""

    def __enter__(self):
        print(f"[{self.started_at}] START {self.job_name} ({self.job_run_id})")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.status = "FAILED"
            self.message = f"{exc_type.__name__}: {exc}"[:500]
        row = {"job_run_id": self.job_run_id, "job_name": self.job_name, "league": self.league,
               "started_at": self.started_at, "finished_at": now_iso(), "status": self.status,
               "rows_written": self.rows_written, "api_calls": self.api_calls, "message": self.message,
               "trigger": self.trigger}
        storage.append_csv(JOB_LOG, pd.DataFrame([row]), key_cols=["job_run_id"], on_duplicate="skip")
        print(f"[{row['finished_at']}] {self.status} {self.job_name}: rows={self.rows_written} calls={self.api_calls} {self.message}")
        return False  # never swallow exceptions
