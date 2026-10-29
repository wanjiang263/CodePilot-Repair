"""Task-local bounded cache for deterministic file reads and complete searches.

Content is rechecked, including negative searches and new files. Large workspaces
bypass search caching; the original search engine/fallback always remains usable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from codepilot.tools.base import Tool, ToolResponse, WorkspacePathPolicy

QUERY_VERSION = "content-query-v1"


class QueryCache:
    def __init__(self, policy: WorkspacePathPolicy) -> None:
        self.policy = policy
        self.entries: OrderedDict[str, ToolResponse] = OrderedDict()
        self.max_bytes = 8 * 1024 * 1024
        self.entry_bytes: dict[str, int] = {}
        self.bytes_used = 0
        self.bypassed_scopes: set[str] = set()
        self.read_ranges: OrderedDict[str, list[tuple[int, int]]] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.epoch = 0
        self.last_invalidation = "first_use"
        self.identities: OrderedDict[str, str | None] = OrderedDict()
        source = Path(__file__).parent
        self.version = hashlib.sha256(
            b"".join(
                (source / name).read_bytes()
                for name in ("query_cache.py", "read_tools.py", "base.py")
            )
        ).hexdigest()

    def invalidate(self, reason: str = "workspace_side_effect") -> None:
        self.entries.clear()
        self.entry_bytes.clear()
        self.read_ranges.clear()
        self.bytes_used = 0
        self.epoch += 1
        self.last_invalidation = reason

    def _content_identity(self, tool: Tool, args: dict[str, Any]) -> str | None:
        policy = self.policy
        digest = hashlib.sha256()
        if tool.name == "read_file":
            path = policy.resolve(str(args["path"]), must_exist=True)
            policy.verify_identity(path)
            if path.stat().st_size > 8 * 1024 * 1024:
                return None
            digest.update(str(path.stat().st_mode).encode())
            with path.open("rb") as handle:
                content = handle.read(8 * 1024 * 1024 + 1)
            if len(content) > 8 * 1024 * 1024:
                return None
            digest.update(content)
            return digest.hexdigest()
        # Without a trusted external-change revision, global queries use the original
        # engine directly. mtime/size are never sufficient for content identity.
        glob = str(args.get("path_glob") or "")
        prefix = re.split(r"[?*\[]", glob, maxsplit=1)[0]
        relative_dir = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        if (
            not relative_dir
            or relative_dir in self.bypassed_scopes
            or len(self.bypassed_scopes) >= 64
        ):
            return None
        root = policy.resolve_directory(relative_dir)
        if not root.is_dir() or root.is_symlink():
            return None
        git_dir = policy.root / ".git"
        if git_dir.is_file() or git_dir.is_symlink():
            return None
        started = time.monotonic()
        total = 0
        count = 0
        # Ancestor ignore changes can change matches in a scoped search.
        for parent in (root, *root.parents):
            if not parent.is_relative_to(policy.root):
                break
            for name in (".gitignore", ".ignore", ".rgignore"):
                ignore = parent / name
                if ignore.is_symlink():
                    return None
                digest.update(str(ignore).encode())
                digest.update(ignore.read_bytes() if ignore.exists() else b"missing")
        exclude = git_dir / "info" / "exclude"
        if exclude.is_symlink():
            return None
        digest.update(exclude.read_bytes() if exclude.exists() else b"missing")
        from codepilot.tools.read_tools import _is_denied_directory

        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                name
                for name in dirs
                if not _is_denied_directory(
                    (Path(directory) / name).relative_to(policy.root).as_posix(), policy
                )
            )
            for name in sorted(files):
                path = Path(directory) / name
                count += 1
                if count > 2000 or time.monotonic() - started > 0.05:
                    self.bypassed_scopes.add(relative_dir)
                    return None
                if path.is_symlink():
                    return None
                relative = path.relative_to(policy.root).as_posix()
                try:
                    policy.resolve(relative, must_exist=True)
                    stat = path.stat()
                except (OSError, ValueError):
                    # Permission transitions must alter the identity too.
                    digest.update(json.dumps([relative, "unreadable"]).encode())
                    continue
                total += stat.st_size
                if total > 1024 * 1024:
                    self.bypassed_scopes.add(relative_dir)
                    return None
                content = path.read_bytes()
                digest.update(json.dumps([relative, len(content), stat.st_mode]).encode())
                digest.update(content)
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    return None
        return digest.hexdigest()

    def execute(self, tool: Tool, arguments: dict[str, Any], role: str) -> ToolResponse:
        args = tool.input_model.model_validate(arguments).model_dump(mode="json")
        engine: Any = None
        if tool.name == "search_code":
            executable = shutil.which("rg")
            if executable:
                stat = Path(executable).stat()
                engine = [executable, stat.st_size, stat.st_mtime_ns]
            else:
                engine = "python_fallback"
        permissions = {
            "root": str(self.policy.root),
            "role": role,
            "deny_directories": sorted(self.policy.deny_directories),
            "deny_files": sorted(self.policy.deny_files),
        }
        identity_started = time.monotonic()
        try:
            identity = self._content_identity(tool, args)
        except (OSError, ValueError):
            identity = None
        identity_seconds = time.monotonic() - identity_started
        lookup_started = time.monotonic()
        request_key = hashlib.sha256(
            json.dumps(
                [
                    QUERY_VERSION,
                    self.version,
                    tool.name,
                    tool.spec().parameters,
                    args,
                    permissions,
                    engine,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        prior_identity = self.identities.get(request_key)
        reason = (
            "workspace_content_changed"
            if request_key in self.identities and prior_identity != identity
            else self.last_invalidation
        )
        self.identities[request_key] = identity
        if len(self.identities) > 64:
            self.identities.popitem(last=False)
        key = hashlib.sha256(
            json.dumps(
                [
                    QUERY_VERSION,
                    self.version,
                    engine,
                    tool.name,
                    tool.spec().parameters,
                    args,
                    permissions,
                    identity,
                    self.epoch,
                ],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        if identity is not None and key in self.entries:
            self.hits += 1
            response = self.entries[key].model_copy(deep=True)
            response.data["cache"] = {
                **response.data.get("cache", {}),
                "hit": True,
                "identity_seconds": identity_seconds,
                "lookup_seconds": time.monotonic() - lookup_started,
                "search_seconds": 0.0,
                "identity": key,
                "reason": "same_content_arguments_permissions_version",
            }
            self.entries.move_to_end(key)
            return self._annotate_read(response, tool.name, identity, permissions)
        self.misses += 1
        lookup_seconds = time.monotonic() - lookup_started
        search_started = time.monotonic()
        response = tool.execute(arguments)
        search_seconds = time.monotonic() - search_started
        kind = "error"
        if response.ok:
            kind = (
                "truncated"
                if response.data.get("truncated")
                or any(m.get("text_truncated") for m in response.data.get("matches", []))
                else "empty"
                if (response.data.get("matches") == [] or response.data.get("content") == "")
                else "complete"
            )
        elif response.error and response.error.code in {"not_found", "path_not_found"}:
            kind = "missing"
        reason = reason if identity else "identity_unavailable_or_scope_limit"
        response.data["cache"] = {
            "hit": False,
            "identity_seconds": identity_seconds,
            "lookup_seconds": lookup_seconds,
            "search_seconds": search_seconds,
            "identity": key,
            "reason": reason,
            "result_kind": kind,
        }
        # Timeout/truncation is not a complete negative. Failures are never sticky.
        if identity is not None and response.ok and kind != "truncated":
            verify_started = time.monotonic()
            try:
                stable = self._content_identity(tool, args) == identity
            except (OSError, ValueError):
                stable = False
            response.data["cache"]["identity_seconds"] += time.monotonic() - verify_started
            size = len(response.model_dump_json().encode())
            identity_cost = response.data["cache"]["identity_seconds"]
            useful = tool.name != "search_code" or identity_cost < max(0.005, search_seconds)
            if not useful:
                glob = str(args.get("path_glob") or "")
                prefix = re.split(r"[?*\[]", glob, maxsplit=1)[0]
                self.bypassed_scopes.add(prefix.rsplit("/", 1)[0])
                response.data["cache"]["reason"] = "identity_cost_exceeds_search"
            if stable and useful and size <= self.max_bytes // 4:
                while self.entries and (
                    len(self.entries) >= 64 or self.bytes_used + size > self.max_bytes
                ):
                    old, _ = self.entries.popitem(last=False)
                    self.bytes_used -= self.entry_bytes.pop(old)
                self.entry_bytes[key] = size
                self.bytes_used += size
                self.entries[key] = response.model_copy(deep=True)

        return self._annotate_read(response, tool.name, identity, permissions)

    def _annotate_read(
        self, response: ToolResponse, name: str, identity: str | None, permissions: dict[str, Any]
    ) -> ToolResponse:
        if name != "read_file" or identity is None or not response.ok:
            return response
        data = response.data
        key = hashlib.sha256(
            json.dumps([data.get("path"), identity, permissions], sort_keys=True).encode()
        ).hexdigest()
        start, end = int(data.get("start_line", 1)), int(data.get("end_line", 0))
        fresh = [(start, end)] if end >= start else []
        previous = self.read_ranges.get(key, [])
        for left, right in previous:
            remaining = []
            for lo, hi in fresh:
                if right < lo or left > hi:
                    remaining.append((lo, hi))
                else:
                    if lo < left:
                        remaining.append((lo, left - 1))
                    if hi > right:
                        remaining.append((right + 1, hi))
            fresh = remaining
        data["new_line_ranges"] = fresh
        data["next_start_line"] = end + 1 if end < int(data.get("total_lines", 0)) else None
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted([*previous, *(([(start, end)]) if end >= start else [])]):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        self.read_ranges[key] = merged[-64:]
        self.read_ranges.move_to_end(key)
        if len(self.read_ranges) > 64:
            self.read_ranges.popitem(last=False)
        return response
