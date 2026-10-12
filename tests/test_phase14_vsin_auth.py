"""
The VSiN subscription session.

The failure these tests exist to prevent: a session that is NOT signed in quietly collecting the one
game on today's card and reporting a successful run. That is what hid a four-day splits outage, so
"configured but anonymous writes nothing" is tested as behaviour, not left to a log line.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pytest

import config
from pipeline import ids
from pipeline.jobs import ingest_splits
from pipeline.log import ValidationLog
from providers import vsin, vsin_auth

FIX = pathlib.Path(__file__).parent / "fixtures" / "vsin_nfl.html"

GATED_PAGE = """<html><body>
<div id="inline-lock" class="template-shown">Subscribe Now</div>
<table><tr><th>NFL - Monday, Oct 5</th><th>Spread SPR</th><th>Handle HND</th><th>Bets BET</th>
<th>Total TOT</th><th>Handle HND</th><th>Bets BET</th><th>Money ML</th><th>Handle HND</th><th>Bets BET</th></tr>
<tr><td></td><td><a href="/nfl/teams/atlanta-falcons">Atlanta Falcons</a></td><td>+1.5</td><td>39%</td>
<td>38%</td><td>47.5</td><td>92%</td><td>73%</td><td>+100</td><td>53%</td><td>42%</td></tr>
<tr><td>15</td><td><a href="/nfl/teams/new-orleans-saints">New Orleans Saints</a></td><td>-1.5</td><td>61%</td>
<td>62%</td><td>47.5</td><td>8%</td><td>27%</td><td>-120</td><td>47%</td><td>58%</td></tr>
</table></body></html>"""

SIGNED_IN_PAGE = FIX.read_text().replace(
    "<body>", '<body><a href="https://vsin.com/my-account/customer-logout/?action=logout">Sign Out</a>')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLES", tmp_path / "tables")
    monkeypatch.setattr(vsin, "SCOPE_CACHE", tmp_path / "tables" / "ops" / "vsin_scope.json")
    monkeypatch.setattr(ids, "ALIASES_PATH", tmp_path / "tables" / "ref" / "team_aliases.csv")
    monkeypatch.setattr(config, "VSIN_COOKIE", "")
    monkeypatch.setattr(config, "VSIN_EMAIL", "")
    monkeypatch.setattr(config, "VSIN_PASSWORD", "")


# ---- credentials ------------------------------------------------------------------------------
def test_credential_mode_is_cookie_then_login_then_anonymous(monkeypatch):
    assert vsin_auth.credentials_present() == (False, "anonymous")
    monkeypatch.setattr(config, "VSIN_EMAIL", "a@b.com")
    monkeypatch.setattr(config, "VSIN_PASSWORD", "pw12345")
    assert vsin_auth.credentials_present() == (True, "login")
    monkeypatch.setattr(config, "VSIN_COOKIE", "wordpress_logged_in_x=abc")
    assert vsin_auth.credentials_present() == (True, "cookie")   # cookie wins: no password sent


def test_cookie_header_is_loaded_for_every_vsin_host(monkeypatch):
    monkeypatch.setattr(config, "VSIN_COOKIE", "wordpress_logged_in_9f=val%7Csig; vsin_sub=1; _ga=GA1.2.3")
    session, state = vsin_auth.build_session(verify=False)
    assert state.mode == "cookie"
    assert set(state.cookie_names) == {"wordpress_logged_in_9f", "vsin_sub", "_ga"}
    # data.vsin.com must receive them, not only vsin.com -- the splits live on the data subdomain
    jar = session.cookies.get_dict(domain=".vsin.com")
    assert jar["vsin_sub"] == "1"


def test_empty_cookie_value_is_refused_not_silently_ignored(monkeypatch):
    monkeypatch.setattr(config, "VSIN_COOKIE", "not-a-cookie-header")
    with pytest.raises(vsin_auth.AuthFailed):
        vsin_auth.build_session(verify=False)


def test_redact_removes_credentials_from_any_message(monkeypatch):
    monkeypatch.setattr(config, "VSIN_COOKIE", "wordpress_logged_in_9f=SECRETVALUE123")
    monkeypatch.setattr(config, "VSIN_PASSWORD", "hunter2hunter2")
    msg = f"failed with {config.VSIN_COOKIE} and password {config.VSIN_PASSWORD}"
    out = vsin_auth.redact(msg)
    assert "SECRETVALUE123" not in out
    assert "hunter2hunter2" not in out


# ---- gate detection ---------------------------------------------------------------------------
def test_gate_and_sign_in_markers_are_read_from_the_page():
    signed, gated = vsin_auth._inspect(GATED_PAGE)
    assert gated is True and signed is False
    signed, gated = vsin_auth._inspect(SIGNED_IN_PAGE)
    assert signed is True and gated is False


def test_login_form_is_found_without_assuming_a_plugin():
    woo = """<html><form class="woocommerce-form-login" action="/my-account/" method="post">
        <input type="hidden" name="woocommerce-login-nonce" value="abc123">
        <input type="text" name="username"><input type="password" name="password"></form></html>"""
    action, hidden, user, pw = vsin_auth._login_form(woo, "https://vsin.com/my-account/")
    assert action == "https://vsin.com/my-account/"
    assert hidden == {"woocommerce-login-nonce": "abc123"}
    assert (user, pw) == ("username", "password")

    custom = """<html><form action="https://vsin.com/login" method="post">
        <input type="email" name="user_email"><input type="password" name="user_pass"></form></html>"""
    action, hidden, user, pw = vsin_auth._login_form(custom, "https://vsin.com/my-account/")
    assert (user, pw) == ("user_email", "user_pass")


def test_login_form_missing_is_an_error_not_a_guess():
    with pytest.raises(vsin_auth.AuthFailed):
        vsin_auth._login_form("<html><form><input name='q'></form></html>", "https://vsin.com/")


# ---- page scope -------------------------------------------------------------------------------
@dataclass
class FakeRes:
    payload: str
    retrieved_at: datetime = None

    def __post_init__(self):
        self.retrieved_at = self.retrieved_at or datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class FakeRM:
    """Only rm.get() is used by fetch_best_scope; this records which pages were actually requested."""

    def __init__(self, by_params: dict):
        self.by_params = by_params
        self.calls: list[dict] = []

    def get(self, url, params=None, **kw):
        self.calls.append(dict(params or {}))
        key = "week" if (params or {}).get("view") == "week" else "day"
        return FakeRes(self.by_params[key])


def test_date_headers_counts_distinct_game_days():
    assert vsin.date_headers(GATED_PAGE) == ["Monday, Oct 5"]
    many = vsin.date_headers(FIX.read_text())
    assert len(many) >= 2, many


def test_scope_prefers_the_page_that_carries_more_games():
    rm = FakeRM({"week": SIGNED_IN_PAGE, "day": GATED_PAGE})
    html, ts, label, notes = vsin.fetch_best_scope(rm, "NFL", minimum=4)
    assert label == "splits week"
    rows, _ = vsin.parse(html)
    assert len(rows) >= 4
    # it stopped once the week page was good enough, rather than spending a second request
    assert len(rm.calls) == 1


def test_a_remembered_page_that_shrinks_triggers_a_resurvey():
    rm = FakeRM({"week": SIGNED_IN_PAGE, "day": GATED_PAGE})
    vsin.fetch_best_scope(rm, "NFL", minimum=4)
    assert vsin.SCOPE_CACHE.exists()
    # the week page now returns the gated two-row card: the cache must not be trusted into writing it
    rm2 = FakeRM({"week": GATED_PAGE, "day": GATED_PAGE})
    html, ts, label, notes = vsin.fetch_best_scope(rm2, "NFL", minimum=4)
    assert any("re-surveying" in n.get("why", "") for n in notes), notes
    assert len(vsin.parse(html)[0]) == 2          # reported as thin, not dressed up


# ---- linetracker ------------------------------------------------------------------------------
def test_books_are_read_out_of_the_observed_linetracker_header():
    header = ["Mon, Oct 5", "Circa", "Boomers", "BetMGM", "Caesars"]
    assert vsin._books_from_header(header) == {1: "circa", 2: "boomers", 3: "betmgm", 4: "caesars"}


# The real signed-in page, 2026-10-12: eight books split across TWO header rows, each followed by a
# SPR/ML/TOT sub-header, and a single flat 25-cell data row carrying all eight.
LT_BOOKS_A = ["Circa", "Boomers", "BetMGM", "Caesars"]
LT_BOOKS_B = ["Westgate", "Stations", "South Point", "Wynn"]
LT_SUB = [""] + ["SPR", "ML", "TOT"] * 4


def _tr(cells):
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _lt_row(label, per_book):
    return _tr([label] + [c for book in per_book for c in book])


def _lt_html():
    open_cells = [["+2 -110", "+120", "53"]] * 4 + [["+2.5 -105", "+125", "53.5"]] * 4
    buf = [["+3.5 -115", "+155", "54.5 o -110"]] * 8
    nyj = [["-3.5 -105", "-180", "54.5 u -110"]] * 8
    return ("<table>"
            + _tr(["Mon, Oct 12"] + LT_BOOKS_A) + _tr(LT_SUB)
            + _tr(["Mon, Oct 12"] + LT_BOOKS_B) + _tr(LT_SUB)
            + _lt_row("▶ 8:15 PM ET ET OPEN", open_cells)
            + _lt_row("Buffalo Bills", buf)
            + _lt_row("New York Jets", nyj)
            + "</table>")


def test_layout_is_derived_from_the_pages_own_two_tier_header():
    """
    Eight books over two header rows, 24 market columns, in page order. Deriving this rather than
    hardcoding it means a book added or reordered re-maps automatically instead of shifting every
    price one column sideways.
    """
    from bs4 import BeautifulSoup
    trs = BeautifulSoup(_lt_html(), "html.parser").find_all("tr")
    cols, probs = vsin._linetracker_layout(trs)
    assert probs == []
    assert len(cols) == 24
    assert cols[:3] == [("circa", "spread"), ("circa", "moneyline"), ("circa", "total")]
    assert cols[12:15] == [("westgate", "spread"), ("westgate", "moneyline"), ("westgate", "total")]
    assert cols[-1] == ("wynn", "total")


@pytest.mark.parametrize("cell,expect", [
    ("+3.5 -115", {"spread": 3.5, "spread_price": -115}),
    ("-3.5 -120", {"spread": -3.5, "spread_price": -120}),
    ("+2 -110", {"spread": 2.0, "spread_price": -110}),
    ("PK -105", {"spread": 0.0, "spread_price": -105}),
    ("", {}),
    ("--", {}),
])
def test_spread_cells(cell, expect):
    assert vsin._spr_cell(cell) == expect


@pytest.mark.parametrize("cell,expect", [
    ("+155", {"ml_price": 155}), ("-180", {"ml_price": -180}), ("+120", {"ml_price": 120}),
    ("", {}), ("--", {}),
    ("-110", {"ml_price": -110}),
])
def test_moneyline_cells(cell, expect):
    assert vsin._ml_cell(cell) == expect


@pytest.mark.parametrize("cell,expect", [
    ("54.5 o -110", {"total": 54.5, "over_price": -110}),
    ("54.5 u -110", {"total": 54.5, "under_price": -110}),
    ("53", {"total": 53.0}),
    ("53.5", {"total": 53.5}),
    ("", {}),
])
def test_total_cells(cell, expect):
    assert vsin._tot_cell(cell) == expect


def test_a_moneyline_cannot_overwrite_the_spreads_vig():
    """
    Both columns ended in an American price and both wrote "price", so the moneyline clobbered the
    spread's vig for every book -- half the pricing gone, silently. The keys are market-namespaced.
    """
    rows, probs = vsin.parse_linetracker(_lt_html())
    assert probs == []
    game = vsin.linetracker_games(rows)[0]
    buf = dict(game["teams"])["Buffalo Bills"]["circa"]
    assert buf == {"spread": 3.5, "spread_price": -115, "ml_price": 155,
                   "total": 54.5, "over_price": -110}


def test_the_open_row_is_kept_apart_from_the_team_rows():
    """'OPEN' is the game's opening number across every book -- the other end of closing-line value."""
    rows, _ = vsin.parse_linetracker(_lt_html())
    games = vsin.linetracker_games(rows)
    assert len(games) == 1
    g = games[0]
    assert g["n_teams"] == 2
    assert "OPEN" in g["open_label"]
    assert g["open"]["circa"]["spread"] == 2.0          # opened +2
    assert dict(g["teams"])["Buffalo Bills"]["circa"]["spread"] == 3.5   # now +3.5
    assert g["open"]["wynn"]["ml_price"] == 125
    assert set(g["open"]) == {"circa", "boomers", "betmgm", "caesars",
                              "westgate", "stations", "southpoint", "wynn"}


def test_paywall_markup_alone_does_not_condemn_a_good_page():
    """
    inline-lock / template-shown sit on this page even when signed in with a full week of prices
    showing. Treating them as a gate threw away a working page, so the content is judged instead.
    """
    html = '<div id="inline-lock" class="template-shown"></div>' + _lt_html()
    rows, probs = vsin.parse_linetracker(html)
    assert len(rows) == 72                          # 3 rows x 8 books x 3 markets
    assert not any(p.get("kind") == "gated" for p in probs)


def test_a_row_whose_cell_count_disagrees_with_the_header_is_skipped():
    """A shifted row misprices every book on it, so it is refused, not best-guessed."""
    short = _lt_row("Short Row", [["+3 -110", "+140", "50"]] * 7)      # 22 cells, not 25
    html = _lt_html().replace("</table>", short + "</table>")
    rows, _ = vsin.parse_linetracker(html)
    assert not any(r["label"] == "Short Row" for r in rows)


def test_no_prices_at_all_is_reported_not_returned_empty_and_silent():
    rows, probs = vsin.parse_linetracker("<table><tr><td>nothing here</td></tr></table>")
    assert rows == []
    assert any(p.get("kind") == "no_prices" or "layout has changed" in p.get("why", "") for p in probs)


# ---- the behaviour that matters ---------------------------------------------------------------
def test_a_configured_but_expired_session_writes_nothing(monkeypatch, tmp_path):
    """
    A stale cookie must not be allowed to collect today's one game and call it a slate. This is the
    exact outage: 1 of 16 games, 95 snapshots, a green job, and four days lost.
    """
    monkeypatch.setattr(config, "VSIN_COOKIE", "wordpress_logged_in_9f=expired")

    state = vsin_auth.AuthState(mode="cookie", signed_in=False, gated=True, detail="2 cookie(s)")

    def fake_session(job_run_id):
        return FakeRM({"week": GATED_PAGE, "day": GATED_PAGE}), state

    monkeypatch.setattr(ingest_splits, "vsin_session", fake_session)
    games = pd.DataFrame([{"game_id": "2026_NFL_W05_ATL_NO", "away_team_id": "NFL_ATL",
                           "home_team_id": "NFL_NO", "week": 5, "status": "SCHEDULED",
                           "kickoff_utc": "2026-10-06T00:15:00Z", "season_type": "REG"}])
    vlog = ValidationLog("TEST", "betting_splits")
    recs, problems = ingest_splits.read_vsin("NFL", 2026, ids.AliasResolver.load(), games, vlog)
    assert recs == []
    assert any(p.get("kind") == "not_signed_in" for p in problems)


def test_a_signed_in_session_does_reach_the_parser(monkeypatch):
    """
    The control for the test above. Without this, 'writes nothing' could be passing because the guard
    rejects everything, which would be a worse bug than the one it fixes.
    """
    monkeypatch.setattr(config, "VSIN_COOKIE", "wordpress_logged_in_9f=fresh")
    monkeypatch.setattr(config, "SPLITS_MIN_TEAM_ROWS", {"NFL": 4})
    state = vsin_auth.AuthState(mode="cookie", signed_in=True, gated=False, detail="2 cookie(s)")
    monkeypatch.setattr(ingest_splits, "vsin_session",
                        lambda job: (FakeRM({"week": SIGNED_IN_PAGE, "day": SIGNED_IN_PAGE}), state))
    tp = config.TABLES / "ref" / "teams.parquet"
    tp.parent.mkdir(parents=True, exist_ok=True)
    _teams().to_parquet(tp)
    games = pd.DataFrame([
        {"game_id": "2026_NFL_W01_NE_SEA", "away_team_id": "NFL_NE", "home_team_id": "NFL_SEA",
         "week": 1, "status": "SCHEDULED", "kickoff_utc": "2026-12-10T00:20:00Z", "season_type": "REG"}])
    vlog = ValidationLog("TEST", "betting_splits")
    recs, problems = ingest_splits.read_vsin("NFL", 2026, ids.AliasResolver.load(), games, vlog)
    assert not any(p.get("kind") == "not_signed_in" for p in problems)
    assert any(r["game_id"] == "2026_NFL_W01_NE_SEA" for r in recs), (recs, problems[:4])


def _teams():
    return pd.DataFrame([
        {"team_id": "NFL_NE", "league": "NFL", "abbr": "NE", "school_or_city": "New England",
         "mascot": "Patriots", "display_name": "New England Patriots"},
        {"team_id": "NFL_SEA", "league": "NFL", "abbr": "SEA", "school_or_city": "Seattle",
         "mascot": "Seahawks", "display_name": "Seattle Seahawks"},
    ])


def test_an_anonymous_session_is_still_allowed_to_read_the_public_page(monkeypatch):
    """No credentials configured is not an error -- it is the old behaviour, with the min-row guard."""
    state = vsin_auth.AuthState(mode="anonymous", signed_in=False, gated=True)
    assert state.configured is False
    assert "anonymous" in state.line()
    assert "VSIN_COOKIE" in vsin_auth.describe(state)


def test_signed_in_state_says_so_plainly():
    state = vsin_auth.AuthState(mode="cookie", signed_in=True, gated=False, detail="3 cookie(s)")
    line = state.line()
    assert "signed in" in line and "NOT" not in line


# ---- closing-line value: the close must actually be a close -----------------------------------
def _snap(game, book, when, spread):
    return {"snapshot_id": f"{game}_{book}_{when}", "game_id": game, "retrieved_at": when,
            "book": book, "spread_home": spread, "total": 44.5, "ml_home": -150, "ml_away": 130}


@dataclass
class FakePick:
    market: str = "SPREAD"
    side_is_home: bool = True
    line: float = -2.5
    side: str = "home"
    price: float = -110


def _close(tmp_path, monkeypatch, rows):
    from pipeline.jobs import build_picks
    p = config.TABLES / "market" / "snapshots" / "NFL" / "2026" / "W05.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(p, index=False)
    return build_picks


def test_a_four_day_old_number_is_not_treated_as_the_close(tmp_path, monkeypatch):
    """The outage's signature: the newest snapshot is days old. CLV must report, not score."""
    bp = _close(tmp_path, monkeypatch, [_snap("G1", "draftkings", "2026-10-01T12:00:00Z", -3.0)])
    close = bp.closing_number("NFL", 2026, 5, "G1", pd.Timestamp("2026-10-05T17:00:00Z"), {})
    assert close["is_close"] is False
    assert close["stale_minutes"] > 4 * 24 * 60 - 60
    out = bp.closing_line_value(FakePick(), close)
    assert out["clv_points"] is None and out["beat_close"] is None
    assert "closing window" in out["close_note"]


def test_a_number_inside_the_window_is_scored(tmp_path, monkeypatch):
    bp = _close(tmp_path, monkeypatch, [_snap("G2", "draftkings", "2026-10-05T15:30:00Z", -3.0)])
    close = bp.closing_number("NFL", 2026, 5, "G2", pd.Timestamp("2026-10-05T17:00:00Z"), {})
    assert close["is_close"] is True and close["stale_minutes"] == 90
    out = bp.closing_line_value(FakePick(line=-2.5), close)
    # took -2.5, closed -3.0: half a point better than the close
    assert out["clv_points"] == 0.5 and out["beat_close"] is True
    assert out["close_book"] == "draftkings"


def test_the_sharpest_book_on_record_supplies_the_close(tmp_path, monkeypatch):
    bp = _close(tmp_path, monkeypatch, [
        _snap("G3", "draftkings", "2026-10-05T16:50:00Z", -3.0),
        _snap("G3", "circa", "2026-10-05T16:40:00Z", -2.0),
        _snap("G3", "caesars", "2026-10-05T16:55:00Z", -3.5)])
    close = bp.closing_number("NFL", 2026, 5, "G3", pd.Timestamp("2026-10-05T17:00:00Z"), {})
    assert close["book"] == "circa" and close["books_seen"] == 3
    # against Circa's -2.0, taking -2.5 is HALF A POINT WORSE, not better
    out = bp.closing_line_value(FakePick(line=-2.5), close)
    assert out["clv_points"] == -0.5 and out["beat_close"] is False


def test_the_odds_page_can_report_an_anonymous_collector(tmp_path, monkeypatch):
    """
    Weeks of near-empty splits with no explanation anywhere on the page is what made this outage so
    slow to diagnose. The session state recorded by each pull has to reach the payload.
    """
    import importlib
    from pipeline import ingest_health, splits_engine as se
    from pipeline.jobs import build_site as bs
    importlib.reload(bs)
    monkeypatch.setattr(se, "SPLITS", config.TABLES / "market" / "splits")
    monkeypatch.setattr(ingest_health, "HEARTBEAT", config.TABLES / "ops" / "ingest_heartbeat.csv")
    ingest_health.record("splits", "NFL", 2026, 1, 1, note="1 upcoming games covered; vsin_auth=anonymous/anonymous")

    class S:
        league, season = "NFL", 2026
    f = bs._splits_freshness(S(), 5)
    assert f["splits_session"] == "anonymous/anonymous"
    importlib.reload(bs)
