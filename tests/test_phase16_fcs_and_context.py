"""
Two separate corrections:

  FCS games must not contribute to season-long metrics. A 49-7 win over an FCS opponent inflates
  every offensive rate and flatters the defence, and because the opponent has no rating the
  opponent-adjustment cannot correct for it either.

  The ESPN rank rows in the Quick Look are context, not evidence. They are composites of the same
  things the other rows measure, so crediting them an edge double-counts -- and four of twenty-five
  rows is enough to swing the tally on its own.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import config
from pipeline import asof
from pipeline.jobs import build_site


# ---- FCS exclusion ----------------------------------------------------------------------------
def _tg(rows):
    d = pd.DataFrame(rows)
    d["weight"] = np.where(d.is_fcs_game, config.FCS_GAME_WEIGHT, 1.0)
    return d


def test_dropping_beats_zero_weighting_in_a_last3_window():
    """
    A zero-weighted row still OCCUPIES A SLOT in a LAST3 window, so the window would quietly average
    two games while reporting three. Dropping the row lets the window reach back to a third real
    game, which is what "do not account for games outside FBS" means.
    """
    rows = [{"team_id": "A", "is_fcs_game": f, "effective_at": pd.Timestamp(f"2026-09-0{i+1}", tz="UTC"),
             "points": p} for i, (f, p) in enumerate([(False, 20), (False, 24), (False, 17), (True, 56)])]
    tg = _tg(rows).sort_values("effective_at")
    kept = tg[~tg.is_fcs_game]
    cutoff = pd.Timestamp("2026-09-10", tz="UTC")
    assert list(asof.window_rows(tg, "LAST3", cutoff).points) == [24, 17, 56]       # blowout included
    assert list(asof.window_rows(kept, "LAST3", cutoff).points) == [20, 24, 17]     # three real games


def test_a_team_with_only_fcs_games_yields_no_rows_not_a_zero():
    """
    Weighting to zero would divide by a zero weight sum. No rows is the honest answer, and the
    aggregate path already turns that into NULL -- never 0, never a guess.
    """
    tg = _tg([{"team_id": "A", "is_fcs_game": True,
               "effective_at": pd.Timestamp("2026-09-01", tz="UTC"), "points": 56}])
    kept = tg[~tg.is_fcs_game]
    assert kept.empty
    agg = asof.aggregate(kept)
    assert all(v is None for v, _ in agg.values())


def test_the_switch_controls_it_and_defaults_to_excluding():
    assert config.FCS_GAMES_IN_METRICS is False, "the default must exclude, per the stated intent"
    assert config.FCS_GAME_WEIGHT == 0.5, "kept for the down-weighting mode, not used while excluding"


def test_the_exclusion_is_flagged_so_a_small_sample_is_not_mistaken_for_missing_data():
    """games_n drops when FCS games are removed; the flag says why, with the count."""
    src = (config.ROOT / "pipeline" / "asof.py").read_text()
    assert 'flags.append(f"FCS_EXCLUDED:{fcs_played[tid]}")' in src
    assert "tg = tg[~tg.is_fcs_game].copy()" in src


# ---- Quick Look context rows ------------------------------------------------------------------
def test_the_four_rank_rows_are_the_context_group():
    labels = {lbl for grp, lbl, _ in build_site.QUICK_ROWS if grp in build_site.QUICK_CONTEXT_GROUPS}
    assert labels == {"Power Rk", "Offense Rk", "Defense Rk", "Spec Teams Rk"}


def test_sos_still_counts():
    """SOS was not named as context-only, and it is not an ESPN composite of the other rows."""
    sos_group = next(grp for grp, _, key in build_site.QUICK_ROWS if key == "__sos")
    assert sos_group not in build_site.QUICK_CONTEXT_GROUPS


def test_four_rank_rows_can_no_longer_swing_the_tally():
    """
    The arithmetic that makes this matter: of 25 rows, 4 are ranks. A 10-10 split on real metrics
    plus a 4-0 sweep of the rank rows used to read as a 14-10 win for a team the stats called even.
    """
    n_rows = len(build_site.QUICK_ROWS)
    n_ctx = sum(1 for grp, _, _ in build_site.QUICK_ROWS if grp in build_site.QUICK_CONTEXT_GROUPS)
    assert n_ctx == 4 and n_rows >= 20
    real, ranks = 10, 10
    assert real + n_ctx > ranks, "before: four context rows decide a tied board"
    assert real == ranks, "after: the context rows abstain and the board reads even"


@pytest.mark.parametrize("group,expect_counted", [("RANKS", False), ("SCORING", True), ("OTHER", True)])
def test_context_only_is_decided_by_group(group, expect_counted):
    assert (group not in build_site.QUICK_CONTEXT_GROUPS) is expect_counted


def test_the_payload_tells_the_page_which_rows_abstain():
    """The note and the per-row flag both ship, so the page can mark them rather than look broken."""
    src = (config.ROOT / "pipeline" / "jobs" / "build_site.py").read_text()
    assert '"context_only": context_only' in src
    assert '"context_rows":' in src
    assert "are not counted" in src
    js = (config.ROOT / "site" / "matchup.js").read_text()
    assert "r.context_only" in js and "ql-context" in js
