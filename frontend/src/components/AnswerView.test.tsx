import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, vi } from "vitest";
import { AnswerView } from "./AnswerView";
import { PassageViewerProvider } from "./Evidence";
import type { Answer } from "../types";

const base: Answer = {
  mode: "supported",
  answer: "",
  claims: [
    {
      id: "c1", text: "Agents plan with tools.", kind: "source_stated", memory_refs: [], support: 0.91, status: "supported", notes: [],
      citations: [{ passage_id: "psg_1", source_id: "src_1", source_title: "Paper A", source_kind: "pdf", page: 2, section: null, quote: "plan with tools", verified: true }],
    },
    { id: "c2", text: "So memory helps.", kind: "inference", citations: [], memory_refs: [], support: null, status: "flagged", notes: [] },
  ],
  conflicts: [], memory_used: [], warnings: [], plan: { intent: "factual", rationale: ["why"] },
  generation_mode: "extractive", evidence_considered: 4, dropped_claims: 1,
};

function renderAnswer(a: Answer) {
  return render(
    <MemoryRouter>
      <PassageViewerProvider ws="ws_1">
        <AnswerView answer={a} ws="ws_1" executionId="exe_1" />
      </PassageViewerProvider>
    </MemoryRouter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("AnswerView honesty", () => {
  it("says it is extractive when no LLM produced the answer", () => {
    renderAnswer(base);
    expect(screen.getByText(/extractive \(no LLM\)/)).toBeInTheDocument();
  });

  it("labels inferences and flags unchecked claims instead of presenting them as quotes", () => {
    renderAnswer(base);
    expect(screen.getByText("inference")).toBeInTheDocument();
    expect(screen.getByText("flagged by checker")).toBeInTheDocument();
    expect(screen.getByText(/1 claim\(s\) rejected by validation/)).toBeInTheDocument();
  });

  it("opens the stored passage with the cited quote highlighted when a citation is clicked", async () => {
    const passage = {
      id: "psg_1", source_id: "src_1", source_title: "Paper A", source_kind: "pdf", text: "Modern agents plan with tools and act.",
      page_start: 2, page_end: 2, section: null, char_start: 0, char_end: 38, suspicious: false, flags: [],
    };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => JSON.stringify(passage) });
    vi.stubGlobal("fetch", fetchMock);
    renderAnswer(base);
    fireEvent.click(screen.getByRole("button", { name: /Paper A, p\.2/ }));
    const mark = await screen.findByText("plan with tools", { selector: "mark" });
    expect(mark).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/workspaces/ws_1/passages/psg_1", expect.anything());
  });

  it("refuses to guess when evidence is insufficient", () => {
    renderAnswer({ ...base, mode: "insufficient", claims: [], dropped_claims: 0 });
    expect(screen.getByText(/won't guess/)).toBeInTheDocument();
  });
});
