"""Researcher agent factory.

Fresh Agent per run (a shared pydantic-ai Agent serializes concurrent runs when
structured output is involved), cached model per process, LLM-agnostic via the
OpenAI-compatible endpoint (OpenRouter by default, kimi-k2.5).

Each researcher gets its own copies of both MCP servers:
  - serper scraper (vendor/) as a stdio subprocess — google_search / scrape / batch_scrape
  - datastore (in-process FastMCP) — the validated write path

Both go through the argument normalizer (tooler's process_tool_call pattern).
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import List


from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from .datastore_mcp import make_datastore_server
from .normalizer import normalizing_process_tool_call
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
SERPER_SCRIPT = ROOT / "vendor" / "serper-scraper" / "serper_scrape_mcp.py"

# Two OpenRouter quirks bite kimi-k2.5 here, both fixed via extra_body:
#   1. Load-balancing routes to third-party providers that return
#      finish_reason='error' on tool-heavy payloads — pin to first-party Moonshot.
#   2. kimi-k2.5 runs with "thinking" on, and Moonshot rejects the tool_choice:
#      'required' that pydantic-ai uses for structured output while thinking is
#      enabled — so disable reasoning.
_OPENROUTER_PROVIDERS = ["moonshotai", "novita"]


def researcher_settings() -> OpenAIChatModelSettings | None:
    base = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    if "openrouter" not in base:
        return None
    return OpenAIChatModelSettings(
        extra_body={
            "provider": {"order": _OPENROUTER_PROVIDERS, "allow_fallbacks": False},
            "reasoning": {"enabled": False},
        }
    )


class RunReport(BaseModel):
    """Structured result every researcher must return."""
    committed: bool = Field(..., description="Whether commit_update succeeded")
    sections_updated: List[str] = Field(default_factory=list, description="e.g. ['bracket', 'stars:FRA']")
    summary: str = Field(..., max_length=900, description="What changed, what was verified but unchanged, what could not be verified")
    sources_consulted: List[str] = Field(default_factory=list)


@lru_cache(maxsize=4)
def get_model(name: str | None = None) -> OpenAIChatModel:
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY (or OPENAI_API_KEY) is not set — refusing to start")
    return OpenAIChatModel(
        name or os.environ.get("LLM_MODEL_NAME", "moonshotai/kimi-k2.5"),
        provider=OpenAIProvider(
            base_url=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=api_key,
        ),
    )


def serper_toolset() -> MCPToolset:
    serper_key = os.environ.get("SERPER_API_KEY")
    if not serper_key:
        raise RuntimeError("SERPER_API_KEY is not set — refusing to start")
    transport = StdioTransport(
        command=sys.executable,
        args=[str(SERPER_SCRIPT)],
        env={
            "SERPER_API_KEY": serper_key,
            # scrape-side LLM noise filtering rides the same OpenRouter account
            "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
            "LLM_MODEL_NAME": os.environ.get("SCRAPE_LLM_MODEL_NAME", "x-ai/grok-4.1-fast"),
        },
    )
    return MCPToolset(
        client=transport,
        id="serper",
        max_retries=3,
        init_timeout=60,
        process_tool_call=normalizing_process_tool_call,
    )


def datastore_toolset(store: Store) -> MCPToolset:
    return MCPToolset(
        client=make_datastore_server(store),
        id="datastore",
        max_retries=3,
        process_tool_call=normalizing_process_tool_call,
    )


def make_researcher(instructions: str, model_name: str | None = None) -> Agent:
    """Fresh Agent wired to fresh MCP toolset copies. One per run, never shared."""
    return Agent(
        get_model(model_name),
        toolsets=[serper_toolset(), datastore_toolset(Store())],
        output_type=RunReport,
        instructions=instructions,
        model_settings=researcher_settings(),
        retries=3,
    )
