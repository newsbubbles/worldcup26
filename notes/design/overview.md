# worldcup26 — live 3D match center

One self-contained 3D interface (`dist/index.html`) + an agentic updater that keeps
the data current. The interface embeds all data and the display font, so the built
file works anywhere (artifact, file://, phone).

## Data flow

```
serper scraper MCP (vendor/, one process per researcher)
        │  google_search / scrape / batch_scrape
        ▼
researcher agents (pydantic-ai, kimi-k2.5 via OpenRouter, fresh Agent per run)
        │  report_* tools (validated, staged)
        ▼
datastore MCP (updater/datastore_mcp.py)  ──commit──▶  data/worldcup.json
                                                          │
                                                 updater/build.py
                                                          ▼
                                                   dist/index.html
```

## Researchers (updater/run_update.py)

- **bracket** — match results, upcoming fixtures, stage tag. Runs every matchday.
- **stats** — golden boot table + star player tournament stats, with source URLs.
- **squads** — starting XI + formation per team (only teams still alive / flagged stale).

Each researcher is a fresh `Agent` per run (shared-Agent concurrency bug), with a
cached per-process `OpenAIModel`. Each gets its **own copy** of the serper MCP and
the datastore MCP via `MCPServerStdio` with tooler's `process_tool_call` argument
normalizer. Output is structured (`RunReport`), never hand-parsed.

## Datastore tool surface (agentic-tooling principles applied)

Domain subset: the world cup data store. Control-complete loop:

| step | tool |
|---|---|
| discover/inspect | `get_snapshot` (windowed by section — never dumps the whole store) |
| act | `report_match_result`, `report_lineup`, `report_player_stats`, `report_top_scorers`, `upsert_team`, `set_stage_tag` |
| verify/close | `commit_update` (atomic merge + returns a diff summary), `discard_staged` |

Design decisions, by principle:

- **Bounded context (P5):** `get_snapshot` takes a `section` (`meta | bracket | teams | team | scorers`)
  and returns only that slice plus per-section freshness. No unwindowed reads.
- **Errors point forward (P9/P11):** every validation error names the field, the
  constraint, and the tool call to make next. Example: unknown team code → error
  lists valid codes and says to call `upsert_team` first.
- **Context-defensive (P10):** reports on already-`played` matches are rejected
  unless `correction=true` with a `note` — protects settled results from
  re-scrape noise. Every report requires a `sourceUrl`. `commit_update` with an
  empty stage is an error, not a no-op.
- **Batch (P6):** lineups land as one 11-player call; the scorer table as one
  replace-all call (min 10 rows so a partial scrape can't clobber the table).
- **Staged, then atomic (P2):** report_* stages in-process; `commit_update` takes a
  file lock, merges into `data/worldcup.json`, validates the whole document with
  pydantic, writes tmp+rename, rebuilds `dist/index.html`, and returns what changed.

No fallbacks anywhere: missing keys, failed validation, or a failed commit raise
with state. A researcher that can't verify a stat omits it; it never guesses.

## Provenance

`meta.sources` is the canonical source list (rendered in the interface footer);
every star player carries a `src` URL rendered on the card back. Commits merge
newly consulted sources in.
