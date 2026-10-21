export type View =
  | { name: "new" }
  | { name: "session"; taskId: string }
  | { name: "replay"; benchId: string }
  | { name: "history" }
  | { name: "benchmarks" }
  | { name: "benchmark"; benchId: string }
  | { name: "settings" };

export function parseView(hash: string): View {
  const path = hash.replace(/^#\/?/, "").split("?")[0];
  const [head, ...rest] = path.split("/").filter(Boolean);
  const value = rest.length ? decodeURIComponent(rest.join("/")) : "";
  if (head === "task" && value) return { name: "session", taskId: value };
  if (head === "replay" && value) return { name: "replay", benchId: value };
  if (head === "history") return { name: "history" };
  if (head === "benchmarks") return { name: "benchmarks" };
  if (head === "benchmark" && value) return { name: "benchmark", benchId: value };
  if (head === "settings") return { name: "settings" };
  return { name: "new" };
}

export function toHash(view: View): string {
  switch (view.name) {
    case "session":
      return `#/task/${encodeURIComponent(view.taskId)}`;
    case "replay":
      return `#/replay/${encodeURIComponent(view.benchId)}`;
    case "benchmark":
      return `#/benchmark/${encodeURIComponent(view.benchId)}`;
    case "history":
      return "#/history";
    case "benchmarks":
      return "#/benchmarks";
    case "settings":
      return "#/settings";
    default:
      return "#/new";
  }
}

export function viewKey(view: View): string {
  switch (view.name) {
    case "session":
      return `session:${view.taskId}`;
    case "replay":
      return `replay:${view.benchId}`;
    case "benchmark":
      return `benchmark:${view.benchId}`;
    default:
      return view.name;
  }
}
