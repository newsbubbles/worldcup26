"""Tool-argument normalizer for MCP calls — tooler's process_tool_call pattern.

Non-Claude models (kimi, grok, ...) sometimes emit stringified JSON for the
`request` object, or wrap the whole args dict in a string. Normalizing here at
the client layer fixes the call before schema validation, instead of burning
retries on a confused agent.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("worldcup26.normalizer")


def extract_json_object(text: str) -> Any | None:
    """Parse a JSON object/array out of a string, tolerating trailing junk
    ("{...}}}}", "{...}{}") and markdown fences."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    try:
        obj, _end = json.JSONDecoder().raw_decode(s)
        return obj
    except json.JSONDecodeError:
        return None


def normalize_tool_args(args: dict[str, Any] | str) -> dict[str, Any]:
    """Normalize stringified JSON arguments (root-level or per-key)."""
    if isinstance(args, str):
        parsed = extract_json_object(args)
        if isinstance(parsed, dict):
            args = parsed
        else:
            return {}
    if not isinstance(args, dict):
        return {}

    normalized = dict(args)
    for key, value in args.items():
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                parsed = extract_json_object(stripped)
                if parsed is not None:
                    normalized[key] = parsed
    return normalized


async def normalizing_process_tool_call(ctx, call_tool, name: str, tool_args: dict[str, Any]):
    """process_tool_call callback for MCPToolset — normalizes args before dispatch."""
    normalized = normalize_tool_args(tool_args)
    if normalized != tool_args:
        logger.info("normalized stringified JSON args for tool %r", name)
    return await call_tool(name, normalized)
