"""Prompt-injection / unsafe-instruction detection for untrusted document text.

This is a *flagging* layer, not the defence. The defence is architectural:
documents are only ever passed to models inside delimited data blocks, models cannot call tools,
model output is schema-validated, and every write goes through harness policies.
Flags let us (1) exclude instruction-like sentences from findings and memory, and (2) show warnings in the UI.
"""
from __future__ import annotations

import re

_PATTERNS: dict[str, re.Pattern[str]] = {
    "override_instructions": re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your|the)\b[^.\n]{0,30}\b(instruction|prompt|rule|guideline|direction)s?", re.I),
    "role_hijack": re.compile(r"\b(you are now|from now on,? you|act as (an?|the)|pretend (to be|you are)|new (system )?instructions?:)", re.I),
    "system_prompt_probe": re.compile(r"\b(reveal|print|show|repeat|leak)\b[^.\n]{0,30}\b(system prompt|hidden prompt|your instructions|api[_ -]?key|secret|password)", re.I),
    "tool_or_command": re.compile(
        r"\b(run|execute|invoke|call)\b[^.\n]{0,30}\b(the following|this)?[^.\n]{0,15}\b(shell|command|script|code|tool|function|powershell|bash|terminal)\b|"
        r"\b(rm\s+-rf|curl\s+[^|\n]*\|\s*(sh|bash)|powershell\s+-|cmd\.exe|sudo\s|wget\s+http|os\.system|subprocess\.)", re.I),
    "memory_manipulation": re.compile(
        r"\b(store|save|retain|remember|write|add)\b[^.\n]{0,40}\b(to|in|into)\b[^.\n]{0,20}\b(long[- ]term )?(memory|hindsight|knowledge base)\b|"
        r"\bmark (this|these)\b[^.\n]{0,20}\b(as )?(verified|true|established)", re.I),
    "exfiltration": re.compile(r"\b(send|post|upload|exfiltrate|forward)\b[^.\n]{0,50}\b(https?://|to the (attacker|server)|email)", re.I),
    "markup_injection": re.compile(r"<\s*(script|iframe|img[^>]*onerror)|\bjavascript:|<\|(im_start|system|assistant)\|>|\[\[?\s*system\s*\]?\]", re.I),
    "fake_citation_demand": re.compile(r"\b(cite|always cite|must cite)\b[^.\n]{0,40}\b(this|as the only|exclusively)", re.I),
}


def scan_text(text: str) -> list[str]:
    """Return the names of triggered patterns (empty list = nothing suspicious)."""
    return [name for name, pat in _PATTERNS.items() if pat.search(text)]
