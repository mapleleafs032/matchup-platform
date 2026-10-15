"""
Quick Look window filters, and the opponent power rank in the schedule panel.

The payload shape changed from {adjustment: block} to a single block whose rows carry per-window
cells. That kind of change fails SILENTLY as a blank panel, so these tests exercise the builder's
output AND run the page's own render function over it.
"""
from __future__ import annotations

import json
import subprocess
import textwrap

import pandas as pd
import pytest

import config
from pipeline.jobs import build_site


def _metrics_rows():
    """Two metrics with DIFFERENT winners per window, so a window change must change the tally."""
    rows = []
    # away leads on season, home leads on last 3 -- the whole point of the filter
    pts = {"SEASON": (0.90, 0.10), "LAST5": (0.80, 0.20), "LAST3": (0.10, 0.90),
           "HOME": (0.50, 0.50), "AWAY": (0.50, 0.50), "CONF": (0.50, 0.50)}
    row = {"metric_key": "points_per_game", "label": "Points/Gm", "group": "SCORING",
           "description": "", "unit": None, "higher_is_better": True, "away": {}, "home": {}}
    for w, (ap, hp) in pts.items():
        for adj in ("RAW", "OPP_ADJ"):
            row["away"][f"{w}:{adj}"] = {"v": 30.0 * ap, "rank": 5, "pct": ap}
            row["home"][f"{w}:{adj}"] = {"v": 30.0 * hp, "rank": 60, "pct": hp}
    rows.append(row)
    return rows


class _S:
    league, season = "CFB", 2026
    reg = pd.DataFrame([{"metric_key": "points_per_game", "label": "Points/Gm", "group": "SCORING",
                         "description": "", "unit": None, "higher_is_better": True,
                         "leagues": "CFB,NFL"}])


@pytest.fixture
def quick(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TABLES", tmp_path / "t")
    monkeypatch.setattr(build_site, "_espn_ranks", lambda S: pd.DataFrame())
    monkeypatch.setattr(build_site, "_espn_sos_manual", lambda S: {})
    monkeypatch.setattr(build_site, "_espn_sos_table", lambda S: {})
    monkeypatch.setattr(build_site, "_team_qb_metric", lambda S, t, w: (None, None))
    return build_site.build_quick_look(_S(), 7, "G1", "HOME_T", "AWAY_T", _metrics_rows())


def test_every_window_and_adjustment_is_present(quick):
    assert quick["windows"] == build_site.WINDOWS
    assert quick["default_window"] == "SEASON"
    want = {f"{w}:{a}" for w in build_site.WINDOWS for a in ("OPP_ADJ", "RAW")}
    assert set(quick["tallies"]) == want
    for r in quick["rows"]:
        assert set(r["cells"]) == want, r["label"]


def test_changing_the_window_changes_the_edge(quick):
    """If the tally did not move with the window, the filter would be decorative."""
    row = next(r for r in quick["rows"] if r["metric_key"] == "points_per_game")
    assert row["cells"]["SEASON:OPP_ADJ"]["edge"] == "away"
    assert row["cells"]["LAST3:OPP_ADJ"]["edge"] == "home"
    assert quick["winners"]["SEASON:OPP_ADJ"] == "away"
    assert quick["winners"]["LAST3:OPP_ADJ"] == "home"
    assert quick["tallies"]["SEASON:OPP_ADJ"] == {"home": 0, "away": 1}
    assert quick["tallies"]["LAST3:OPP_ADJ"] == {"home": 1, "away": 0}


def test_an_even_window_has_no_winner(quick):
    assert quick["winners"]["HOME:OPP_ADJ"] is None


def test_rows_with_no_per_window_form_are_marked_and_repeated(quick):
    """
    QBR, SOS and the four ESPN ranks are season-to-date or current standings. Showing a different
    number per window would be inventing one, so they repeat and say so.
    """
    fixed = [r for r in quick["rows"] if not r["windowed"]]
    assert {r["metric_key"] for r in fixed} >= {"__sos", "__espn_power", "__espn_off",
                                                "__espn_def", "__espn_st"}
    for r in fixed:
        vals = {json.dumps(c, sort_keys=True) for c in r["cells"].values()}
        assert len(vals) == 1, f"{r['label']} differs across windows but has no window form"
    assert "SOS" in quick["unwindowed_rows"]


def test_the_rank_rows_still_abstain_in_every_window(quick):
    for r in quick["rows"]:
        if r["context_only"]:
            assert all(c["edge"] is None for c in r["cells"].values()), r["label"]


# ---- the page's own renderer, run for real ----------------------------------------------------
JS_HARNESS = """
const fs = require('fs');
let src = fs.readFileSync(process.argv[2], 'utf8');
// lift the two functions under test out of the module closure
const start = src.indexOf('  const WLAB =');
const end = src.indexOf('  paintQuick();');
const body = src.slice(start, end);
const d = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const el = (t, a, ...k) => ({ t, a, k, append(){}, replaceChildren(){}, children: [],
                              setAttribute(){}, addEventListener(){}, get textContent(){ return ''; } });
const fmt = { kick: () => '' };
const gameLink = (g, t) => t;
const fn = new Function('d', 'el', 'fmt', 'gameLink',
  body + '\\nreturn { quickTableHTML, qlAdj };');
const out = fn(d, el, fmt, gameLink);
const res = {};
for (const w of d.quick_look.windows) { out.qlAdj.w = w; res[w] = out.quickTableHTML(d.quick_look); }
process.stdout.write(JSON.stringify(res));
"""


def test_the_page_renders_a_different_table_for_each_window(quick, tmp_path):
    """
    Running the real quickTableHTML, not a reimplementation. The payload reshape would otherwise
    show up as an empty panel in the browser and pass every Python test.
    """
    payload = {"quick_look": quick,
               "game": {"league": "CFB",
                        "away": {"short": "Away", "name": "Away", "abbr": "AWY"},
                        "home": {"short": "Home", "name": "Home", "abbr": "HOM"}}}
    pj = tmp_path / "payload.json"; pj.write_text(json.dumps(payload))
    hj = tmp_path / "h.js"; hj.write_text(textwrap.dedent(JS_HARNESS))
    r = subprocess.run(["node", str(hj), str(config.ROOT / "site" / "matchup.js"), str(pj)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[:2000]
    tables = json.loads(r.stdout)
    assert set(tables) == set(build_site.WINDOWS)
    for w, html in tables.items():
        assert '<table class="ql">' in html, w
        assert "Points/Gm" in html, w
    # season favours away, last 3 favours home -- the rendered tally must say so
    assert "AWY" in tables["SEASON"]
    assert tables["SEASON"] != tables["LAST3"], "the window selector changed nothing on the page"
    assert "season" in tables["SEASON"].lower() and "last 3" in tables["LAST3"].lower()


# ---- opponent power rank ----------------------------------------------------------------------
def test_schedule_rows_carry_the_opponent_power_rank():
    src = (config.ROOT / "pipeline" / "jobs" / "build_site.py").read_text()
    assert '"opponent_power_rk": power.get(opp)' in src
    assert "def _espn_power_by_team" in src
    js = (config.ROOT / "site" / "matchup.js").read_text()
    assert "opponent_power_rk" in js and '"Pwr"' in js
    # an unranked opponent must not borrow a number
    assert "FCS" in js and "pwr none" in js
