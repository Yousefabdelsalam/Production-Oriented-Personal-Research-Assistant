"""Explicit policies: who may do what, what may enter long-term memory, how conflicts are represented.

Everything here is deterministic code (testable); none of it is delegated to a prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.errors import PolicyViolation
from app.harness.injection import scan_text
from app.harness.logging import contains_secret

# ---------------------------------------------------------------------------------------------
# Capability matrix
# ---------------------------------------------------------------------------------------------
WORKFLOW_CAPABILITIES: dict[str, frozenset[str]] = {
    # A. ingestion: reads/fetches sources, writes evidence+findings, retains validated findings. Never edits summaries.
    "ingestion": frozenset({"fetch_url", "read_sources", "write_sources", "write_findings", "write_memory", "queue_followups"}),
    # B. QA: read-only on sources; recalls memory; may write memory ONLY in RECORD_USEFUL_EXPERIENCE (stage-restricted below).
    "qa": frozenset({"read_sources", "recall_memory", "write_memory"}),
    # C. synthesis: relationships between validated findings; may retain validated relationships.
    "synthesis": frozenset({"read_sources", "write_relationships", "recall_memory", "write_memory", "queue_followups"}),
    # D. summary: the ONLY workflow allowed to modify the living summary.
    "summary": frozenset({"read_sources", "recall_memory", "modify_summary", "queue_followups"}),
    # E. memory retention / consolidation: the only one allowed to consolidate (mental model).
    "memory": frozenset({"read_sources", "recall_memory", "write_memory", "consolidate_memory"}),
    # F. evaluation: read-only except in its own sandboxed workspaces.
    "evaluation": frozenset({"read_sources", "recall_memory"}),
}

# capability -> {(workflow, stage)} pairs where it is allowed; omitted capabilities are allowed in any stage of a permitted workflow
STAGE_RESTRICTED: dict[tuple[str, str], frozenset[str]] = {
    ("qa", "write_memory"): frozenset({"RECORD_USEFUL_EXPERIENCE"}),
    ("summary", "modify_summary"): frozenset({"PERSIST_VERSION"}),
    ("summary", "queue_followups"): frozenset({"QUEUE_CONSOLIDATION"}),
    ("ingestion", "write_memory"): frozenset({"RETAIN_USEFUL_MEMORY"}),
    ("synthesis", "write_memory"): frozenset({"RETAIN_RELATIONSHIP_MEMORY"}),
    ("memory", "write_memory"): frozenset({"ENSURE_BANK", "RETAIN_PENDING"}),
    ("memory", "consolidate_memory"): frozenset({"CONSOLIDATE"}),
    ("synthesis", "write_relationships"): frozenset({"PERSIST_RELATIONSHIPS"}),
    ("synthesis", "queue_followups"): frozenset({"COMPLETE"}),
    ("ingestion", "write_findings"): frozenset({"EXTRACT_RESEARCH_FINDINGS", "VALIDATE_FINDINGS", "DEDUPLICATE", "CHUNK_AND_INDEX"}),
}


def require(workflow: str, stage: str, capability: str) -> None:
    allowed = WORKFLOW_CAPABILITIES.get(workflow, frozenset())
    if capability not in allowed:
        raise PolicyViolation(f"workflow '{workflow}' may not use capability '{capability}'")
    stages = STAGE_RESTRICTED.get((workflow, capability))
    if stages is not None and stage not in stages:
        raise PolicyViolation(f"capability '{capability}' is not permitted in stage '{stage}' of workflow '{workflow}'")


# ---------------------------------------------------------------------------------------------
# Memory retention policy
# ---------------------------------------------------------------------------------------------
RETAINABLE_KINDS = frozenset({"finding", "definition", "method", "result", "limitation", "open_question", "relationship", "user_context", "synthesis"})
RETAINABLE_EPISTEMIC = frozenset({"source_stated", "user_note", "inference", "uncertain"})
MIN_LEN, MAX_LEN = 20, 700


@dataclass
class RetentionDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def evaluate_retention(
    *, kind: str, statement: str, epistemic: str, has_provenance: bool, validated: bool, quote: str | None = None
) -> RetentionDecision:
    """What may enter Hindsight. Rejections are recorded (not silent) by the caller."""
    reasons: list[str] = []
    if kind not in RETAINABLE_KINDS:
        reasons.append(f"kind '{kind}' is not retainable")
    if epistemic not in RETAINABLE_EPISTEMIC:
        reasons.append(f"unknown epistemic label '{epistemic}'")
    if not validated:
        reasons.append("not validated")
    if not has_provenance:
        reasons.append("missing source provenance")
    if not (MIN_LEN <= len(statement.strip()) <= MAX_LEN):
        reasons.append("statement length outside policy bounds (raw chunks are never retained)")
    blob = statement + "\n" + (quote or "")
    if contains_secret(blob):
        reasons.append("contains credential-like text")
    flags = scan_text(blob)
    if flags:
        reasons.append("instruction-like text from a document: " + ",".join(flags))
    return RetentionDecision(not reasons, reasons)


# ---------------------------------------------------------------------------------------------
# Conflict representation
# ---------------------------------------------------------------------------------------------
PREDICATES = ("supports", "contradicts", "extends", "uses", "depends_on", "applies_to")
