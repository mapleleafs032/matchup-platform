"""
All outbound HTTP goes through RequestManager. Nothing else in the codebase calls requests.get.

Responsibilities:
  * enforce per-provider daily/monthly budgets (config.API_BUDGET)
  * retry with backoff on 5xx / timeouts; never retry on 4xx auth errors
  * archive every successful payload to data/raw/<provider>/<date>/<sha256>.json.gz
  * append an index row to data/tables/ops/raw_responses.csv
  * expose provider-reported remaining quota when headers carry it
"""
from __future__ import annotations
import gzip
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests

import config
from pipeline import storage


class BudgetExceeded(RuntimeError):
    pass


class ProviderError(RuntimeError):
    pass


@dataclass
class FetchResult:
    payload: Any
    raw_id: str
    retrieved_at: datetime
    remaining_reported: Optional[int]
    http_status: int


class RequestManager:
    BUDGET_PATH = config.TABLES / "ops" / "api_budget.csv"
    RAW_INDEX_PATH = config.TABLES / "ops" / "raw_responses.csv"

    def __init__(self, provider: str, job_run_id: str, session: Optional[requests.Session] = None):
        if provider not in config.API_BUDGET:
            raise ValueError(f"Unknown provider {provider}; add it to config.API_BUDGET")
        self.provider = provider
        self.job_run_id = job_run_id
        self.session = session or requests.Session()
        self.calls_this_run = 0
        self._budget = self._load_budget()

    # ---- budget ----------------------------------------------------------------
    def _load_budget(self) -> pd.DataFrame:
        if self.BUDGET_PATH.exists():
            df = pd.read_csv(self.BUDGET_PATH, dtype={"provider": str, "day": str})
        else:
            df = pd.DataFrame(columns=["provider", "day", "requests", "credits", "failures",
                                       "remaining_reported", "last_request_at"])
        return df

    def _used(self, scope: str) -> int:
        d = self._budget[self._budget.provider == self.provider]
        if d.empty:
            return 0
        today = date.today().isoformat()
        if scope == "day":
            return int(d[d.day == today].credits.fillna(0).sum())
        month = today[:7]
        return int(d[d.day.str.startswith(month)].credits.fillna(0).sum())

    def remaining_monthly(self) -> int:
        return config.API_BUDGET[self.provider]["monthly"] - self._used("month")

    def _check_budget(self, cost: int) -> None:
        lim = config.API_BUDGET[self.provider]
        if self._used("month") + cost > lim["monthly"]:
            raise BudgetExceeded(f"{self.provider}: monthly budget {lim['monthly']} would be exceeded")
        if self._used("day") + cost > lim["daily_soft"]:
            raise BudgetExceeded(f"{self.provider}: daily soft limit {lim['daily_soft']} would be exceeded")

    def _record(self, cost: int, failed: bool, remaining: Optional[int]) -> None:
        today = date.today().isoformat()
        mask = (self._budget.provider == self.provider) & (self._budget.day == today)
        now = datetime.now(timezone.utc).isoformat()
        if mask.any():
            i = self._budget[mask].index[0]
            self._budget.loc[i, "requests"] = int(self._budget.loc[i, "requests"]) + 1
            self._budget.loc[i, "credits"] = int(self._budget.loc[i, "credits"] or 0) + (0 if failed else cost)
            self._budget.loc[i, "failures"] = int(self._budget.loc[i, "failures"]) + (1 if failed else 0)
            if remaining is not None:
                self._budget.loc[i, "remaining_reported"] = remaining
            self._budget.loc[i, "last_request_at"] = now
        else:
            row = {"provider": self.provider, "day": today, "requests": 1, "credits": 0 if failed else cost,
                   "failures": 1 if failed else 0, "remaining_reported": remaining, "last_request_at": now}
            self._budget = pd.concat([self._budget, pd.DataFrame([row])], ignore_index=True)
        self.BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._budget.to_csv(self.BUDGET_PATH, index=False)

    # ---- fetch -----------------------------------------------------------------
    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            cost: int = 1, timeout: int = 30, max_retries: int = 3, expect_json: bool = True) -> FetchResult:
        self._check_budget(cost)
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                time.sleep(2 ** attempt)
                continue
            remaining = self._remaining_from_headers(resp.headers)
            if resp.status_code in (401, 403):
                self._record(cost, failed=True, remaining=remaining)
                raise ProviderError(f"{self.provider} auth error {resp.status_code}: check API key secret")
            if resp.status_code == 429:
                self._record(cost, failed=True, remaining=remaining)
                raise BudgetExceeded(f"{self.provider} returned 429 (quota exhausted at provider)")
            if resp.status_code >= 500:
                last_err = ProviderError(f"{self.provider} {resp.status_code}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                self._record(cost, failed=True, remaining=remaining)
                raise ProviderError(f"{self.provider} HTTP {resp.status_code}: {resp.text[:200]}")
            body = resp.content
            payload = resp.json() if expect_json else body.decode("utf-8", errors="replace")
            retrieved_at = datetime.now(timezone.utc)
            raw_id = self._archive(url, params, body, retrieved_at, resp.status_code)
            self._record(cost, failed=False, remaining=remaining)
            self.calls_this_run += 1
            return FetchResult(payload, raw_id, retrieved_at, remaining, resp.status_code)
        self._record(cost, failed=True, remaining=None)
        raise ProviderError(f"{self.provider}: failed after {max_retries} attempts: {last_err}")

    @staticmethod
    def _remaining_from_headers(h) -> Optional[int]:
        for key in ("x-requests-remaining", "X-Requests-Remaining", "x-ratelimit-remaining"):
            if key in h:
                try:
                    return int(float(h[key]))
                except ValueError:
                    return None
        return None

    # ---- raw archive -----------------------------------------------------------
    def _archive(self, url: str, params: dict | None, body: bytes, retrieved_at: datetime, status: int) -> str:
        sha = hashlib.sha256(body).hexdigest()
        day_dir = config.RAW / self.provider / retrieved_at.date().isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{sha}.json.gz"
        archived = self.provider in config.RAW_ARCHIVE_PROVIDERS
        if archived and not path.exists():
            with gzip.open(path, "wb") as f:
                f.write(body)
        safe_params = {k: v for k, v in (params or {}).items() if "key" not in k.lower()}
        storage.append_csv(self.RAW_INDEX_PATH, pd.DataFrame([{
            "raw_id": sha, "provider": self.provider, "endpoint": url.split("?")[0],
            "params": json.dumps(safe_params, sort_keys=True), "retrieved_at": retrieved_at.isoformat(),
            "http_status": status, "bytes": len(body), "path": (str(path.relative_to(config.ROOT)) if path.is_relative_to(config.ROOT) else str(path)) if archived else "",
            "job_run_id": self.job_run_id,
        }]), key_cols=["raw_id", "job_run_id"], on_duplicate="skip")
        return sha
