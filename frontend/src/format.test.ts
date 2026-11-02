import { errorMessage } from "./api";
import { citationLabel, splitAroundQuote } from "./format";

const errorMessageFixture = (body: unknown) => errorMessage(body, "fallback");

describe("splitAroundQuote", () => {
  it("splits around a verbatim quote", () => {
    expect(splitAroundQuote("alpha beta gamma", "beta")).toEqual(["alpha ", "beta", " gamma"]);
  });
  it("tolerates whitespace differences", () => {
    const r = splitAroundQuote("alpha  beta\ngamma", "beta gamma");
    expect(r?.[1]).toBe("beta gamma");
  });
  it("returns null when the quote is not in the text (never fabricates a match)", () => {
    expect(splitAroundQuote("alpha beta", "delta")).toBeNull();
    expect(splitAroundQuote("alpha beta", "")).toBeNull();
  });
});

describe("citationLabel", () => {
  it("prefers page, then section, and truncates long titles", () => {
    expect(citationLabel({ source_title: "Paper", page: 3 })).toBe("Paper, p.3");
    expect(citationLabel({ source_title: "Paper", page: null, section: "Intro" })).toBe("Paper, Intro");
    expect(citationLabel({ source_title: "x".repeat(60), page: 1 })).toMatch(/^x{36}…, p\.1$/);
  });
});

describe("errorMessage", () => {
  it("reads the app error envelope and FastAPI detail shapes", () => {
    expect(errorMessageFixture({ error: { code: "bad", message: "nope" } })).toEqual({ code: "bad", message: "nope" });
    expect(errorMessageFixture({ detail: { code: "qa_failed", message: "boom" } })).toEqual({ code: "qa_failed", message: "boom" });
    expect(errorMessageFixture({ detail: [{ msg: "field required" }] }).message).toBe("field required");
    expect(errorMessageFixture(null).message).toBe("fallback");
  });
});
