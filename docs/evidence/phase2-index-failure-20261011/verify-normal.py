"""Compare raw success bytes against immutable pre-fix evidence; never normalize."""

import gzip
import hashlib
import json
from pathlib import Path

directory = Path(__file__).resolve().parent
root = directory.parents[2]
previous = root / "docs/evidence/phase2-retrieval-eval-20261011"
before = gzip.decompress((previous / "retrieval-evaluation.json.gz").read_bytes())
after = (root / "artifacts/phase2/retrieval-evaluation.json").read_bytes()
retrieve_equal = (previous / "cli-retrieve.stdout").read_bytes() == (directory / "cli-retrieve.stdout").read_bytes()
report = json.loads(after)
assert before == after and retrieve_equal
assert report["total_tasks"] == 12 and all(not row["failures"] for row in report["rows"])
assert all(json.loads((directory / f"cli-{name}.json").read_text(encoding="utf-8"))["exit_code"] == 0
           for name in ("retrieve", "eval"))
data = {
    "report_bytes_equal": True,
    "before_sha256": hashlib.sha256(before).hexdigest(),
    "after_sha256": hashlib.sha256(after).hexdigest(),
    "retrieve_stdout_bytes_equal": retrieve_equal,
    "tasks": report["total_tasks"],
    "row_failures": {row["name"]: len(row["failures"]) for row in report["rows"]},
    "metrics": [{"channel": row["name"], "metrics": row["metrics"]} for row in report["rows"]],
}
(directory / "normal-comparison.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(json.dumps({k: v for k, v in data.items() if k != "metrics"}, indent=2))
