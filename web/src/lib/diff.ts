export type DiffKind = "add" | "del" | "ctx" | "hunk" | "meta";

export interface DiffLine {
  kind: DiffKind;
  text: string;
  oldNo: number | null;
  newNo: number | null;
}

export interface SplitRow {
  left: DiffLine | null;
  right: DiffLine | null;
  meta: DiffLine | null;
}

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/;

export function parseDiff(diff: string): DiffLine[] {
  if (!diff.trim()) return [];
  const output: DiffLine[] = [];
  let oldNo: number | null = null;
  let newNo: number | null = null;
  let inHunk = false;
  for (const raw of diff.replace(/\r\n/g, "\n").split("\n")) {
    if (raw.startsWith("diff --git ")) {
      inHunk = false;
      output.push({ kind: "meta", text: raw, oldNo: null, newNo: null });
      continue;
    }
    if (
      raw.startsWith("index ") ||
      raw.startsWith("--- ") ||
      raw.startsWith("+++ ") ||
      raw.startsWith("new file mode") ||
      raw.startsWith("deleted file mode") ||
      raw.startsWith("old mode") ||
      raw.startsWith("new mode") ||
      raw.startsWith("similarity index") ||
      raw.startsWith("rename ")
    ) {
      output.push({ kind: "meta", text: raw, oldNo: null, newNo: null });
      continue;
    }
    const hunk = HUNK.exec(raw);
    if (hunk) {
      inHunk = true;
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[2]);
      output.push({ kind: "hunk", text: raw, oldNo: null, newNo: null });
      continue;
    }
    if (!inHunk) {
      output.push({ kind: "meta", text: raw, oldNo: null, newNo: null });
      continue;
    }
    if (raw.startsWith("+")) {
      output.push({ kind: "add", text: raw.slice(1), oldNo: null, newNo });
      newNo = (newNo ?? 0) + 1;
    } else if (raw.startsWith("-")) {
      output.push({ kind: "del", text: raw.slice(1), oldNo, newNo: null });
      oldNo = (oldNo ?? 0) + 1;
    } else if (raw.startsWith(" ")) {
      output.push({ kind: "ctx", text: raw.slice(1), oldNo, newNo });
      oldNo = (oldNo ?? 0) + 1;
      newNo = (newNo ?? 0) + 1;
    } else if (raw === "" || raw.startsWith("\\ No newline")) {
      output.push({ kind: "meta", text: raw, oldNo: null, newNo: null });
    } else {
      output.push({ kind: "ctx", text: raw, oldNo, newNo });
      oldNo = (oldNo ?? 0) + 1;
      newNo = (newNo ?? 0) + 1;
    }
  }
  return output;
}

export function toSplitRows(lines: DiffLine[]): SplitRow[] {
  const rows: SplitRow[] = [];
  let deletions: DiffLine[] = [];
  let additions: DiffLine[] = [];

  const flush = () => {
    const count = Math.max(deletions.length, additions.length);
    for (let index = 0; index < count; index += 1) {
      rows.push({
        left: deletions[index] ?? null,
        right: additions[index] ?? null,
        meta: null,
      });
    }
    deletions = [];
    additions = [];
  };

  for (const line of lines) {
    if (line.kind === "del") {
      deletions.push(line);
      continue;
    }
    if (line.kind === "add") {
      additions.push(line);
      continue;
    }
    flush();
    if (line.kind === "ctx") {
      rows.push({ left: line, right: line, meta: null });
    } else {
      rows.push({ left: null, right: null, meta: line });
    }
  }
  flush();
  return rows;
}

export type TokenClass =
  | "plain"
  | "tok-keyword"
  | "tok-string"
  | "tok-comment"
  | "tok-number"
  | "tok-fn";

export interface Token {
  text: string;
  className: TokenClass;
}

const KEYWORDS = new Set(
  `False None True and as assert async await break class continue def del elif else except
  finally for from global if import in is lambda nonlocal not or pass raise return try while
  with yield match case`.split(/\s+/),
);

const TOKEN = /(#[^\n]*)|("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)/g;

export function tokenizeLine(line: string): Token[] {
  const tokens: Token[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  TOKEN.lastIndex = 0;
  while ((match = TOKEN.exec(line)) !== null) {
    if (match.index > last) {
      tokens.push({ text: line.slice(last, match.index), className: "plain" });
    }
    const value = match[0];
    let className: TokenClass = "plain";
    if (match[1]) className = "tok-comment";
    else if (match[2]) className = "tok-string";
    else if (match[3]) className = "tok-number";
    else if (match[4] && KEYWORDS.has(value)) className = "tok-keyword";
    else if (match[4] && /^[A-Za-z_]/.test(value) && line[match.index + value.length] === "(") {
      className = "tok-fn";
    }
    tokens.push({ text: value, className });
    last = match.index + value.length;
  }
  if (last < line.length) tokens.push({ text: line.slice(last), className: "plain" });
  return tokens;
}
