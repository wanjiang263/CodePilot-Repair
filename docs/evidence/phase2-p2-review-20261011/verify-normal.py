"""Compare raw Phase 2 CLI bytes against the immutable prior evidence; never normalize."""

import gzip
import hashlib
import json
from pathlib import Path

directory = Path(__file__).resolve().parent
root = directory.parents[2]
previous = root / "docs/evidence/phase2-retrieval-eval-20261011"
last_round = root / "docs/evidence/phase2-index-failure-20261011"

before = gzip.decompress((previous / "retrieval-evaluation.json.gz").read_bytes())
after = (root / "artifacts/phase2/retrieval-evaluation.json").read_bytes()

recorded = (previous / "cli-retrieve.stdout").read_bytes()
last_recorded = (last_round / "cli-retrieve.stdout").read_bytes()
current = (directory / "cli-retrieve.stdout").read_bytes()

report = json.loads(after)
assert before == after, "evaluation report bytes changed"
assert recorded == last_recorded == current, "retrieve stdout bytes changed"
assert report["total_tasks"] == 12 and all(not row["failures"] for row in report["rows"])
assert all(
    json.loads((directory / f"cli-{name}.json").read_text(encoding="utf-8"))["exit_code"] == 0
    for name in ("retrieve", "eval")
)

data = {
    "evaluation_bytes_equal": True,
    "retrieve_stdout_bytes_equal": True,
    "before_sha256": hashlib.sha256(before).hexdigest(),
    "after_sha256": hashlib.sha256(after).hexdigest(),
    "retrieve_sha256": hashlib.sha256(current).hexdigest(),
    "tasks": report["total_tasks"],
    "row_failures": {row["name"]: len(row["failures"]) for row in report["rows"]},
    "metrics": [{"channel": row["name"], "metrics": row["metrics"]} for row in report["rows"]],
}
(directory / "normal-comparison.json").write_text(
    json.dumps(data, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({k: v for k, v in data.items() if k != "metrics"}, indent=2))
