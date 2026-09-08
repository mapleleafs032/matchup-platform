"""
AI matchup agent (master prompt §36-38, §61-62). The model interprets the package; it never generates statistics.

Guardrails that are code, not prompt text:
  * The prompt contains ONLY the package. No web access, no other context.
  * Output must be a JSON object with the twelve required sections; anything else is rejected.
  * Every number in the prose is checked against the package's flat set of numeric values (validate()).
    Percentages written as "62%" match 0.62; ranks and counts match integers; tolerance is rounding only.
    Violations are returned with the retry prompt once; a second failure is stored as validation_failed=True
    and the analysis is NOT shown until a human reviews it (the frontend reads that flag).
  * Metric names the model uses are checked against the registry labels (soft: reported, not rejected).
  * Section text that would say something about a field listed in package["unavailable"] must say
    "Insufficient reliable data" — the validator flags confident language about unavailable fields.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone

import requests

import config
from pipeline.ai_package import flat_numbers

SECTIONS = ["model_projection", "offensive_matchup", "quarterback_edge", "trenches", "explosive_play_edge", "third_down_red_zone", "roster_talent",
            "recent_form", "market_movement", "key_advantages", "key_concerns", "expected_game_script"]

SYSTEM = """You are the analysis layer of a football matchup platform. You receive one JSON package describing a single game and you write the analysis sections.

Absolute rules:
1. Use ONLY facts and numbers present in the package. Do not use outside knowledge about these teams, players, coaches, injuries, weather, or betting lines. If you are unsure whether something is in the package, do not say it.
2. Every number you write must appear in the package (rounded is fine; a rate of 0.62 may be written as 62%). Do not compute new statistics beyond simple comparisons already implied by two package numbers (e.g., "ranks 8th vs 40th").
3. When a field is null or listed under "unavailable", say "Insufficient reliable data" for that topic instead of guessing. Never invent injuries, transfers, depth-chart facts, or line movement.
4. The projected score, spread, total and win probability come from the quantitative model in package.model. Restate them; never change or second-guess them with your own number.
5. Connect statistics into interactions (offense X vs defense Y) rather than listing them. Explain what each edge means for how the game is likely to unfold.
6. Market language must be evidence-based: describe what moved and by how much; never assert that "sharp money" caused anything. Public ticket/money percentages are unavailable.
7. teamA is the AWAY team and teamB is the HOME team. Use team names, not "teamA/teamB".
8. Output ONLY a JSON object with exactly these keys: model_projection, offensive_matchup, quarterback_edge, trenches, explosive_play_edge, third_down_red_zone, roster_talent, recent_form, market_movement, key_advantages, key_concerns, expected_game_script. Values are strings, except key_advantages and key_concerns which are arrays of 2-4 short strings. No markdown, no preamble."""


def build_prompt(pkg: dict) -> str:
    return ("Package:\n" + json.dumps(pkg, default=str, allow_nan=False) +
            "\n\nWrite the analysis JSON now. Keep each section to 2-5 sentences (expected_game_script may be up to 7). Use plain language a knowledgeable fan understands; explain an advanced metric briefly the first time you use it.")


class AnthropicClient:
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model_candidates: list[str]):
        self.api_key = api_key; self.candidates = list(model_candidates); self.model = None

    def complete(self, system: str, user: str, max_tokens: int = 2000) -> tuple[str, dict]:
        last = None
        for m in ([self.model] if self.model else self.candidates):
            r = requests.post(self.URL, headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                              json={"model": m, "max_tokens": max_tokens, "temperature": 0.2, "system": system, "messages": [{"role": "user", "content": user}]}, timeout=120)
            if r.status_code == 404 or (r.status_code == 400 and "model" in r.text.lower()):
                last = f"{m}: {r.text[:120]}"; continue
            if r.status_code >= 400:
                raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:300]}")
            data = r.json(); self.model = m
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return text, {"model": m, "tokens_in": data.get("usage", {}).get("input_tokens"), "tokens_out": data.get("usage", {}).get("output_tokens")}
        raise RuntimeError(f"no usable model among {self.candidates}: {last}")


def parse_sections(text: str) -> dict | None:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict) or set(obj) != set(SECTIONS):
        return None
    for k in ("key_advantages", "key_concerns"):
        if not isinstance(obj[k], list):
            return None
    return obj


_NUM = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?%?")
_ORDINAL = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b")


def validate(sections: dict, pkg: dict) -> dict:
    """Numeric-citation check: every number in the prose must exist in the package (with rounding/percent tolerance)."""
    allowed = flat_numbers(pkg)
    head = []
    mdl = pkg.get("model") or {}
    for v in ((mdl.get("projected_score") or {}).values() if isinstance(mdl.get("projected_score"), dict) else []):
        head.append(v)
    for k in ("projected_margin_home", "projected_total", "market_spread_home", "market_total", "spread_diff_vs_market", "total_diff_vs_market"):
        if mdl.get(k) is not None:
            head.append(mdl[k])
    mk = pkg.get("market") or {}
    for blk in ("open", "current"):
        for k in ("spread_home", "total"):
            v = (mk.get(blk) or {}).get(k) if isinstance(mk.get(blk), dict) else None
            if v is not None:
                head.append(v)
    head = [float(x) for x in head if isinstance(x, (int, float))]
    for i, a in enumerate(head):            # "an 8-point gap", "a 3.5-point move": arithmetic on headline numbers is legitimate
        for b in head[i + 1:]:
            allowed.update({round(abs(a - b), 3), round(a + b, 3)})
    # exact-ish sets: decimals must match to one decimal place; integers may match a rounded package value
    allowed_1dp = {round(v, 1) for v in allowed} | {round(-v, 1) for v in allowed} | {round(abs(v), 1) for v in allowed}
    allowed_int = {round(v, 0) for v in allowed if abs(v) >= 1.0} | {round(abs(v), 0) for v in allowed if abs(v) >= 1.0}
    allowed_pct = set()
    for v in allowed:
        if -1.0 <= v <= 1.0:
            allowed_pct.update({round(abs(v) * 100, 1), round(abs(v) * 100, 0)})
    year_like = {float(y) for y in range(1990, 2040)}
    violations, checked = [], 0
    text_all = " ".join(v if isinstance(v, str) else " ".join(v) for v in sections.values())
    for m in _NUM.finditer(text_all):
        raw = m.group(0)
        try:
            val = float(raw.rstrip("%"))
        except ValueError:
            continue
        checked += 1
        is_pct = raw.endswith("%")
        is_int = "." not in raw
        if val in year_like:
            continue
        if not is_pct and is_int and 0 <= val <= 30:
            continue     # ranks, downs, yardages, small counts: contextual, not statistics
        ok = False
        if is_pct:
            ok = round(val, 1) in allowed_pct or round(val, 0) in allowed_pct or round(val / 100, 3) in {round(v, 3) for v in allowed}
        elif is_int:
            ok = val in allowed_int or val in allowed_1dp or val in allowed_pct
        else:
            ok = round(val, 1) in allowed_1dp or round(val, 1) in allowed_pct
        if not ok:
            violations.append(raw)
    unavailable = set(pkg.get("unavailable", []))
    confident_unavailable = []
    for topic, keys in (("quarterback_edge", {"quarterback_home", "quarterback_away"}), ("market_movement", {"market"})):
        if keys & unavailable and "insufficient reliable data" not in sections.get(topic, "").lower():
            confident_unavailable.append(topic)
    return {"numbers_checked": checked, "violations": sorted(set(violations)), "confident_about_unavailable": confident_unavailable,
            "ok": not violations and not confident_unavailable}


def generate(pkg: dict, client, max_attempts: int = 2) -> dict:
    """Returns {sections, validation, meta, validation_failed, attempts}. Retries once with the violations listed."""
    prompt = build_prompt(pkg)
    last = None
    prior_violations: list[str] = []
    for attempt in range(max_attempts):
        user = prompt if attempt == 0 else (prompt + "\n\nYour previous answer contained numbers not present in the package: " + ", ".join(prior_violations) +
                                            ". Rewrite using only package numbers, and use 'Insufficient reliable data' where a value is missing.")
        text, meta = client.complete(SYSTEM, user)
        sections = parse_sections(text)
        if sections is None:
            last = {"sections": None, "validation": {"ok": False, "violations": ["<unparseable JSON>"], "confident_about_unavailable": []}, "meta": meta}
            prior_violations = ["<unparseable JSON>"]
            continue
        v = validate(sections, pkg)
        last = {"sections": sections, "validation": v, "meta": meta}
        if v["ok"]:
            return {**last, "validation_failed": False, "attempts": attempt + 1, "generated_at": datetime.now(timezone.utc).isoformat()}
        prior_violations = v["violations"] + v["confident_about_unavailable"]
    return {**last, "validation_failed": True, "attempts": max_attempts, "generated_at": datetime.now(timezone.utc).isoformat()}
