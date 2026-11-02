# Evaluation

> Status: implemented. Run with `python -m evaldata.run` from `backend/` (exit 0 = all gates passed, 1 = a gate failed,
> 2 = inconclusive because some gates were skipped, e.g. Hindsight unreachable). Also available from the UI's Evaluation page.
> The last recorded run (extractive mode, live Hindsight 0.10.2, 2026-10-19) passed all 14 gates. Extractive-mode results say
> nothing about LLM answer quality.

## Dataset (`backend/evaldata/corpus.py`)
Small deterministic corpus of **synthetic** papers/notes (fictional, so expected answers are unambiguous):
questions, expected supporting sources, expected findings, a known contradiction pair, a prompt-injection document,
and a "new evidence" source for summary updates.

## Metrics and gates
| Metric | Definition | Gate |
|---|---|---|
| Retrieval relevance | recall@5 and MRR of expected source/passage | recall@5 ≥ 0.9 |
| Citation validity | fraction of citations resolving to real passages with verbatim quotes | = 1.0 |
| Groundedness | fraction of returned claims passing the support check | ≥ 0.9 |
| Conflict detection | known contradiction pair surfaced as conflicting/hypothesis | = 1.0 |
| Memory usefulness | later-session recall returns the expected retained fact (real Hindsight) | ≥ 0.8 |
| Summary update quality | new evidence ⇒ version bump; irrelevant source ⇒ no bump; prior items preserved | all pass |
| Injection resistance | malicious instructions not obeyed, not retained | = 1.0 |
| Isolation | zero cross-workspace evidence/memory leakage | = 1.0 |

Results are persisted as `EvaluationRun` rows and shown in the UI; a failed gate is surfaced, not hidden.
