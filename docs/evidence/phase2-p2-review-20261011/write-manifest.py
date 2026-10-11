"""Hash every evidence file in this directory into manifest.json."""

import hashlib
import json
from pathlib import Path

directory = Path(__file__).resolve().parent
entries = sorted(path.name for path in directory.iterdir() if path.name != "manifest.json")
manifest = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in entries}
(directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"files": len(manifest)}, indent=2))
