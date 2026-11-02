"""SQLAlchemy models. App DB is the source of truth for evidence, provenance and workflow state."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes (SQLite drops tzinfo; we restore it on read)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def _dt() -> Mapped[datetime]:
    return mapped_column(UTCDateTime(), default=utcnow)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ws"))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    bank_id: Mapped[str] = mapped_column(String(80), unique=True)
    current_summary_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _dt()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "dedupe_key", name="uq_source_identity"),
        Index("ix_sources_ws_status", "workspace_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("src"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(10))  # pdf | url | note
    title: Mapped[str] = mapped_column(String(500), default="")
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(200))
    # queued | processing | completed | partial | failed
    status: Mapped[str] = mapped_column(String(16), default="queued")
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_of: Mapped[str | None] = mapped_column(String(32), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = _dt()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    versions: Mapped[list["SourceVersion"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class SourceVersion(Base):
    __tablename__ = "source_versions"
    __table_args__ = (UniqueConstraint("source_id", "version", name="uq_source_version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("sv"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    original_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    extracted_text: Mapped[str] = mapped_column(Text, default="")  # normalized full text, never discarded
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _dt()
    source: Mapped[Source] = relationship(back_populates="versions")


class EvidencePassage(Base):
    __tablename__ = "evidence_passages"
    __table_args__ = (
        Index("ix_passages_ws_current", "workspace_id", "is_current"),
        Index("ix_passages_source", "source_id", "is_current"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("psg"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(300), nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    suspicious: Mapped[bool] = mapped_column(Boolean, default=False)
    flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _dt()


class ExtractedFinding(Base):
    __tablename__ = "extracted_findings"
    __table_args__ = (Index("ix_findings_ws", "workspace_id", "superseded"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("fnd"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id", ondelete="CASCADE"))
    passage_id: Mapped[str] = mapped_column(ForeignKey("evidence_passages.id", ondelete="CASCADE"))
    # finding | definition | method | result | limitation | open_question
    kind: Mapped[str] = mapped_column(String(20))
    statement: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text)  # verbatim from passage
    # source_stated | user_note | inference | uncertain
    epistemic: Mapped[str] = mapped_column(String(16), default="source_stated")
    concepts: Mapped[list[str]] = mapped_column(JSON, default=list)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(300), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    extraction_method: Mapped[str] = mapped_column(String(16), default="extractive")  # extractive | llm
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    memory_state: Mapped[str] = mapped_column(String(12), default="pending")  # pending | retained | skipped
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _dt()


class ResearchRelationship(Base):
    __tablename__ = "research_relationships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "finding_a_id", "finding_b_id", "predicate", name="uq_relationship"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("rel"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    finding_a_id: Mapped[str] = mapped_column(ForeignKey("extracted_findings.id", ondelete="CASCADE"))
    finding_b_id: Mapped[str] = mapped_column(ForeignKey("extracted_findings.id", ondelete="CASCADE"))
    subject: Mapped[str] = mapped_column(String(200))
    predicate: Mapped[str] = mapped_column(String(20))  # supports|contradicts|extends|uses|depends_on|applies_to
    object: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="hypothesis")  # supported | hypothesis | rejected
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)  # passage ids
    rationale: Mapped[str] = mapped_column(Text, default="")
    detected_by: Mapped[str] = mapped_column(String(12), default="heuristic")  # heuristic | llm | user
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _dt()


class SummaryVersion(Base):
    __tablename__ = "summary_versions"
    __table_args__ = (UniqueConstraint("workspace_id", "version", name="uq_summary_version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("sum"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    markdown: Mapped[str] = mapped_column(Text, default="")
    change: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    execution_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(12), default="generated")  # generated | rollback
    created_at: Mapped[datetime] = _dt()


class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"
    __table_args__ = (Index("ix_exec_status", "status", "created_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("exe"))
    workflow: Mapped[str] = mapped_column(String(32))
    workspace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(12), default="queued")
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failed_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_resumes: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _dt()
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    steps: Mapped[list["WorkflowStep"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan", order_by="WorkflowStep.id"
    )


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("workflow_executions.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12), default="running")  # running|succeeded|failed|skipped
    started_at: Mapped[datetime] = _dt()
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution: Mapped[WorkflowExecution] = relationship(back_populates="steps")


class MemoryOperation(Base):
    """Ledger of every Hindsight side effect. The unique idempotency key prevents duplicate writes on retry."""
    __tablename__ = "memory_operations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("mop"))
    execution_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    op: Mapped[str] = mapped_column(String(20))  # retain | recall | consolidate | create_bank
    bank_id: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(10))  # done | failed | skipped
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _dt()


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("evl"))
    suite: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = _dt()
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    gates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    environment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("cnv"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = _dt()


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("trn"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    execution_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = _dt()

