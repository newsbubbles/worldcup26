"""Run the researcher agents.

  python -m updater.run_update                    # all three researchers
  python -m updater.run_update --scope bracket
  python -m updater.run_update --scope squads --teams FRA,ENG
  python -m updater.run_update --parallel 1       # serialize (debugging)

Each researcher runs as a fresh Agent with its own serper + datastore MCP
copies. Failures raise with state — there is no fallback path.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from pydantic_ai.usage import UsageLimits  # noqa: E402

from .agents import RunReport, make_researcher  # noqa: E402  (env must load first)
from .store import Store  # noqa: E402

# A thorough refresh reads a snapshot, searches + cross-checks two sources per game
# or player, then stages and commits — comfortably more than pydantic-ai's default
# 50. High enough for a full round, low enough to still trip a runaway loop.
RUN_LIMITS = UsageLimits(request_limit=120)

TODAY = dt.date.today().isoformat()

PREAMBLE = f"""You are a research agent keeping a live World Cup 2026 interface current.
Today is {TODAY}. The tournament is the 2026 FIFA World Cup (USA/Canada/Mexico, 48 teams).

Tools come from two attached MCP servers. EVERY tool takes a single `request`
object argument — pass a JSON object, never a JSON string.

Web (serper scraper):
- google_search(request): Google via Serper. Use tbs='qdr:d' for last-day news.
- scrape(request): fetch one URL. Set process_with_llm=false — you do the reading.
- batch_scrape(request): parallel fetch several URLs for cross-checking.

Datastore (the ONLY write path — validated, staged, atomic):
- get_snapshot(request): read current state. ALWAYS start with section='meta',
  then read the sections you plan to touch.
- report_match_result / report_lineup / report_player_stats / report_top_scorers /
  upsert_team / set_stage_tag: stage validated changes. Every report needs a
  sourceUrl you actually fetched.
- commit_update(request): atomically write everything staged and rebuild the site.
- discard_staged(request): abandon staged work after a failed commit.

Rules:
- Verify before you write. Cross-check anything surprising against a second source.
- Never guess or extrapolate a stat. If you cannot verify it, omit it and say so
  in your final summary.
- If the stored data already matches what you verified, change nothing — finish
  with committed=false and say the data was confirmed fresh.
- A tool error message tells you what to do next; read it instead of retrying the
  same call.
- Finish by returning the RunReport structured output."""

MISSIONS = {
    "bracket": PREAMBLE + """

Mission: the knockout bracket.
1. get_snapshot(section='bracket'); note matches with status 'upcoming' whose date
   is today or earlier — those are your primary targets.
2. Search for their final scores (ESPN, FIFA.com, BBC, Al Jazeera, Wikipedia's
   '2026 FIFA World Cup knockout stage' page). Scrape at least two sources per result.
3. report_match_result for each newly played match (scores, pens/pw if a shootout,
   venue, short date like 'Jul 7').
4. Your ONLY writes are the RESULTS of games that were actually played
   (report_match_result). Do NOT set the stage tag and do NOT edit fixtures for
   games that haven't happened — bracket advancement and the stage label are both
   computed automatically from the results you record. Report a quarter-final with
   the real team codes (e.g. ESP, BEL); it fills the right slot on its own.
5. commit_update with a summary of the matchday.""",

    "stats": PREAMBLE + """

Mission: the Golden Boot race and star player stats.
1. get_snapshot(section='scorers') and section='meta'.
2. Find the current official top-scorer standings (FIFA.com, ESPN, NBC Sports,
   Sky Sports). Scrape the full table, not a headline.
3. If tallies changed: report_top_scorers with the full table (>=10 rows) and a
   fresh one-line gbNote. Goals never decrease — a lower number means your source
   is stale, not a correction.
4. For teams whose stars scored/assisted since the data was last updated
   (get_snapshot(section='team', team=CODE) to see current values):
   report_player_stats with updated wcG/wcA and each player's stats page as src
   (prefer FBref or ESPN player pages; keep existing note text unless wrong).
5. commit_update.""",

    "squads": PREAMBLE + """

Mission: starting lineups and formations for the teams listed in the task message.
1. For each team: get_snapshot(section='team', team=CODE) to see the stored XI and
   which match it came from (lineupNote).
2. Find the team's MOST RECENT completed match lineup (ESPN lineups pages are
   canonical; cross-check shirt numbers against Wikipedia's '2026 FIFA World Cup
   squads'). If the stored lineupNote already references that match, the team is
   fresh — skip it.
3. report_lineup with the XI in pitch order: GK first, then each formation row
   from the back line forward, left-to-right (e.g. 4-2-3-1: LB,CB,CB,RB / DM,DM /
   LW,AM,RW / ST).
4. If a team in the interface got eliminated, leave its entry as its final XI —
   do not delete teams.
5. commit_update.""",
}


_TRANSIENT = ("finish_reason", "'error'", "rate limit", "429", "500", "502", "503",
              "overloaded", "timeout", "temporarily")


def _is_transient(exc: BaseException) -> bool:
    """OpenRouter's cheap endpoints intermittently return a non-standard
    finish_reason='error' or a 5xx/429 under load. Those are worth another swing;
    a schema/logic error is not."""
    msg = str(exc).lower()
    return any(sig in msg for sig in _TRANSIENT)


async def run_role(role: str, extra: str, sem: asyncio.Semaphore, attempts: int = 4) -> RunReport:
    async with sem:
        task = {"bracket": "Run the bracket refresh now.",
                "stats": "Run the stats refresh now.",
                "squads": f"Refresh lineups for these teams: {extra}."}[role]
        for attempt in range(1, attempts + 1):
            agent = make_researcher(MISSIONS[role])  # fresh agent + MCP copies per swing
            print(f"[{role}] starting (attempt {attempt}/{attempts})", flush=True)
            try:
                result = await agent.run(task, usage_limits=RUN_LIMITS)
            except Exception as e:  # noqa: BLE001 — provider transients are opaque
                if attempt < attempts and _is_transient(e):
                    backoff = 4 * attempt
                    print(f"[{role}] transient provider error ({type(e).__name__}); "
                          f"retrying in {backoff}s", flush=True)
                    await asyncio.sleep(backoff)
                    continue
                raise
            report = result.output
            print(f"[{role}] done: committed={report.committed} — {report.summary}", flush=True)
            return report


def alive_teams() -> list[str]:
    """Teams with entries still in an upcoming match. Resolve the bracket first so a
    round's winners (whose next fixture is still a 'W-...' placeholder in the raw
    file) are counted as alive."""
    from .build import resolve_bracket
    data = resolve_bracket(Store().load())
    upcoming = {c for m in data["matches"] if m["status"] == "upcoming" for c in (m["a"], m["b"])}
    alive = [code for code in data["teams"] if code in upcoming]
    return alive or list(data["teams"])


async def main() -> int:
    ap = argparse.ArgumentParser(description="Run world cup researcher agents")
    ap.add_argument("--scope", choices=["all", "bracket", "stats", "squads"], default="all")
    ap.add_argument("--teams", help="comma-separated team codes for the squads researcher")
    ap.add_argument("--parallel", type=int, default=3)
    args = ap.parse_args()

    roles = ["bracket", "stats", "squads"] if args.scope == "all" else [args.scope]
    teams = args.teams or ",".join(alive_teams())
    sem = asyncio.Semaphore(max(1, args.parallel))

    results = await asyncio.gather(
        *(run_role(r, teams, sem) for r in roles), return_exceptions=True
    )

    failed = False
    for role, res in zip(roles, results):
        if isinstance(res, BaseException):
            failed = True
            print(f"[{role}] FAILED: {type(res).__name__}: {res}", file=sys.stderr, flush=True)
        else:
            print(f"[{role}] report:", json.dumps(res.model_dump(), ensure_ascii=False, indent=2), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
