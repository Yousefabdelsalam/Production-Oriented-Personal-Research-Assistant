import { useState } from "react";
import { Link } from "react-router-dom";
import { KIND_LABEL, MODE_LABEL } from "../format";
import type { Answer } from "../types";
import { CitationChip, usePassageViewer } from "./Evidence";
import { Chip, Notice } from "./ui";

const MODE_TONE: Record<string, string> = { supported: "good", partial: "warn", conflicting: "bad", insufficient: "neutral" };
const KIND_TONE: Record<string, string> = { source_stated: "info", user_note: "accent", inference: "warn", uncertain: "bad" };

export function ModeBadge({ mode }: { mode: string }) {
  return <Chip tone={MODE_TONE[mode] ?? "neutral"}>{MODE_LABEL[mode] ?? mode}</Chip>;
}

export function AnswerView({ answer, ws, executionId }: { answer: Answer; ws: string; executionId?: string | null }) {
  const [showMem, setShowMem] = useState(false);
  const [showPlan, setShowPlan] = useState(false);
  const viewer = usePassageViewer();
  return (
    <div className="answer stack">
      <div className="row gap wrap">
        <ModeBadge mode={answer.mode} />
        <Chip tone={answer.generation_mode === "llm" ? "accent" : "neutral"} title="How this answer was produced">
          {answer.generation_mode === "llm" ? "LLM-drafted · validated" : "extractive (no LLM) · verbatim sentences"}
        </Chip>
        {answer.plan.intent && <Chip title={(answer.plan.rationale ?? []).join(" · ")}>intent: {answer.plan.intent.replace("_", " ")}</Chip>}
        {answer.dropped_claims > 0 && <Chip tone="warn" title="Claims the validator rejected (unsupported or unverifiable citation)">{answer.dropped_claims} claim(s) rejected by validation</Chip>}
      </div>

      {answer.mode === "insufficient" && (
        <Notice tone="warn">
          {answer.claims.length === 0 && "The workspace does not contain enough evidence to answer this. I won't guess — try adding sources or rephrasing."}
        </Notice>
      )}

      {answer.claims.length > 0 && (
        <ol className="claims">
          {answer.claims.map((c) => (
            <li key={c.id} className={`claim ${c.status === "flagged" ? "flagged" : ""}`}>
              <div>{c.text}</div>
              <div className="row gap wrap small">
                <Chip tone={KIND_TONE[c.kind]} title={c.kind === "inference" ? "Not stated by any source: an AI conclusion" : undefined}>{KIND_LABEL[c.kind] ?? c.kind}</Chip>
                {c.citations.map((ci, i) => (
                  <CitationChip key={i} c={ci} />
                ))}
                {c.support != null && <span className="muted">support {Math.round(c.support * 100)}%</span>}
                {c.status === "flagged" && <Chip tone="warn">flagged by checker</Chip>}
              </div>
            </li>
          ))}
        </ol>
      )}

      {answer.conflicts.map((cf, i) => (
        <Notice key={i} tone="bad">
          <div className="strong">Conflicting evidence {cf.status !== "supported" && <Chip tone="warn">unverified hypothesis</Chip>}</div>
          <div className="small">{cf.description}</div>
          <div className="sides">
            {cf.sides.map((s, j) => (
              <div key={j} className="side">
                <div className="small strong">
                  {s.source_title}
                  {s.page ? `, p.${s.page}` : ""} {!s.cited && <Chip tone="neutral">not cited above</Chip>}
                </div>
                <div className="small">“{s.text}”</div>
                <button className="btn small" onClick={() => viewer.open(s.passage_id, s.text)}>
                  Open passage
                </button>
              </div>
            ))}
          </div>
        </Notice>
      ))}

      {answer.warnings.map((w, i) => (
        <Notice key={i} tone="warn">
          {w}
        </Notice>
      ))}

      <div className="row gap wrap small">
        {answer.memory_used.length > 0 && (
          <button className="btn small" onClick={() => setShowMem((v) => !v)}>
            {showMem ? "Hide" : "Show"} recalled memory ({answer.memory_used.length})
          </button>
        )}
        <button className="btn small" onClick={() => setShowPlan((v) => !v)}>
          {showPlan ? "Hide" : "Why these sources?"}
        </button>
        {executionId && (
          <Link className="btn small" to={`/w/${ws}/inspector?exec=${executionId}`}>
            Inspect execution
          </Link>
        )}
      </div>
      {showMem && (
        <div className="memlist">
          <Notice tone="info">Recalled from Hindsight. Memory is learned context, <strong>not evidence</strong>: claims above are backed by the cited source passages.</Notice>
          {answer.memory_used.map((m) => (
            <div key={m.id} className="mem small">
              {m.role === "discrepancy" && <Chip tone="bad">disagrees with the source on figures</Chip>} {m.text}
            </div>
          ))}
        </div>
      )}
      {showPlan && (
        <div className="small muted">
          <ul>{(answer.plan.rationale ?? []).map((r, i) => <li key={i}>{r}</li>)}</ul>
          Evidence passages considered: {answer.evidence_considered}.
        </div>
      )}
    </div>
  );
}
