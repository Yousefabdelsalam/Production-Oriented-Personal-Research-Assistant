import { render, screen } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, vi } from "vitest";
import { Chat } from "./Chat";

function renderChat() {
  return render(
    <MemoryRouter>
      <Routes>
        <Route element={<Outlet context={{ ws: "ws_1" }} />}>
          <Route index element={<Chat />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Chat page", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, text: async () => "[]" }));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  });

  it("renders its empty state", async () => {
    renderChat();
    expect(await screen.findByText("Ask a question about this workspace")).toBeInTheDocument();
  });

  // Regression: current Chrome returns a Promise from scrollIntoView(). An effect that returned it made React treat the
  // Promise as a cleanup function and crash the whole page ("TypeError: n is not a function") on the next re-render/unmount.
  it("survives browsers whose scrollIntoView returns a Promise", async () => {
    Element.prototype.scrollIntoView = vi.fn(() => Promise.resolve()) as unknown as Element["scrollIntoView"];
    const errors: unknown[] = [];
    const onError = (e: ErrorEvent) => {
      errors.push(e.error);
      e.preventDefault();
    };
    window.addEventListener("error", onError);
    const view = renderChat();
    await screen.findByText("Ask a question about this workspace");
    view.unmount(); // runs effect cleanups
    window.removeEventListener("error", onError);
    expect(errors).toEqual([]);
  });
});
