"""Staged, validated store over data/worldcup.json.

report_* operations stage; commit() locks the file, merges, validates the whole
document against the models, writes atomically, and rebuilds dist/index.html.
Errors are StoreError with forward-pointing guidance (they surface verbatim to
the researcher agent).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from filelock import FileLock
from pydantic import ValidationError

from .models import LineupPlayer, StarPlayer, TopScorer, WorldCupData

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = Path(os.getenv("WORLDCUP_DATA_FILE", ROOT / "data" / "worldcup.json"))


class StoreError(ValueError):
    """Validation/merge failure with guidance on what to do next."""


def _match_key(a: str, b: str) -> frozenset[str]:
    return frozenset((a, b))


class Store:
    def __init__(self, data_file: Path = DATA_FILE):
        self.data_file = Path(data_file)
        self.lock = FileLock(str(self.data_file) + ".lock")
        self.staged: list[dict[str, Any]] = []

    # ---------- reads ----------

    def load(self) -> dict[str, Any]:
        if not self.data_file.exists():
            raise StoreError(
                f"data file {self.data_file} does not exist — the store must be seeded "
                f"before researchers run; stop and report this to the operator"
            )
        raw = json.loads(self.data_file.read_text(encoding="utf-8"))
        # Validate on read so a corrupt file is caught at the boundary
        WorldCupData.model_validate(raw)
        return raw

    def snapshot(self, section: str, team: str | None = None) -> dict[str, Any]:
        data = self.load()
        base = {"staged_ops": len(self.staged), "updated": data["updated"]}
        if section == "meta":
            return base | {
                "stageTag": data["stageTag"],
                "teams": {
                    code: {"lineupNote": t.get("lineupNote", ""), "stars": len(t.get("stars", []))}
                    for code, t in data["teams"].items()
                },
                "match_counts": {
                    s: sum(1 for m in data["matches"] if m["status"] == s)
                    for s in ("played", "upcoming")
                },
                "topScorers_rows": len(data["topScorers"]),
            }
        if section == "bracket":
            return base | {"matches": data["matches"], "teamNames": data["teamNames"]}
        if section == "teams":
            return base | {
                "teams": {
                    code: {"name": t["name"], "formation": t["formation"], "lineupNote": t.get("lineupNote", "")}
                    for code, t in data["teams"].items()
                }
            }
        if section == "team":
            if not team or team not in data["teams"]:
                raise StoreError(
                    f"section='team' needs a valid team code; known codes: {sorted(data['teams'])}"
                )
            return base | {"team": data["teams"][team]}
        if section == "scorers":
            return base | {"topScorers": data["topScorers"], "gbNote": data["gbNote"]}
        raise StoreError(
            f"unknown section {section!r}; use one of: meta, bracket, teams, team, scorers"
        )

    # ---------- staging (validates against current data, then queues) ----------

    def _stage(self, kind: str, payload: dict[str, Any], source: dict[str, str] | None) -> str:
        self.staged.append({"kind": kind, "payload": payload, "source": source})
        return (
            f"staged ({len(self.staged)} pending). Stage further reports or call "
            f"commit_update to write them."
        )

    def stage_match(self, payload: dict[str, Any], source_url: str, correction: bool, note: str) -> str:
        data = self.load()
        known = set(data["teamNames"]) | set(data["teams"])
        for code in (payload["a"], payload["b"]):
            if code not in known:
                raise StoreError(
                    f"unknown team code {code!r}. Known codes: {sorted(known)}. "
                    f"If this is a genuinely new team, call upsert_team first; if it is a "
                    f"placeholder slot (e.g. a QF winner), use the existing placeholder code "
                    f"from get_snapshot(section='bracket')."
                )
        existing = next(
            (m for m in data["matches"]
             if m["round"] == payload["round"] and _match_key(m["a"], m["b"]) == _match_key(payload["a"], payload["b"])),
            None,
        )
        if existing and existing["status"] == "played" and not correction:
            raise StoreError(
                f"match {payload['round']} {payload['a']}-{payload['b']} is already recorded as "
                f"played ({existing.get('sa')}-{existing.get('sb')}). Settled results are protected. "
                f"If a source proves the stored score wrong, re-call report_match_result with "
                f"correction=true and a note explaining the discrepancy."
            )
        return self._stage("match", payload, {"topic": f"{payload['round']} {payload['a']}–{payload['b']}", "url": source_url})

    def stage_lineup(self, team: str, formation: str, lineup: list[dict], lineup_note: str, source_url: str) -> str:
        data = self.load()
        if team not in data["teams"]:
            raise StoreError(
                f"no team entry for {team!r}; call upsert_team with full metadata first. "
                f"Teams with entries: {sorted(data['teams'])}"
            )
        # dry-run the full team validation with the new lineup
        candidate = copy.deepcopy(data["teams"][team])
        candidate["formation"] = formation
        candidate["lineup"] = lineup
        candidate["lineupNote"] = lineup_note
        self._validate_team(team, candidate)
        return self._stage(
            "lineup",
            {"team": team, "formation": formation, "lineup": lineup, "lineupNote": lineup_note},
            {"topic": f"{team} lineup", "url": source_url},
        )

    def stage_stars(self, team: str, stars: list[dict]) -> str:
        data = self.load()
        if team not in data["teams"]:
            raise StoreError(f"no team entry for {team!r}; call upsert_team first")
        for s in stars:
            try:
                StarPlayer.model_validate(s)
            except ValidationError as e:
                raise StoreError(
                    f"star {s.get('name', '?')!r} invalid: {e.errors()[0]['msg']} "
                    f"(field {e.errors()[0]['loc']}). Every star needs a src URL for its stats — "
                    f"omit players whose stats you could not verify."
                ) from e
        candidate = copy.deepcopy(data["teams"][team])
        candidate["stars"] = stars
        self._validate_team(team, candidate)
        return self._stage("stars", {"team": team, "stars": stars}, None)

    def stage_scorers(self, scorers: list[dict], gb_note: str, source_url: str) -> str:
        data = self.load()
        if len(scorers) < 10:
            raise StoreError(
                f"got {len(scorers)} scorers — the table replaces wholesale and needs >=10 rows "
                f"so a partial scrape can't clobber it. Scrape a full top-scorers list first."
            )
        known = set(data["teamNames"]) | set(data["teams"])
        for s in scorers:
            TopScorer.model_validate(s)
            if s["country"] not in known:
                raise StoreError(
                    f"scorer {s['name']!r}: unknown country code {s['country']!r}. "
                    f"Use codes from get_snapshot(section='bracket') teamNames."
                )
        prev = {x["name"]: x["goals"] for x in data["topScorers"]}
        for s in scorers:
            if s["name"] in prev and s["goals"] < prev[s["name"]]:
                raise StoreError(
                    f"{s['name']} has {prev[s['name']]} goals on record but the report says "
                    f"{s['goals']} — goal tallies never decrease. Verify against a second source; "
                    f"if the stored value is wrong, include a gbNote explaining the correction."
                )
        return self._stage(
            "scorers",
            {"scorers": scorers, "gbNote": gb_note},
            {"topic": "Golden Boot standings", "url": source_url},
        )

    def stage_team(self, code: str, team: dict[str, Any], source_url: str) -> str:
        self._validate_team(code, team)
        return self._stage("team", {"code": code, "team": team}, {"topic": f"{code} team profile", "url": source_url})

    def stage_stage_tag(self, stage_tag: str, source_url: str) -> str:
        if len(stage_tag) > 120 or "<script" in stage_tag.lower():
            raise StoreError("stageTag must be a short header line (<=120 chars, <b> only)")
        return self._stage("stageTag", {"stageTag": stage_tag}, {"topic": "tournament stage", "url": source_url})

    def _validate_team(self, code: str, team: dict[str, Any]) -> None:
        from .models import Team
        try:
            Team.model_validate(team)
        except ValidationError as e:
            first = e.errors()[0]
            raise StoreError(
                f"team {code!r} invalid at {'.'.join(str(p) for p in first['loc'])}: {first['msg']}. "
                f"Fix the payload and re-call; get_snapshot(section='team', team='{code}') shows the "
                f"current valid entry as a reference."
            ) from e

    # ---------- commit ----------

    def discard(self) -> str:
        n = len(self.staged)
        self.staged.clear()
        return f"discarded {n} staged operation(s); the data file was not touched"

    def commit(self, summary: str) -> dict[str, Any]:
        if not self.staged:
            raise StoreError(
                "nothing staged — call report_* tools first, then commit. If you found no "
                "changes worth making, finish without committing and say so in your report."
            )
        with self.lock:
            data = self.load()
            changes: list[str] = []
            for op in self.staged:
                changes.append(self._apply(data, op))
            for op in self.staged:
                src = op.get("source")
                if src and all(s["url"] != src["url"] for s in data["sources"]):
                    data["sources"].append(src)
            data["updated"] = dt.date.today().isoformat()

            try:
                WorldCupData.model_validate(data)
            except ValidationError as e:
                first = e.errors()[0]
                raise StoreError(
                    f"merged document failed validation at "
                    f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}. "
                    f"Nothing was written. Discard_staged and re-stage corrected reports."
                ) from e

            tmp = self.data_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.data_file)

        from .build import build
        dist = build()
        self.staged.clear()
        return {"committed": changes, "summary": summary, "rebuilt": str(dist), "updated": data["updated"]}

    def _apply(self, data: dict[str, Any], op: dict[str, Any]) -> str:
        kind, p = op["kind"], op["payload"]
        if kind == "match":
            existing = next(
                (m for m in data["matches"]
                 if m["round"] == p["round"] and _match_key(m["a"], m["b"]) == _match_key(p["a"], p["b"])),
                None,
            )
            if existing:
                was = f"{existing.get('sa')}-{existing.get('sb')} ({existing['status']})"
                existing.update(p)
                return f"match {p['round']} {p['a']}–{p['b']}: {was} -> {p.get('sa')}-{p.get('sb')} ({p['status']})"
            data["matches"].append(p)
            return f"match added: {p['round']} {p['a']}–{p['b']} ({p['status']})"
        if kind == "lineup":
            t = data["teams"][p["team"]]
            t["formation"], t["lineup"], t["lineupNote"] = p["formation"], p["lineup"], p["lineupNote"]
            return f"lineup {p['team']}: {p['formation']} ({p['lineupNote']})"
        if kind == "stars":
            data["teams"][p["team"]]["stars"] = p["stars"]
            return f"stars {p['team']}: {len(p['stars'])} players"
        if kind == "scorers":
            data["topScorers"] = p["scorers"]
            if p["gbNote"]:
                data["gbNote"] = p["gbNote"]
            return f"topScorers replaced ({len(p['scorers'])} rows)"
        if kind == "team":
            data["teams"][p["code"]] = p["team"]
            data["teamNames"][p["code"]] = p["team"]["name"]
            return f"team upserted: {p['code']}"
        if kind == "stageTag":
            data["stageTag"] = p["stageTag"]
            return f"stageTag -> {p['stageTag']!r}"
        raise StoreError(f"unknown staged op kind {kind!r}")
