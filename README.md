# worldcup26

Live 3D World Cup 2026 match center. One self-contained HTML file
(`dist/index.html`) — 3D pitch with tap-to-flip player cards, knockout bracket,
Golden Boot race, sourced stats — kept current by pydantic-ai researcher agents
over the serper scraper MCP.

```
.venv\Scripts\python -m updater.run_update            # refresh everything
.venv\Scripts\python -m updater.run_update --scope bracket
.venv\Scripts\python -m updater.build                 # rebuild dist from data only
```

Data lives in `data/worldcup.json` (validated by `updater/models.py`); the
researchers write through the datastore MCP (`updater/datastore_mcp.py`), never
directly. Design notes in `notes/design/overview.md`.
