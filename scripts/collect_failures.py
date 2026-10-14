"""Collect existing project artifacts only; never run an input project's commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree

from codepilot.failure.parser import FailureParser
from codepilot.observability.redaction import redact_text
from codepilot.schemas.execution import ExecutionResult


def sanitize(text: str, root: Path) -> str:
    value = redact_text(text)
    for spelling in (str(root), root.as_posix(), str(root).replace("\\", "\\\\")):
        value = value.replace(spelling, "/fixture")
    # Host user and Python installation paths are not needed for failure semantics.
    return re.sub(
        r"(?i)[A-Z]:[\\/](?:Users[\\/][^\s<>\"']+|[^\s<>\"']*[\\/]Lib[\\/]site-packages)",
        "[runtime]",
        value,
    )


def collect(input_dir: Path, output_dir: Path, *, project_root: Path) -> dict[str, object]:
    root = project_root.resolve(strict=True)
    source = input_dir.resolve(strict=True)
    destination = output_dir.resolve()
    if not source.is_relative_to(root) or not destination.is_relative_to(root):
        raise ValueError("collection directories must belong to this project")
    if source == destination or destination.is_relative_to(source):
        raise ValueError("output directory must be outside the input tree")
    destination.mkdir(parents=True, exist_ok=True)
    collected = duplicates = errors = 0
    sources: list[str] = []
    for path in sorted(source.rglob("*")):
        if path.name in {"expected.json", "provenance.json"} or path.name.endswith(
            ".metadata.json"
        ):
            continue
        if path.suffix not in (".xml", ".json") or not path.is_file():
            continue
        try:
            if not path.resolve(strict=True).is_relative_to(source):
                raise ValueError("artifact symlink escapes input")
            if path.stat().st_size > 5_000_000:
                raise ValueError("artifact exceeds collection limit")
            value = sanitize(path.read_text(encoding="utf-8"), root)
            if path.suffix == ".xml":
                batch = FailureParser().parse_xml(value)
                xml = ElementTree.fromstring(value)
                for element in xml.iter():
                    for key in ("time", "timestamp", "hostname"):
                        element.attrib.pop(key, None)
                value = ElementTree.tostring(xml, encoding="unicode")
            else:
                result = ExecutionResult.model_validate_json(value)
                batch = FailureParser().parse_execution(result)
                value = result.model_dump_json()
            if not batch.failures and batch.execution_failure is None:
                continue
            digest = hashlib.sha256(value.encode()).hexdigest()
            target = destination / f"sample-{digest}{path.suffix}"
            sources.append(path.relative_to(root).as_posix())
            if target.exists():
                duplicates += 1
                continue
            target.write_text(value, encoding="utf-8")
            metadata = {
                "source": path.relative_to(root).as_posix(),
                "sanitized_sha256": digest,
                "failures": [f.model_dump(mode="json") for f in batch.failures],
                "execution_failure": batch.execution_failure.model_dump(mode="json")
                if batch.execution_failure
                else None,
            }
            target.with_suffix(".metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            collected += 1
        except (OSError, ValueError):
            errors += 1
    return {"collected": collected, "duplicates": duplicates, "errors": errors, "sources": sources}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            collect(args.input, args.output, project_root=Path(__file__).resolve().parents[1]),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
