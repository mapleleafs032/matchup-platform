"""
Signed-in VSiN session (paid subscription).

Why this exists
---------------
VSiN's public splits page shows only the CURRENT DAY's card, wrapped in a paywall teaser
(`inline-lock` / `template-shown` markup). Measured on 2026-10-05: 2 team rows for NFL, 0 for the
week view, 0 on the linetracker. The parser was never broken -- the page simply does not contain the
week unless you are signed in. A VSiN Pro subscription includes "DraftKings & Circa betting splits
updated in real time", so the fix is to present the subscriber's own session, not to defeat the gate.

Two credential modes, in priority order:

  VSIN_COOKIE                 the Cookie header copied from a signed-in browser
                              ("wordpress_logged_in_abc=...; other=...")
                              Simplest and survives CAPTCHA/SSO on the login form. Expires, so the
                              job reports when it stops working instead of writing a partial slate.

  VSIN_EMAIL + VSIN_PASSWORD  log in through the account form each run and keep the session cookies.
                              Self-renewing; breaks if the form gains a CAPTCHA.

Both come from environment variables (GitHub Actions secrets). Nothing is ever written to the repo:
no cookie jar is persisted, and every log line goes through redact().

The hard rule here: a session that is NOT signed in must not be allowed to write a partial slate.
That is the exact failure that looked like a working job for four days -- one game, 95 snapshots,
everything downstream quietly starving. So auth state is measured on every run, reported, and when
credentials are configured but the session is anonymous the job fails loudly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests

import config

# A real browser's UA. The purpose is not disguise -- the session is a paying subscriber's own, and
# the identifying project string stays in the X-Client header below. Some WordPress membership
# plugins simply refuse to set a login cookie for a non-browser UA, which would make a legitimate
# sign-in fail for no reason.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
CLIENT_NOTE = "matchup-platform/1.0 (personal football research; subscriber session)"

# Markup that says the paywall teaser is still in force on a fetched page.
GATE_MARKERS = ("inline-lock", "template-shown")
# Markup that says the response was served to a signed-in account.
SIGNED_IN_MARKERS = ("action=logout", "logged-in", "my-account/customer-logout",
                     "wp-admin-bar-logout", "Sign Out", "Log Out", "logout_vsin")
# Markup that says the response was served to nobody in particular.
ANON_MARKERS = ("Sign In", "Subscribe Now", "my-account/?action=login")

_COOKIE_SPLIT = re.compile(r";\s*")
_SECRETISH = re.compile(r"(?i)(cookie|password|passwd|token|secret|nonce|session)")


class AuthNotConfigured(RuntimeError):
    """No VSiN credentials in the environment. Not an error on its own -- the public page still works."""


class AuthFailed(RuntimeError):
    """Credentials are present but the session did not come back signed in."""


@dataclass
class AuthState:
    mode: str                       # "cookie" | "login" | "anonymous"
    signed_in: bool
    gated: bool
    detail: str = ""
    cookie_names: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return self.mode != "anonymous"

    def line(self) -> str:
        if self.mode == "anonymous":
            return "VSiN session: anonymous (no subscription credentials configured) — public page shows only today's card"
        who = "signed in" if self.signed_in else "NOT signed in"
        gate = "paywall teaser still present" if self.gated else "no paywall markup"
        return f"VSiN session: {self.mode} auth, {who}, {gate}{(' — ' + self.detail) if self.detail else ''}"


def redact(text: str) -> str:
    """Blank anything that looks like a credential before it reaches a log or a job message."""
    out = str(text)
    for val in (config.VSIN_COOKIE, config.VSIN_PASSWORD, config.VSIN_EMAIL):
        if val and len(val) > 3:
            out = out.replace(val, "<redacted>")
    # also mask k=v pairs whose key looks secret, in case a value arrives from elsewhere
    return re.sub(r"(?i)\b([a-z0-9_\-]*(?:cookie|password|token|secret|nonce|session)[a-z0-9_\-]*)=([^;,\s]{4,})",
                  r"\1=<redacted>", out)


def credentials_present() -> tuple[bool, str]:
    if config.VSIN_COOKIE:
        return True, "cookie"
    if config.VSIN_EMAIL and config.VSIN_PASSWORD:
        return True, "login"
    return False, "anonymous"


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": BROWSER_UA,
        "X-Client": CLIENT_NOTE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _apply_cookie_header(s: requests.Session, header: str) -> list[str]:
    """Load a browser Cookie header string into the jar for every vsin.com host."""
    names = []
    for part in _COOKIE_SPLIT.split(header.strip().lstrip("Cookie:").strip()):
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        # set on the parent domain so it is sent to vsin.com and data.vsin.com alike
        s.cookies.set(name, value, domain=".vsin.com", path="/")
        names.append(name)
    return names


def _login_form(html: str, page_url: str) -> tuple[str, dict, str, str]:
    """
    Find the login form without assuming a plugin.

    Returns (action_url, hidden_fields, username_field, password_field). Field names are read from the
    form that actually contains a password input, so a WooCommerce form ('username'/'password') and a
    custom one ('log'/'pwd', 'email') both work without a special case.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        pw = form.find("input", attrs={"type": "password"})
        if not pw or not pw.get("name"):
            continue
        user = None
        for inp in form.find_all("input"):
            t = (inp.get("type") or "text").lower()
            if t in ("text", "email") and inp.get("name"):
                user = inp["name"]
                break
        if not user:
            continue
        hidden = {i["name"]: i.get("value", "") for i in form.find_all("input", attrs={"type": "hidden"})
                  if i.get("name")}
        action = form.get("action") or page_url
        if action.startswith("/"):
            action = "https://vsin.com" + action
        elif not action.startswith("http"):
            action = page_url
        return action, hidden, user, pw["name"]
    raise AuthFailed("no form with a password field was found on the account page; "
                     "the login page has changed, or it is rendered in the browser")


def _inspect(html: str) -> tuple[bool, bool]:
    """(signed_in, gated) read from page markup."""
    gated = any(m in html for m in GATE_MARKERS)
    signed = any(m in html for m in SIGNED_IN_MARKERS)
    return signed, gated


def build_session(verify: bool = True) -> tuple[requests.Session, AuthState]:
    """
    A requests.Session carrying the subscriber's VSiN credentials, plus what state it is actually in.

    verify=True spends one extra request confirming the session is signed in. That request is the
    whole point: without it a stale cookie reads as a successful run that collected one game.
    """
    present, mode = credentials_present()
    s = _new_session()
    if not present:
        return s, AuthState(mode="anonymous", signed_in=False, gated=True)

    names: list[str] = []
    detail = ""
    if mode == "cookie":
        names = _apply_cookie_header(s, config.VSIN_COOKIE)
        if not names:
            raise AuthFailed("VSIN_COOKIE is set but no name=value pairs could be read from it. "
                             "Paste the whole Cookie request header from a signed-in browser tab.")
        detail = f"{len(names)} cookie(s): {', '.join(sorted(names)[:6])}"
    else:
        url = config.VSIN_AUTH["login_url"]
        try:
            page = s.get(url, timeout=45)
            action, hidden, user_field, pw_field = _login_form(page.text, url)
            data = dict(hidden)
            data[user_field] = config.VSIN_EMAIL
            data[pw_field] = config.VSIN_PASSWORD
            data.setdefault("login", "Log in")
            data.setdefault("rememberme", "forever")
            resp = s.post(action, data=data, timeout=45,
                          headers={"Referer": url, "Origin": "https://vsin.com"})
        except requests.RequestException as e:
            raise AuthFailed(f"could not reach the VSiN login page: {redact(e)}") from None
        got = sorted(c.name for c in s.cookies)
        names = got
        detail = f"posted to {action.split('?')[0]}, HTTP {resp.status_code}, {len(got)} cookie(s)"
        if not any("logged_in" in n or "wordpress_sec" in n or "vsin" in n.lower() for n in got):
            raise AuthFailed(
                "the login POST returned no session cookie. The email/password may be wrong, or the "
                "form now needs a CAPTCHA. Use VSIN_COOKIE instead: sign in with a browser, copy the "
                "Cookie request header, and store it as the VSIN_COOKIE secret. " + redact(detail))

    state = AuthState(mode=mode, signed_in=False, gated=True, detail=detail, cookie_names=names)
    if not verify:
        return s, state

    try:
        r = s.get(config.VSIN_AUTH["verify_url"], params={"sport": "NFL", "source": "DK"}, timeout=45)
        html = r.text
    except requests.RequestException as e:
        state.detail = f"{detail}; verification request failed: {redact(e)}"
        return s, state
    state.signed_in, state.gated = _inspect(html)
    state.detail = f"{detail}; verify page {len(html)} chars"
    return s, state


def describe(state: AuthState, html: str | None = None) -> str:
    """A diagnostic block for `--explain auth`: what the session is, and what the gate did to the page."""
    out = [state.line()]
    if state.cookie_names:
        out.append(f"cookie names presented: {', '.join(sorted(state.cookie_names))}")
    if html is not None:
        signed, gated = _inspect(html)
        out.append(f"verify page: {len(html)} chars; signed-in markers {'found' if signed else 'absent'}; "
                   f"gate markers {'present' if gated else 'absent'}")
        hits = [m for m in GATE_MARKERS + SIGNED_IN_MARKERS + ANON_MARKERS if m in html]
        out.append(f"markers seen: {', '.join(hits) if hits else 'none'}")
    if not state.configured:
        out.append("Set the VSIN_COOKIE secret (or VSIN_EMAIL + VSIN_PASSWORD) to use the subscription.")
    elif not state.signed_in:
        out.append("Credentials are configured but the page did not come back signed in — the cookie has "
                   "most likely expired. Sign in again in a browser and replace the VSIN_COOKIE secret.")
    return "\n    ".join(redact(x) for x in out)
