"""
python -m pipeline.jobs.probe_espn_sos --season 2026

Decides which column of ESPN's unlabelled `resume` array is strength of schedule -- or proves that none
of them is usable.

The problem: requested sorted by SOS, resume[2] counted 1,2,3... down the list. That is exactly what a
real SOS rank looks like, AND exactly what a meaningless row index looks like. One observation cannot
separate them.

The experiment: pull the same season under two different sorts.
  * a column whose values FOLLOW the row order under both sorts is a row index -- useless
  * a column whose values STAY WITH THE TEAM across both sorts is a real statistic
Teams are then listed with every candidate column so a known rank can pin it for good.
"""
from __future__ import annotations
import argparse

import config
from providers import espn_fpi
from providers.base import RequestManager, ProviderError

SORTS = [("resume.avgsosrank:asc", "sorted by average SOS rank"),
         ("resume.strengthofrecord:desc", "sorted by strength of record")]


def _pull(rm, season, sort):
    params = {"region": "us", "lang": "en", "contentorigin": "espn", "limit": 400, "page": 1, "season": season}
    if sort:
        params["sort"] = sort
    res = rm.get(espn_fpi.FITT, params=params,
                 headers={"User-Agent": espn_fpi.UA, "Accept": "application/json",
                          "Referer": "https://www.espn.com/college-football/fpi/"}, timeout=45)
    return {name: vals for name, vals in espn_fpi.resume_values(res.payload) if name}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=config.SEASON)
    a = ap.parse_args(argv)
    rm = RequestManager("espn", "probe")
    pulls = {}
    for sort, label in SORTS:
        try:
            pulls[label] = _pull(rm, a.season, sort)
            print(f"{label}: {len(pulls[label])} teams")
        except ProviderError as e:
            print(f"{label}: FAILED {str(e)[:160]}")
    if len(pulls) < 2:
        print("\nNeed two successful pulls to tell a statistic from a row index.")
        return
    (la, A), (lb, B) = list(pulls.items())
    order_a = list(A.keys())
    print(f"\nfirst five teams, {la}: {order_a[:5]}")
    print(f"first five teams, {lb}: {list(B.keys())[:5]}")
    if order_a == list(B.keys()):
        print("\nBoth sorts returned the SAME order, so ESPN is ignoring the sort parameter. "
              "The probe cannot separate the columns; the page's own ordering is the only clue left.")
        return
    ncols = min(len(v) for v in A.values() if v) if A else 0
    print(f"\ncolumn behaviour across the two sorts ({ncols} columns):")
    for i in range(ncols):
        follows_row = sum(1 for pos, t in enumerate(B, start=1)
                          if t in B and len(B[t]) > i and int(float(B[t][i])) == pos)
        stays = sum(1 for t in A if t in B and len(A[t]) > i and len(B[t]) > i
                    and float(A[t][i]) == float(B[t][i]))
        both = sum(1 for t in A if t in B)
        verdict = ("ROW INDEX (tracks position, not the team)" if follows_row > both * 0.9
                   else "real statistic (travels with the team)" if stays > both * 0.9 else "inconsistent")
        print(f"  resume[{i}]: stays with team {stays}/{both}, equals row position {follows_row}/{both}  -> {verdict}")
    print("\nvalues per team for the real-statistic columns (check any one against ESPN's page to pin it):")
    for t in order_a[:8]:
        print(f"  {t}: {A[t]}")


if __name__ == "__main__":
    main()
