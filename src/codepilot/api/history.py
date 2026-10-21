"""Read-only replay of completed SWE-bench experiment packages.

The catalog is an explicit allow-list in ``configs/benchmarks.yaml``. Nothing in
this module writes to a historical package: no report regeneration, no event
completion, no patch normalisation on disk. Missing stages stay missing.

Official SWE-bench verdicts come from the archived harness report. Internal
verification comes from the archived ``report.json``/``state_final.json``. The
two are intentionally separate fields all the way to the UI.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codepilot.api.reporting import build_report_view, load_json, usage_view
from codepilot.api.schemas import (
    BenchmarkView,
    HistoryView,
    IntegrityCheckView,
    OfficialVerdictView,
    ReportView,
)
from codepilot.api.security import PathScrubber
from codepilot.api.status import ui_status_for
from codepilot.config import PROJECT_ROOT, Settings

__all__ = ["HistoryCatalog", "HistoryEntry"]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,80}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,200}$")


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    bench_id: str
    label: str
    package: Path
    artifacts_root: Path
    run_dir: Path
    run_id: str
    instance_id: str
    repository: str
    model: str
    recorded_at: str | None
    note: str | None
    official_report: Path | None

    @property
    def state(self) -> dict[str, Any]:
        return load_json(self.run_dir / "state_final.json")

    @property
    def report(self) -> dict[str, Any]:
        return load_json(self.run_dir / "report.json")

    @property
    def official(self) -> dict[str, Any]:
        return load_json(self.official_report) if self.official_report else {}


class HistoryCatalog:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._entries = self._load()
        self._by_run = {entry.run_id: entry for entry in self._entries}

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._entries)

    def get(self, bench_id: str) -> HistoryEntry | None:
        return next((item for item in self._entries if item.bench_id == bench_id), None)

    def get_by_run(self, run_id: str) -> HistoryEntry | None:
        return self._by_run.get(run_id)

    def _load(self) -> list[HistoryEntry]:
        path = self.settings.resolved_history_catalog
        if not path.is_file():
            return []
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return []
        items = payload.get("benchmarks") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        entries: list[HistoryEntry] = []
        seen: set[str] = set()
        for raw in items:
            if not isinstance(raw, dict):
                continue
            entry = self._entry(raw)
            if entry is None or entry.bench_id in seen:
                continue
            seen.add(entry.bench_id)
            entries.append(entry)
        return sorted(entries, key=lambda item: item.recorded_at or "", reverse=True)

    def _entry(self, raw: dict[str, Any]) -> HistoryEntry | None:
        bench_id = str(raw.get("id") or "").strip()
        if not _ID.fullmatch(bench_id):
            return None
        package_raw = str(raw.get("package") or "").strip()
        if not package_raw:
            return None
        package = Path(package_raw)
        if not package.is_absolute():
            package = PROJECT_ROOT / package
        package = package.resolve()
        # The catalog is server-controlled configuration, not request input. It
        # may legitimately point at an archived artifact root outside the repo
        # (for example a mounted experiment disk). Relative entries still resolve
        # under the project root and the run directory must stay inside the
        # package.
        if not package.is_dir():
            return None
        artifacts = (package / str(raw.get("run_artifacts") or "run-artifacts")).resolve()
        if not artifacts.is_relative_to(package) or not artifacts.is_dir():
            return None
        run_dir = self._discover_run_dir(artifacts, raw)
        if run_dir is None:
            return None
        state = load_json(run_dir / "state_final.json")
        report = load_json(run_dir / "report.json")
        run_id = str(state.get("run_id") or report.get("run_id") or "")
        if not _RUN_ID.fullmatch(run_id):
            return None
        official = self._official_report(artifacts, raw)
        official_payload = load_json(official) if official else {}
        official_instance = _dict(official_payload.get("instance"))
        prediction = _dict(official_payload.get("prediction"))
        manifest = _dict(prediction.get("manifest"))
        instance = str(
            raw.get("instance_id")
            or official_instance.get("instance_id")
            or state.get("task_id")
            or ""
        )
        repository = str(raw.get("repository") or report.get("repository") or "")
        model = str(raw.get("model") or manifest.get("model_name_or_path") or "unknown")
        recorded_at = raw.get("recorded_at") or official_payload.get("generated_at")
        return HistoryEntry(
            bench_id=bench_id,
            label=str(raw.get("label") or bench_id),
            package=package,
            artifacts_root=artifacts,
            run_dir=run_dir,
            run_id=run_id,
            instance_id=instance,
            repository=repository,
            model=model,
            recorded_at=str(recorded_at) if recorded_at else None,
            note=str(raw["note"]) if raw.get("note") else None,
            official_report=official,
        )

    @staticmethod
    def _discover_run_dir(artifacts: Path, raw: dict[str, Any]) -> Path | None:
        explicit = raw.get("run_dir")
        if explicit:
            candidate = (artifacts / str(explicit)).resolve()
            if candidate.is_relative_to(artifacts) and candidate.is_dir():
                return candidate
            return None
        if (artifacts / "state_final.json").is_file() or (artifacts / "report.json").is_file():
            return artifacts
        runs = artifacts / "runs"
        if not runs.is_dir():
            return None
        for candidate in sorted(item for item in runs.iterdir() if item.is_dir()):
            if (candidate / "state_final.json").is_file():
                return candidate
        return None

    @staticmethod
    def _official_report(artifacts: Path, raw: dict[str, Any]) -> Path | None:
        explicit = raw.get("official_report")
        if explicit:
            candidate = (artifacts / str(explicit)).resolve()
            if candidate.is_relative_to(artifacts) and candidate.is_file():
                return candidate
        reports = artifacts / "reports"
        if reports.is_dir():
            candidates = sorted(reports.glob("*.json"))
            if candidates:
                return candidates[0]
        return None

    # ------------------------------------------------------------- responses
    def history_item(self, entry: HistoryEntry) -> HistoryView:
        state = entry.state
        status = str(state.get("status") or "unknown")
        stop_reason = state.get("stop_reason")
        official = self._official_view(entry)
        report = entry.report
        event_count = self.events_info(entry)["count"]
        return HistoryView(
            history_id=entry.bench_id,
            run_id=entry.run_id,
            label=entry.label,
            instance_id=entry.instance_id,
            repository=entry.repository,
            model=entry.model,
            recorded_at=entry.recorded_at,
            status=status,
            raw_status=status,
            ui_status=ui_status_for(status, str(stop_reason) if stop_reason else None),
            stop_reason=str(stop_reason) if stop_reason else None,
            official_verdict=official.verdict,
            verification_level=_as_str(report.get("verification_level")) or None,
            event_count=event_count,
            patch_available=(entry.run_dir / "patch_final.diff").is_file(),
            report_available=(entry.run_dir / "report.json").is_file(),
            note=entry.note,
        )

    def benchmark(self, entry: HistoryEntry) -> BenchmarkView:
        state = entry.state
        report = entry.report
        status = str(state.get("status") or report.get("status") or "unknown")
        stop_reason = state.get("stop_reason") or report.get("stop_reason")
        stop_text = str(stop_reason) if stop_reason else None
        official = self._official_view(entry)
        integrity, official_payload = self._integrity(entry)
        view = build_report_view(
            run_id=entry.run_id,
            task_id=str(state.get("task_id") or report.get("task_id") or ""),
            run_dir=entry.run_dir,
            report=report,
            state=state,
            status=status,
            stop_reason=stop_text,
            official=official,
            events_count=self.events_info(entry)["count"],
            readonly=True,
        )
        harness = _dict(official_payload.get("harness"))
        tests = view.tests
        return BenchmarkView(
            bench_id=entry.bench_id,
            label=entry.label,
            run_id=entry.run_id,
            instance_id=entry.instance_id,
            repository=entry.repository,
            model=entry.model,
            recorded_at=entry.recorded_at,
            status=status,
            raw_status=status,
            ui_status=view.ui_status,
            stop_reason=stop_text,
            official=official,
            internal=view.internal,
            usage=usage_view(state.get("usage") if isinstance(state.get("usage"), dict) else {}),
            budget=view.budget,
            tests=tests,
            review=view.review,
            harness={
                "decision": harness.get("decision"),
                "exit_code": harness.get("exit_code"),
                "timed_out": harness.get("timed_out"),
                "runtime_seconds": harness.get("runtime_seconds"),
                "error_kind": harness.get("error_kind"),
                "verdicts": harness.get("verdicts") or [],
                "status": official_payload.get("status"),
            },
            integrity=integrity,
            artifacts={
                "files": view.artifacts,
                "readonly": True,
                "official_report": entry.official_report.name if entry.official_report else None,
            },
            note=entry.note,
            readonly=True,
        )

    def report(self, entry: HistoryEntry) -> ReportView:
        state = entry.state
        report = entry.report
        status = str(state.get("status") or report.get("status") or "unknown")
        stop_reason = state.get("stop_reason") or report.get("stop_reason")
        scrubber = self._scrubber(entry, absolute=False)
        view = build_report_view(
            run_id=entry.run_id,
            task_id=str(state.get("task_id") or report.get("task_id") or ""),
            run_dir=entry.run_dir,
            report=report,
            state=state,
            status=status,
            stop_reason=str(stop_reason) if stop_reason else None,
            official=self._official_view(entry),
            events_count=self.events_info(entry)["count"],
            readonly=True,
        )
        payload = view.model_dump(mode="json")
        payload["markdown"] = scrubber.text(str(payload.get("markdown") or ""))
        payload["raw"] = {}
        return ReportView.model_validate(payload)

    def events(self, entry: HistoryEntry) -> list[dict[str, Any]]:
        scrubber = self._scrubber(entry)
        path = entry.run_dir / "events.jsonl"
        events: list[dict[str, Any]] = []
        seen: set[int] = set()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            seq = payload.get("seq")
            if not isinstance(seq, int) or seq in seen:
                continue
            seen.add(seq)
            payload.setdefault("run_id", entry.run_id)
            events.append(scrubber.payload(payload))
        return sorted(events, key=lambda item: int(item["seq"]))

    def events_info(self, entry: HistoryEntry) -> dict[str, Any]:
        path = entry.run_dir / "events.jsonl"
        count = 0
        last = 0
        sequential = True
        first_run_id = ""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {"count": 0, "last_seq": 0, "sequential": False, "first_run_id": ""}
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                sequential = False
                continue
            if not isinstance(payload, dict):
                sequential = False
                continue
            seq = payload.get("seq")
            if not isinstance(seq, int):
                sequential = False
                continue
            if count == 0:
                first_run_id = str(payload.get("run_id") or "")
                sequential = sequential and seq == 1
            elif seq != last + 1:
                sequential = False
            count += 1
            last = seq
        return {
            "count": count,
            "last_seq": last,
            "sequential": sequential,
            "first_run_id": first_run_id,
        }

    def integrity(self, entry: HistoryEntry) -> list[IntegrityCheckView]:
        return self._integrity(entry)[0]

    def official_view(self, entry: HistoryEntry) -> OfficialVerdictView:
        return self._official_view(entry)

    def events_list(self, entry: HistoryEntry) -> list[dict[str, Any]]:
        return self.events(entry)

    def patch_text(self, entry: HistoryEntry) -> str:
        try:
            return (entry.run_dir / "patch_final.diff").read_text(encoding="utf-8")
        except OSError:
            return ""

    def _official_view(self, entry: HistoryEntry) -> OfficialVerdictView:
        official = entry.official
        if not official:
            return OfficialVerdictView(
                recorded=False,
                verdict=None,
                resolved=None,
                status=None,
                harness_status="NOT RECORDED",
                authoritative=True,
                source=None,
                note=entry.note or "No archived official harness report was found.",
            )
        harness = _dict(official.get("harness"))
        verdicts = harness.get("verdicts") if isinstance(harness.get("verdicts"), list) else []
        first = verdicts[0] if verdicts and isinstance(verdicts[0], dict) else {}
        comparison = _dict(official.get("m2_vs_official"))
        verdict = first.get("verdict") or comparison.get("official")
        resolved = first.get("resolved")
        if resolved is None and isinstance(verdict, str):
            resolved = verdict.upper() == "RESOLVED"
        return OfficialVerdictView(
            recorded=True,
            verdict=str(verdict) if verdict is not None else None,
            resolved=bool(resolved) if resolved is not None else None,
            status=_as_str(official.get("status")) or None,
            harness_status=_as_str(harness.get("decision")) or "NOT RECORDED",
            authoritative=bool(comparison.get("official_authoritative", True)),
            source=entry.official_report.name if entry.official_report else None,
            note=_as_str(comparison.get("note")) or entry.note,
        )

    def _integrity(self, entry: HistoryEntry) -> tuple[list[IntegrityCheckView], dict[str, Any]]:
        state = entry.state
        report = entry.report
        official = entry.official
        events = self.events_info(entry)
        patch_text = self.patch_text(entry)
        patch_sha = hashlib.sha256(patch_text.encode("utf-8")).hexdigest() if patch_text else ""
        state_patch_sha = hashlib.sha256(
            str(state.get("current_patch") or "").encode("utf-8")
        ).hexdigest()
        prediction_sha = _dict(official.get("prediction")).get("patch_sha256")
        required = [
            entry.run_dir / name
            for name in (
                "state_final.json",
                "report.json",
                "events.jsonl",
                "patch_final.diff",
            )
        ]
        checks = [
            IntegrityCheckView(
                name="required-artifacts",
                ok=all(path.is_file() for path in required),
                detail=", ".join(
                    f"{path.name}={'present' if path.is_file() else 'missing'}" for path in required
                ),
            ),
            IntegrityCheckView(
                name="run-id-consistency",
                ok=(
                    str(state.get("run_id") or "") == entry.run_id
                    and str(report.get("run_id") or "") == entry.run_id
                    and (not official or str(official.get("run_id") or "") == entry.run_id)
                ),
                detail=(
                    f"state={state.get('run_id')}, report={report.get('run_id')}, "
                    f"official={official.get('run_id') or 'n/a'}, catalog={entry.run_id}"
                ),
            ),
            IntegrityCheckView(
                name="event-sequence",
                ok=bool(events["sequential"]) and events["count"] > 0,
                detail=(
                    f"count={events['count']}, last_seq={events['last_seq']}, "
                    f"sequential={events['sequential']}"
                ),
            ),
            IntegrityCheckView(
                name="patch-identity",
                ok=bool(patch_sha)
                and patch_sha == state_patch_sha
                and (prediction_sha is None or prediction_sha == state_patch_sha),
                detail=(
                    f"patch_file={patch_sha[:16]}, state={state_patch_sha[:16]}, "
                    f"prediction={prediction_sha[:16] if isinstance(prediction_sha, str) else 'n/a'}"
                ),
            ),
            IntegrityCheckView(
                name="official-harness",
                ok=bool(official)
                and str(_dict(official.get("harness")).get("decision") or "") == "EXECUTED"
                and _dict(official.get("harness")).get("exit_code") == 0,
                detail=(
                    "archive present"
                    if official
                    else "No archived official harness report; NOT RUN is preserved."
                ),
            ),
        ]
        return checks, official

    def _scrubber(self, entry: HistoryEntry, *, absolute: bool = True) -> PathScrubber:
        return PathScrubber(
            self.settings,
            extra_roots={
                str(entry.package): "<history-package>",
                "/root/cp-run": "<history-root>",
                "/root/swebench-venv": "<harness-env>",
                "/workspace": "<workspace>",
            },
            scrub_absolute_paths=absolute,
        )


def _as_str(value: Any) -> str:
    return str(value) if value is not None else ""


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
