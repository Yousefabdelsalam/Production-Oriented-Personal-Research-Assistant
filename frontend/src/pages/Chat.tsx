import { FormEvent, useEffect, useRef, useState } from "react";
import { useOutletContext } from "react-router-dom";
import { api } from "../api";
import { AnswerView } from "../components/AnswerView";
import { Card, Empty, ErrorBox, Loading } from "../components/ui";
import { relTime } from "../format";
import { useAction, useAsync } from "../hooks";
import type { Turn } from "../types";

type Ctx = { ws: string };

export function Chat() {
  const { ws } = useOutletContext<Ctx>();
  const [cid, setCid] = useState<string | undefined>();
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [live, setLive] = useState<Turn[]>([]);
  const convs = useAsync(() => api.conversations(ws), [ws]);
  const turns = useAsync(() => (cid ? api.turns(ws, cid) : Promise.resolve([] as Turn[])), [ws, cid]);
  const ask = useAction((q: string, c?: string) => api.ask(ws, q, c));
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setCid(undefined);
    setLive([]);
  }, [ws]);
  useEffect(() => {
    // Block body on purpose: newer Chrome returns a Promise from scrollIntoView, and React would treat a returned value as a cleanup function.
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [turns.data, live.length, pending]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || ask.busy) return;
    setPending(q);
    setQuestion("");
    const r = await ask.run(q, cid);
    setPending(null);
    if (!r) {
      setQuestion(q); // keep the question so it can be retried
      return;
    }
    setLive((l) => [...l, { id: r.execution_id, question: q, answer: r.answer, execution_id: r.execution_id, created_at: new Date().toISOString() }]);
    if (!cid) {
      setCid(r.conversation_id);
      setLive([]);
    } else {
      turns.reload();
    }
    convs.reload();
  }

  const shown = cid ? [...(turns.data ?? []), ...live.filter((l) => !(turns.data ?? []).some((t) => t.execution_id === l.execution_id))] : live;

  return (
    <div className="chat">
      <Card title="Conversations" actions={<button className="btn small" onClick={() => { setCid(undefined); setLive([]); }}>New</button>} className="chat-side">
        {convs.loading && !convs.data && <Loading />}
        <ErrorBox error={convs.error} onRetry={convs.reload} />
        <ul className="list">
          {convs.data?.map((c) => (
            <li key={c.id} className={c.id === cid ? "listitem active" : "listitem"} tabIndex={0} onClick={() => { setCid(c.id); setLive([]); }} onKeyDown={(e) => e.key === "Enter" && setCid(c.id)}>
              <div>{c.title}</div>
              <div className="small muted">{relTime(c.created_at)}</div>
            </li>
          ))}
        </ul>
      </Card>

      <div className="chat-main stack">
        {shown.length === 0 && !pending && (
          <Empty title="Ask a question about this workspace">
            Answers cite stored source passages. If the evidence isn’t there, the assistant says so rather than guessing.
          </Empty>
        )}
        {turns.loading && cid && !turns.data && <Loading />}
        <ErrorBox error={turns.error} onRetry={turns.reload} />
        {shown.map((t) => (
          <div key={t.id} className="turn stack">
            <div className="q">{t.question}</div>
            <Card>
              <AnswerView answer={t.answer} ws={ws} executionId={t.execution_id} />
            </Card>
          </div>
        ))}
        {pending && (
          <div className="turn stack">
            <div className="q">{pending}</div>
            <Loading label="Retrieving evidence, drafting and checking citations…" />
          </div>
        )}
        <ErrorBox error={ask.error} />
        <div ref={endRef} />
        <form className="askbar row gap" onSubmit={submit}>
          <textarea
            aria-label="Question"
            rows={2}
            placeholder="e.g. How do the two papers differ on memory consolidation?"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <button className="btn primary" disabled={ask.busy || !question.trim()}>
            {ask.busy ? "Thinking…" : "Ask"}
          </button>
        </form>
      </div>
    </div>
  );
}
