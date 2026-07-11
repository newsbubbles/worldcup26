"""Deterministic bracket logic shared by the store and the build.

A knockout fixture can carry placeholder team codes until its feeders are decided:
  - 'W-XXXYYY'  two-team slot  = winner of the played XXX-vs-YYY match
The functions here resolve those placeholders from recorded results and compute
the current tournament stage. Pure structure, no model judgment.
"""

from __future__ import annotations

from typing import Any

ROUND_ORDER = ["R32", "R16", "QF", "SF", "F"]
ROUND_LABELS = {
    "R32": "Round of 32", "R16": "Round of 16", "QF": "Quarter-finals",
    "SF": "Semi-finals", "F": "Final",
}


def winner(m: dict[str, Any]) -> str | None:
    if m.get("pw"):
        return m["pw"]
    sa, sb = m.get("sa"), m.get("sb")
    if sa is None or sb is None:
        return None
    if sa > sb:
        return m["a"]
    if sb > sa:
        return m["b"]
    return None


def resolve_code(matches: list[dict], code: str) -> str:
    """A 'W-XXXYYY' placeholder → the winner of the played XXX-vs-YYY match, if decided."""
    if isinstance(code, str) and code.startswith("W-") and len(code) == 8:
        c1, c2 = code[2:5], code[5:8]
        for m in matches:
            if m["status"] == "played" and frozenset((m["a"], m["b"])) == frozenset((c1, c2)):
                if (w := winner(m)):
                    return w
    return code


def resolved_pair(matches: list[dict], m: dict) -> frozenset[str]:
    return frozenset((resolve_code(matches, m["a"]), resolve_code(matches, m["b"])))


def find_fixture(matches: list[dict], round_: str, a: str, b: str) -> dict | None:
    """The fixture a reported result belongs to — matched by exact codes OR by a
    placeholder slot in the same round that resolves to the reported teams. This is
    what lets 'ESP vs BEL' fill the 'W-PORESP vs W-USABEL' quarter-final instead of
    creating a duplicate."""
    want = frozenset((a, b))
    for m in matches:
        if m["round"] != round_:
            continue
        if frozenset((m["a"], m["b"])) == want or resolved_pair(matches, m) == want:
            return m
    return None


def current_stage(matches: list[dict]) -> str:
    """The earliest round that still has an unplayed fixture — the live stage."""
    for r in ROUND_ORDER:
        rms = [m for m in matches if m["round"] == r]
        if rms and any(m["status"] == "upcoming" for m in rms):
            return r
    return "F"


def stage_tag(matches: list[dict]) -> str:
    return f"Knockout Stage · <b>{ROUND_LABELS[current_stage(matches)]}</b> · North America"
