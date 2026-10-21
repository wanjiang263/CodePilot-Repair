import { describe, expect, it } from "vitest";

import { parseDiff, toSplitRows, tokenizeLine } from "./diff";

const DIFF = `diff --git a/src/calc.py b/src/calc.py
index 1111111..2222222 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,3 +1,4 @@
 def add(a, b):
-    return a + b - 1
+    return a + b
+    # exact sum
`;

describe("parseDiff", () => {
  it("tracks old and new line numbers", () => {
    const lines = parseDiff(DIFF);
    const context = lines.find((line) => line.kind === "ctx");
    expect(context?.oldNo).toBe(1);
    expect(context?.newNo).toBe(1);
    const removed = lines.find((line) => line.kind === "del");
    expect(removed?.oldNo).toBe(2);
    const added = lines.find((line) => line.kind === "add");
    expect(added?.newNo).toBe(2);
  });

  it("pairs deletions and additions in split rows", () => {
    const rows = toSplitRows(parseDiff(DIFF));
    const pair = rows.find((row) => row.left?.kind === "del" && row.right?.kind === "add");
    expect(pair).toBeTruthy();
    expect(pair?.left?.text).toContain("a + b - 1");
    expect(pair?.right?.text).toContain("return a + b");
  });

  it("does not invent a diff for empty input", () => {
    expect(parseDiff("")).toEqual([]);
    expect(toSplitRows([])).toEqual([]);
  });
});

describe("tokenizeLine", () => {
  it("marks keywords, strings, comments and numbers", () => {
    const tokens = tokenizeLine('def add(a, b=2):  # comment "quoted"');
    expect(tokens.some((token) => token.className === "tok-keyword" && token.text === "def")).toBe(true);
    expect(tokens.some((token) => token.className === "tok-number" && token.text === "2")).toBe(true);
    expect(tokens.some((token) => token.className === "tok-comment")).toBe(true);
  });

  it("does not treat a hash inside a string as a comment", () => {
    const tokens = tokenizeLine('value = "# not a comment"');
    expect(tokens.find((token) => token.className === "tok-string")?.text).toContain("# not a comment");
    expect(tokens.some((token) => token.className === "tok-comment")).toBe(false);
  });
});
