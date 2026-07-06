"""Agent-facing datastore MCP — the researchers' only write path.

Control-complete loop over the world cup data store:
  discover/inspect: get_snapshot
  act:              report_match_result / report_lineup / report_player_stats /
                    report_top_scorers / upsert_team / set_stage_tag
  verify/close:     commit_update (atomic, returns diff) / discard_staged

Every tool takes exactly (request: BaseModel, ctx: Context). Every error message
says what was wrong and what call to make next.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from .models import LineupPlayer, StarPlayer, Team, TopScorer
from .store import Store, StoreError


# ---------- request models ----------

class SnapshotRequest(BaseModel):
    section: Literal["meta", "bracket", "teams", "team", "scorers"] = Field(
        ..., description="Which slice to read. Start with 'meta' for freshness overview."
    )
    team: Optional[str] = Field(None, description="Team code — required when section='team'")


class MatchReport(BaseModel):
    round: Literal["R32", "R16", "QF", "SF", "F"]
    a: str = Field(..., description="Team code of the first team (as listed in the bracket)")
    b: str
    sa: Optional[int] = Field(None, ge=0, description="Full-time/AET score for a; omit if upcoming")
    sb: Optional[int] = Field(None, ge=0)
    pens: Optional[str] = Field(None, description="Shootout score like '4-2', only if it went to pens")
    pw: Optional[str] = Field(None, description="Team code that won the shootout (required with pens)")
    date: str = Field(..., description="Short display date, e.g. 'Jul 9'")
    venue: str = ""
    status: Literal["played", "upcoming"]
    correction: bool = Field(False, description="Set true ONLY to overwrite an already-played result, with note")
    note: str = Field("", description="Required when correction=true: why the stored score was wrong")
    sourceUrl: str = Field(..., description="URL of the page the result was verified against")


class LineupReport(BaseModel):
    team: str = Field(..., description="Team code with an existing team entry")
    formation: str = Field(..., description="e.g. '4-2-3-1'; outfield rows must sum to 10")
    lineup: List[LineupPlayer] = Field(
        ..., min_length=11, max_length=11,
        description="Exactly 11: GK first, then each formation row back-to-front, left-to-right",
    )
    lineupNote: str = Field(..., description="Which match this XI started, e.g. 'R16 vs Spain, Jul 6'")
    sourceUrl: str


class StarsReport(BaseModel):
    team: str
    stars: List[StarPlayer] = Field(
        ..., min_length=1, max_length=6,
        description="Replaces the team's star list. Each star carries its own src stats URL. "
                    "Omit players whose stats you could not verify — never guess.",
    )


class ScorersReport(BaseModel):
    scorers: List[TopScorer] = Field(..., description="Full Golden Boot table, best first, >=10 rows")
    gbNote: str = Field("", description="One-line context for the race (records, tiebreakers)")
    sourceUrl: str


class TeamUpsert(BaseModel):
    code: str = Field(..., min_length=3, max_length=3, description="FIFA-style 3-letter code")
    team: Team
    sourceUrl: str


class StageTagReport(BaseModel):
    stageTag: str = Field(..., description="Header line, e.g. 'Knockout Stage · <b>Quarter-finals</b> · North America'")
    sourceUrl: str


class CommitRequest(BaseModel):
    summary: str = Field(..., min_length=10, description="One or two sentences: what changed and why")


class DiscardRequest(BaseModel):
    confirm: bool = Field(..., description="Must be true — staged reports are lost")


# ---------- server factory ----------

def make_datastore_server(store: Store) -> FastMCP:
    mcp = FastMCP("WorldCupDatastore")

    def guard(fn, *args):
        try:
            return fn(*args)
        except StoreError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def get_snapshot(request: SnapshotRequest, ctx: Context) -> dict:
        """Read a windowed slice of the current data. Sections: 'meta' (freshness
        overview — start here), 'bracket' (all matches + team codes), 'teams'
        (formations per team), 'team' (one full team entry), 'scorers' (Golden Boot)."""
        return guard(store.snapshot, request.section, request.team)

    @mcp.tool()
    async def report_match_result(request: MatchReport, ctx: Context) -> str:
        """Stage one match result or fixture. Settled (played) matches are protected:
        overwriting one requires correction=true plus a note. Requires sourceUrl."""
        if request.correction and not request.note.strip():
            raise ToolError(
                "correction=true requires a note explaining why the stored score is wrong "
                "(cite the source). Re-call with the note filled in."
            )
        payload = request.model_dump(exclude={"correction", "note", "sourceUrl"}, exclude_none=True)
        return guard(store.stage_match, payload, request.sourceUrl, request.correction, request.note)

    @mcp.tool()
    async def report_lineup(request: LineupReport, ctx: Context) -> str:
        """Stage a team's starting XI + formation as one batch. Order matters: GK first,
        then each formation row from the back line forward, left-to-right — the 3D pitch
        places pins in this order."""
        return guard(
            store.stage_lineup,
            request.team,
            request.formation,
            [p.model_dump() for p in request.lineup],
            request.lineupNote,
            request.sourceUrl,
        )

    @mcp.tool()
    async def report_player_stats(request: StarsReport, ctx: Context) -> str:
        """Stage a team's star players (replaces the team's star list). Each star needs
        tournament goals/assists and a src URL where the stats can be checked. Omit any
        stat you could not verify rather than estimating."""
        return guard(store.stage_stars, request.team, [s.model_dump() for s in request.stars])

    @mcp.tool()
    async def report_top_scorers(request: ScorersReport, ctx: Context) -> str:
        """Stage the full Golden Boot table (replace-all, >=10 rows, goals never decrease).
        Country must be a known team code from get_snapshot(section='bracket')."""
        return guard(
            store.stage_scorers,
            [s.model_dump() for s in request.scorers],
            request.gbNote,
            request.sourceUrl,
        )

    @mcp.tool()
    async def upsert_team(request: TeamUpsert, ctx: Context) -> str:
        """Stage a full team entry (metadata + kit colors + flag CSS + XI + stars).
        Only for teams that don't have an entry yet — for existing teams use
        report_lineup / report_player_stats."""
        return guard(store.stage_team, request.code, request.team.model_dump(), request.sourceUrl)

    @mcp.tool()
    async def set_stage_tag(request: StageTagReport, ctx: Context) -> str:
        """Stage the header stage line (advances as the tournament does)."""
        return guard(store.stage_stage_tag, request.stageTag, request.sourceUrl)

    @mcp.tool()
    async def commit_update(request: CommitRequest, ctx: Context) -> dict:
        """Atomically merge all staged reports into the data file, re-validate the whole
        document, and rebuild dist/index.html. Returns the list of applied changes —
        include it in your final report. Fails (writing nothing) if the merged document
        is invalid."""
        return guard(store.commit, request.summary)

    @mcp.tool()
    async def discard_staged(request: DiscardRequest, ctx: Context) -> str:
        """Throw away all staged reports without writing. Use after a failed commit to
        start over."""
        if not request.confirm:
            raise ToolError("set confirm=true to discard, or keep staging and commit_update instead")
        return store.discard()

    return mcp
