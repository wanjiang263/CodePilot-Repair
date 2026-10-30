"""Phase28 archive→verify 闭环集成：临时 git 仓库 + 临时 archive root，只碰回环。

与 ``tests/unit/test_phase28_archive*.py`` 的分工：单测钉住生成器/校验器各自的
字段级契约；本文件走**完整闭环**——生成器产出的目录交给 ``scripts/phase28_archive_verify.py``
的校验入口（含 subprocess 调 CLI 的真实进程路径），再人为篡改档案证明校验真的会失败。
另覆盖 ``scripts/phase28_archive.py --dry-run`` 的 CLI 外部契约（只读真实仓库，零落盘）。

零外部网络、零 Docker、零模型请求；除 dry-run（显式不写盘）外所有写都发生在 ``tmp_path``。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from codepilot.archive import create_candidate

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "phase28_archive_verify.py"
ARCHIVE_SCRIPT = REPO_ROOT / "scripts" / "phase28_archive.py"

CODER_SOURCE = '"""Coder fixture."""\n\n_SYSTEM = "coder prompt fixture"\n\n\ndef run() -> str:\n    return _SYSTEM\n'
PLANNER_SOURCE = '"""Planner fixture."""\n\n_SYSTEM = "planner prompt fixture"\n'
REVIEWER_SOURCE = '"""Reviewer fixture."""\n\n_SYSTEM = "reviewer prompt fixture"\n'
FEATURE_V1 = "def feature() -> int:\n    return 1\n"
FEATURE_V2 = "def feature() -> int:\n    return 2\n"
FEATURE_V3 = "def feature() -> int:\n    return 3\n"
NOTE_V1 = "note v1\n"
NOTE_V2 = "note v2\n"
EXTRA_SOURCE = "def extra() -> str:\n    return 'extra'\n"

V1 = "phase28-s6-v1"
V2 = "phase28-s6-v2"


def git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def write_file(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode("utf-8").strip()


def build_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """base → v1 变更（提交）→ v2 变更（提交）；每个创建候选时工作树都是干净的。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "loop@example.invalid")
    git(repo, "config", "user.name", "Loop Fixture")
    git(repo, "config", "core.autocrlf", "false")
    write_file(repo, "agents/coder.py", CODER_SOURCE)
    write_file(repo, "agents/planner.py", PLANNER_SOURCE)
    write_file(repo, "agents/reviewer.py", REVIEWER_SOURCE)
    write_file(repo, "src/feature.py", FEATURE_V1)
    write_file(repo, "docs/note.md", NOTE_V1)
    base = commit_all(repo, "base")
    write_file(repo, "src/feature.py", FEATURE_V2)
    write_file(repo, "docs/note.md", NOTE_V2)
    head1 = commit_all(repo, "candidate one")
    write_file(repo, "src/feature.py", FEATURE_V3)
    write_file(repo, "src/extra.py", EXTRA_SOURCE)
    head2 = commit_all(repo, "candidate two")
    return repo, base, head1, head2


def source_paths(extra: bool = False) -> tuple[str, ...]:
    paths = (
        "agents/coder.py",
        "agents/planner.py",
        "agents/reviewer.py",
        "src/feature.py",
        "docs/note.md",
    )
    return paths + (("src/extra.py",) if extra else ())


def load_verify_module() -> Any:
    spec = importlib.util.spec_from_file_location("phase28_archive_verify_loop", VERIFY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_two_candidates(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """archive root + 临时仓库，v1（parent 链根）与 v2（parent=v1）都已生成。"""
    repo, base, head1, head2 = build_repo(tmp_path)
    root = tmp_path / "archive"
    create_candidate(
        root,
        repo,
        version=V1,
        diff_base_commit=base,
        source_commit=head1,
        source_files=list(source_paths()),
    )
    create_candidate(
        root,
        repo,
        version=V2,
        parent=V1,
        diff_base_commit=head1,
        source_commit=head2,
        source_files=list(source_paths(extra=True)),
    )
    return root, repo, {"base": base, "head1": head1, "head2": head2}


def run_cli(
    script: Path, *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        check=False,
        cwd=str(cwd or REPO_ROOT),
        env=dict(os.environ),
        timeout=120,
    )


# ---------------------------------------------------------------- 闭环 1：生成 → 校验通过


def test_generated_candidates_verify_clean_with_parent_chain(tmp_path: Path) -> None:
    root, repo, commits = build_two_candidates(tmp_path)
    verify = load_verify_module()

    for version in (V1, V2):
        folder = root / "candidates" / version
        assert sorted(p.name for p in folder.iterdir()) == [
            "candidate.diff",
            "candidate.json",
            "snapshot.json",
            "sources.json",
        ]
        record = json.loads((folder / "candidate.json").read_text(encoding="utf-8"))
        assert record["schema_version"] == 3
        assert record["source_origin"] == "git_commit"
        expected_base = commits["head1"] if version == V2 else commits["base"]
        assert record["diff_base_commit"] == expected_base

    report = verify.verify_versions(
        archive_root=root, repo=repo, versions=[V1, V2], ignore_raw_drift=False
    )
    assert report["exit_code"] == 0
    assert report["new_archive_status"] == "passed"
    assert report["totals"]["new_archives"] == 2
    assert report["totals"]["new_archive_failures"] == 0
    by_version = {entry["version"]: entry for entry in report["versions"]}
    for version in (V1, V2):
        entry = by_version[version]
        assert entry["ok"], list(entry["failures"])
        assert entry["archive_class"] == "new"
    # parent 链：v2 记录的 parent_integrity 与 v1 档案实际复算一致。
    parent_check = next(
        c for c in by_version[V2]["checks"] if c["check"] == "parent_integrity_matches"
    )
    assert parent_check["ok"] and V1 in parent_check["detail"]
    # 校验器只读：校验后档案目录里没有多出任何文件，也没有 active 指针。
    assert not (root / "active.json").exists()
    assert not (root / "active.next.json").exists()


# ---------------------------------------------------------------- 闭环 2：篡改 → 校验失败


def test_tampered_snapshot_fails_verification(tmp_path: Path) -> None:
    root, repo, _ = build_two_candidates(tmp_path)
    verify = load_verify_module()

    before = verify.verify_versions(
        archive_root=root, repo=repo, versions=[V1], ignore_raw_drift=False
    )
    assert before["exit_code"] == 0

    snapshot = root / "candidates" / V1 / "snapshot.json"
    snapshot.write_bytes(snapshot.read_bytes() + b" ")  # 单字节篡改：文件哈希必变

    after = verify.verify_versions(
        archive_root=root, repo=repo, versions=[V1], ignore_raw_drift=False
    )
    assert after["exit_code"] == 1
    assert after["new_archive_status"] == "failed"
    entry = after["versions"][0]
    assert entry["ok"] is False
    failed_checks = {f["check"] for f in entry["failures"]}
    assert "snapshot_file_sha256" in failed_checks
    # canonical 哈希不受尾随空白影响：只允许文件字节哈希这一条失败。
    assert "snapshot_canonical_sha256" not in failed_checks


# ---------------------------------------------------------------- 闭环 3：subprocess 调校验 CLI


def test_verify_cli_exit_codes_on_clean_and_tampered_archive(tmp_path: Path) -> None:
    root, repo, _ = build_two_candidates(tmp_path)

    clean = run_cli(
        VERIFY_SCRIPT, "--version", V1, "--archive-root", str(root), "--repo", str(repo)
    )
    assert clean.returncode == 0, clean.stderr.decode("utf-8", errors="replace")
    stdout = clean.stdout.decode("utf-8", errors="replace")
    assert "new  ok" in stdout

    diff_file = root / "candidates" / V2 / "candidate.diff"
    diff_file.write_bytes(diff_file.read_bytes().replace(b"return 3", b"return 9"))

    tampered = run_cli(
        VERIFY_SCRIPT, "--version", V2, "--archive-root", str(root), "--repo", str(repo)
    )
    assert tampered.returncode == 1
    tampered_out = tampered.stdout.decode("utf-8", errors="replace")
    assert "[fail]" in tampered_out
    assert "diff_sha256" in tampered_out


# ---------------------------------------------------------------- 闭环 4：archive CLI --dry-run


def test_archive_cli_dry_run_prints_json_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """真实仓库上的 --dry-run：完整校验与哈希计算，退出码 0，档案根零落盘。"""
    archive_root = tmp_path / "dry-run-archive"
    source_list = tmp_path / "sources.json"
    source_list.write_text(json.dumps(["README.md"]), encoding="utf-8")

    completed = run_cli(
        ARCHIVE_SCRIPT,
        "--version",
        "phase28-s6-dryrun",
        "--parent",
        "",
        "--diff-base",
        "HEAD",
        "--source-list",
        str(source_list),
        "--archive-root",
        str(archive_root),
        # Phase29 契约：脏容忍必须显式声明 worktree 来源（dry-run 不落盘）。
        "--source-origin",
        "worktree",
        "--allow-dirty",
        "--dry-run",
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    summary = json.loads(completed.stdout.decode("utf-8"))
    assert summary["dry_run"] is True
    assert summary["source_origin"] == "worktree"
    assert summary["version"] == "phase28-s6-dryrun"
    assert summary["source_files"] == 1
    assert summary["snapshot_file_sha256"] and summary["diff_sha256"]
    assert summary["directory"] == str(archive_root / "candidates" / "phase28-s6-dryrun")
    # --dry-run 绝不落盘：档案根里连 candidates/ 目录都不该出现。
    assert not archive_root.exists()
