"""
python -m pipeline.jobs.build_ai --league NFL             # current + next week, hash-gated
Writes data/tables/model/ai_analyses/{league}/{season}/{game_id}_{hash}.json  (APPEND-ONLY: one file per inputs hash)
and appends an index row to data/tables/model/ai_analyses_index.csv.
Regeneration happens only when the package hash changes (§48). Requires ANTHROPIC_API_KEY.
"""
from __future__ import annotations
import argparse
import json

import pandas as pd

import config
from pipeline import ai_agent, ai_package, storage
from pipeline.log import JobRun

AI_DIR = config.TABLES / "model" / "ai_analyses"
INDEX = config.TABLES / "model" / "ai_analyses_index.csv"


def _write(league, season, wk, g, pkg, out):
    aid = f"{g.game_id}_{pkg['inputs_hash']}"
    (AI_DIR / league / str(season)).mkdir(parents=True, exist_ok=True)
    (AI_DIR / league / str(season) / f"{aid}.json").write_text(json.dumps({"analysis_id": aid, "game_id": g.game_id, "inputs_hash": pkg["inputs_hash"],
        "model_version": pkg["model"]["model_version"], "llm_model": out["meta"].get("model"), "prompt_version": config.AI_PROMPT_VERSION,
        "sections": out["sections"], "validation": out["validation"], "validation_failed": out["validation_failed"], "attempts": out["attempts"],
        "generated_at": out["generated_at"], "tokens_in": out["meta"].get("tokens_in"), "tokens_out": out["meta"].get("tokens_out"),
        "stop_reason": out["meta"].get("stop_reason"), "raw_text": out.get("raw_text")}, default=str, indent=0))
    storage.append_csv(INDEX, pd.DataFrame([{"analysis_id": aid, "game_id": g.game_id, "league": league, "season": season, "week": wk, "inputs_hash": pkg["inputs_hash"],
                                             "llm_model": out["meta"].get("model"), "validation_failed": out["validation_failed"], "violations": ",".join(out["validation"].get("violations", [])),
                                             "tokens_in": out["meta"].get("tokens_in"), "tokens_out": out["meta"].get("tokens_out"), "generated_at": out["generated_at"]}]),
                       ["analysis_id"], on_duplicate="skip")


def run(league: str, season: int, weeks: list[int], job: JobRun, client=None, limit: int | None = None, workers: int = 4) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if client is None:
        if not config.ANTHROPIC_API_KEY:
            job.status = "SKIPPED"; job.message = "ANTHROPIC_API_KEY not set"; return
        client = ai_agent.AnthropicClient(config.ANTHROPIC_API_KEY, config.AI_MODEL_CANDIDATES)
    games = storage.read_table(storage.games_path(league, season))
    idx = storage.read_table(INDEX)
    done = set(idx[~idx.validation_failed.astype(bool)].analysis_id) if not idx.empty else set()   # failed analyses are retried
    now = pd.Timestamp.now(tz="UTC")
    budget = limit or config.AI_MAX_GAMES_PER_RUN
    todo = []
    n_skip = 0
    for wk in weeks:
        wk_games = games[(games.week == wk) & (games.status == "SCHEDULED") & (pd.to_datetime(games.kickoff_utc, utc=True) > now)]
        wk_games = wk_games[~wk_games.home_team_id.str.startswith("CFB_FCS") & ~wk_games.away_team_id.str.startswith("CFB_FCS")]
        for _, g in wk_games.iterrows():
            pkg = ai_package.build_package(league, season, wk, g.game_id)
            if pkg is None or pkg.get("model") is None:
                continue                       # no prediction yet -> nothing to interpret
            if f"{g.game_id}_{pkg['inputs_hash']}" in done:
                n_skip += 1; continue
            todo.append((wk, g, pkg))
    todo = todo[:budget]
    # the first call also settles which model the account can use; run it alone, then fan out
    n_gen = n_fail = 0
    violations: dict[str, int] = {}
    if todo:
        wk, g, pkg = todo[0]
        out = ai_agent.generate(pkg, client); _write(league, season, wk, g, pkg, out); n_gen += 1; n_fail += int(out["validation_failed"])
        for v in out["validation"].get("violations", []):
            violations[v] = violations.get(v, 0) + 1
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(ai_agent.generate, pkg, client): (wk, g, pkg) for wk, g, pkg in todo[1:]}
            for f in as_completed(futs):
                wk, g, pkg = futs[f]
                try:
                    out = f.result()
                except Exception as e:      # one failed request never kills the run; the game is retried next time
                    print(f"  {g.game_id}: request failed: {str(e)[:160]}"); continue
                _write(league, season, wk, g, pkg, out); n_gen += 1; n_fail += int(out["validation_failed"])
                for v in out["validation"].get("violations", []):
                    violations[v] = violations.get(v, 0) + 1
    job.rows_written = n_gen
    top = sorted(violations.items(), key=lambda x: -x[1])[:12]
    print(f"{league} {season} weeks {weeks}: generated {n_gen}, unchanged {n_skip}, validation failures {n_fail} (model {getattr(client, 'model', None)}, workers {workers})")
    if top:
        print(f"  most common validator objections: {top}")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--league", required=True, choices=config.LEAGUES)
    p.add_argument("--season", type=int, default=config.SEASON)
    p.add_argument("--weeks", nargs="*", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--trigger", default="manual")
    a = p.parse_args(argv)
    games = storage.read_table(storage.games_path(a.league, a.season))
    if a.weeks:
        weeks = a.weeks
    else:
        sched = games[games.status == "SCHEDULED"]
        cur = int(sched.week.min()) if not sched.empty else int(games.week.max())
        weeks = [cur, cur + 1]
    with JobRun(f"{a.league}_AI", a.league, a.trigger) as job:
        run(a.league, a.season, weeks, job, limit=a.limit, workers=a.workers)


if __name__ == "__main__":
    main()
