"""Canonical data model for data/worldcup.json.

The JSON shape is exactly what the frontend embeds as DATA — build.py injects it
verbatim. Everything the updater stages or commits validates against these models.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Round = Literal["R32", "R16", "QF", "SF", "F"]
MatchStatus = Literal["played", "upcoming"]


def _require_http(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"source URL must be absolute http(s), got: {url!r}")
    return url


class Match(BaseModel):
    round: Round
    a: str = Field(..., description="Team code (or placeholder code like W49)")
    b: str
    sa: Optional[int] = Field(None, ge=0, description="Score for a (omit if upcoming)")
    sb: Optional[int] = Field(None, ge=0)
    pens: Optional[str] = Field(None, pattern=r"^\d+-\d+$", description="Shootout score, e.g. 4-2")
    pw: Optional[str] = Field(None, description="Team code of shootout winner (required with pens)")
    date: str = Field(..., description="Display date, e.g. 'Jul 9'")
    venue: str = ""
    status: MatchStatus

    @model_validator(mode="after")
    def _coherent(self) -> "Match":
        if self.status == "played" and (self.sa is None or self.sb is None):
            raise ValueError("played match needs both scores; set status='upcoming' if not finished")
        if self.pens and not self.pw:
            raise ValueError("pens given without pw (shootout winner code)")
        if self.pw and self.pw not in (self.a, self.b):
            raise ValueError(f"pw={self.pw!r} is neither a={self.a!r} nor b={self.b!r}")
        return self


class LineupPlayer(BaseModel):
    num: int = Field(..., ge=1, le=99)
    name: str = Field(..., min_length=2)
    pos: str = Field(..., min_length=1, max_length=8)


class StarPlayer(BaseModel):
    name: str = Field(..., min_length=2)
    pos: str = ""
    club: str = ""
    age: Optional[int] = Field(None, ge=15, le=45)
    num: Optional[int] = Field(None, ge=1, le=99)
    wcG: int = Field(0, ge=0, description="Goals at this World Cup")
    wcA: Optional[int] = Field(None, ge=0, description="Assists at this World Cup (omit if unverified)")
    min: Optional[int] = Field(None, ge=0, description="Minutes at this World Cup")
    note: str = Field("", max_length=400)
    src: str = Field(..., description="URL of the page the stats were verified against")

    _src_http = field_validator("src")(_require_http)


class TeamColors(BaseModel):
    p: str = Field(..., description="Primary kit color (hex)")
    s: str = Field(..., description="Secondary kit color (hex)")
    ink: str = Field("#ffffff", description="Number color on the shirt")


class Team(BaseModel):
    name: str
    nickname: str = ""
    coach: str = ""
    rank: Optional[int] = Field(None, ge=1)
    colors: TeamColors
    flag: str = Field(..., description="CSS background for the flag chip (gradient)")
    formation: str = Field(..., pattern=r"^\d(-\d){2,4}$")
    lineup: List[LineupPlayer] = Field(..., min_length=11, max_length=11)
    stars: List[StarPlayer] = Field(default_factory=list, max_length=6)
    lineupNote: str = Field("", description="Which match this XI is from")

    @model_validator(mode="after")
    def _formation_matches_xi(self) -> "Team":
        outfield = sum(int(n) for n in self.formation.split("-"))
        if outfield != 10:
            raise ValueError(f"formation {self.formation} sums to {outfield}, needs 10 outfielders")
        nums = [p.num for p in self.lineup]
        if len(set(nums)) != 11:
            raise ValueError(f"duplicate shirt numbers in lineup: {sorted(nums)}")
        if self.lineup[0].pos.upper() != "GK":
            raise ValueError("lineup[0] must be the goalkeeper (pos='GK') — pitch layout depends on it")
        return self


class TopScorer(BaseModel):
    name: str
    country: str = Field(..., description="Team code")
    goals: int = Field(..., ge=1)
    assists: Optional[int] = Field(None, ge=0)


class Source(BaseModel):
    topic: str
    url: str

    _url_http = field_validator("url")(_require_http)


class WorldCupData(BaseModel):
    updated: str = Field(..., description="ISO date the data was last verified")
    stageTag: str = Field(..., description="Header line, e.g. 'Knockout Stage · <b>Round of 16</b> · North America'")
    defaultTeam: str
    teamNames: Dict[str, str] = Field(..., description="code -> display name for every code used in matches")
    flags: Dict[str, str] = Field(..., description="code -> flag CSS for teams without a full entry")
    matches: List[Match]
    teams: Dict[str, Team]
    topScorers: List[TopScorer] = Field(..., min_length=5)
    gbNote: str = ""
    sources: List[Source] = Field(..., min_length=1)
    faces: Dict[str, Any] = Field(default_factory=dict, description="player name -> {img, credit, license, src}; injected at build time, not stored in worldcup.json")

    @model_validator(mode="after")
    def _codes_resolve(self) -> "WorldCupData":
        known = set(self.teamNames) | set(self.teams)
        for m in self.matches:
            for code in (m.a, m.b):
                if code not in known:
                    raise ValueError(
                        f"match {m.round} {m.a}-{m.b}: code {code!r} not in teamNames/teams; "
                        f"add it to teamNames (or upsert_team) first"
                    )
        for s in self.topScorers:
            if s.country not in known:
                raise ValueError(f"topScorers: unknown country code {s.country!r} for {s.name}")
        if self.defaultTeam not in self.teams:
            raise ValueError(f"defaultTeam {self.defaultTeam!r} has no team entry")
        return self
