"""Deterministic, advisory exploration signals; never a forced write/stop rule."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


def exploration_feedback(facts: dict[str, Any]) -> str:
    feedback = (
        "This query was already made. Reuse its evidence unless content changed."
        if facts.get("repeated_query")
        else ""
    )
    if facts.get("low_yield_streak", 0) >= 3:
        feedback += (
            " Limited new evidence for planned files (heuristic, not proof of irrelevance). "
            "State Hypothesis: <cause>, connect it to the public contract, and choose a minimal "
            "source edit when supported. If evidence is insufficient, state the missing fact "
            "and investigate a different scope. Necessary investigation remains allowed."
        )
    if facts.get("low_yield_streak", 0) >= 3:
        feedback += " Evidence gaps: " + json.dumps(
            facts.get("missing_facts", []), ensure_ascii=False
        )
        feedback += "; Next options: " + json.dumps(
            facts.get("next_options", []), ensure_ascii=False
        )
    return feedback.strip()


@dataclass
class ExplorationProgress:
    relevant_files: set[str] = field(default_factory=set)
    requests: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)
    hypothesis: str = ""
    modified: bool = False
    candidate_hash: str | None = None
    observed_files: set[str] = field(default_factory=set)
    low_yield: int = 0

    def observe(
        self, name: str, arguments: dict[str, Any], result: dict[str, Any], narration: str
    ) -> dict[str, Any]:
        request = json.dumps([name, arguments], sort_keys=True, ensure_ascii=False)
        repeated = request in self.requests
        self.requests.add(request)
        data = result.get("data") or {}
        new = set()
        if result.get("ok"):
            if name == "read_file" and data.get("path") in self.relevant_files:
                new.add(json.dumps([data.get("path"), data.get("content")], sort_keys=True))
            for match in data.get("matches", []):
                if match.get("path") in self.relevant_files:
                    new.add(json.dumps(match, sort_keys=True))
        self.observed_files.update(
            [str(data["path"])]
            if new and data.get("path")
            else [
                str(m["path"])
                for m in data.get("matches", [])
                if m.get("path") in self.relevant_files
            ]
        )
        new = {hashlib.sha256(item.encode()).hexdigest() for item in new} - self.evidence
        self.evidence.update(new)
        stated = re.search(r"(?:hypothesis|根因|假设)\s*[:：]\s*([^\n]{1,500})", narration, re.I)
        hypothesis = stated.group(1).strip() if stated else self.hypothesis
        changed = hypothesis != self.hypothesis
        self.hypothesis = hypothesis
        if name in {"edit_file", "create_file"} and result.get("ok"):
            self.modified = True
        readonly = name in {
            "read_file",
            "read_evidence",
            "search_code",
            "find_symbol",
            "get_callers",
            "get_callees",
            "get_related_tests",
            "get_git_diff",
        }
        self.low_yield = self.low_yield + 1 if readonly and not new and not changed else 0
        missing = sorted(self.relevant_files - self.observed_files)
        gaps = [f"inspect planned source: {path}" for path in missing[:8]]
        if not hypothesis:
            gaps.append("state a cause tied to the public contract")
        if not gaps:
            gaps.append(
                "identify an observation that distinguishes the current hypothesis from alternatives"
            )
        facts = {
            "known_evidence": sorted(self.observed_files)[:20],
            "missing_facts": gaps,
            "next_options": [
                "read the missing planned source",
                "query callers or adjacent module with a stated gap",
                "make a minimal change if contract and cause are supported",
            ],
            "repeated_query": repeated,
            "new_relevant_evidence": len(new),
            "relevance_basis": "planned_file_overlap",
            "hypothesis_changed": changed,
            "hypothesis": hypothesis,
            "has_modification": self.modified,
            "low_yield_streak": self.low_yield,
        }
        return {**facts, "feedback": exploration_feedback(facts)}

    def observe_candidate(self, identity: str) -> dict[str, Any]:
        previous = self.candidate_hash
        self.candidate_hash = identity
        self.modified = bool(identity)
        return {
            "has_modification": self.modified,
            "candidate_changed": previous is not None and previous != identity,
            "candidate_hash": identity,
            "previous_candidate_hash": previous,
            "modification_basis": "workspace_diff_content",
        }
