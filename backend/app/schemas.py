"""Structured answer schemas shared by workflows, API and UI."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ClaimKind = Literal["source_stated", "user_note", "inference", "uncertain"]
AnswerMode = Literal["supported", "partial", "conflicting", "insufficient"]


class Citation(BaseModel):
    passage_id: str
    source_id: str
    source_title: str
    source_kind: str = ""
    page: int | None = None
    section: str | None = None
    quote: str = ""  # verbatim excerpt from the stored passage
    verified: bool = False  # quote verified as substring of the stored passage


class Claim(BaseModel):
    id: str
    text: str
    kind: ClaimKind = "source_stated"
    citations: list[Citation] = Field(default_factory=list)
    memory_refs: list[str] = Field(default_factory=list)
    support: float | None = None
    status: Literal["supported", "unsupported", "flagged"] = "supported"
    notes: list[str] = Field(default_factory=list)


class ConflictNote(BaseModel):
    description: str
    relationship_id: str | None = None
    status: str = "hypothesis"  # supported | hypothesis
    claims: list[str] = Field(default_factory=list)
    passage_ids: list[str] = Field(default_factory=list)
    sides: list[dict] = Field(default_factory=list)  # [{source_title, source_id, passage_id, page, text, cited}]


class MemoryUsed(BaseModel):
    id: str
    text: str
    source_ids: list[str] = Field(default_factory=list)
    role: str = "context"  # context | discrepancy | source_hint


class AnswerResult(BaseModel):
    mode: AnswerMode
    answer: str
    claims: list[Claim] = Field(default_factory=list)
    conflicts: list[ConflictNote] = Field(default_factory=list)
    memory_used: list[MemoryUsed] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    plan: dict = Field(default_factory=dict)
    generation_mode: str = "extractive"
    evidence_considered: int = 0
    dropped_claims: int = 0
