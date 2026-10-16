"""为 ``codepilot bench`` 生成 FakeLLM 脚本——工程 smoke，不是模型能力证据。

脚本里的"修复动作"来自 fixture 的 gold patch，也就是说这是一个**读了答案的脚本化
Agent**。它的唯一用途是让 推理 → 容器执行 → 评测 → journal → 报告 这条链路在不调用
任何付费模型的前提下确定性地走一遍：链路里的每一个数字都可复现，而"成功率"这个数字
本身没有任何含义。报告会把 ``simulated_llm=True`` 与 ``claims_model_capability=False``
写进产物，就是为了防止这份数据被当成效果证据读。

用法::

    uv run python scripts/make_bench_scripts.py \\
        --task budget-member-discount-inverted-condition:correct \\
        --task textkit-table-keeps-its-own-split-rule:wrong \\
        --out artifacts/phase10/fake-llm-scripts

``mode=correct`` 让脚本化 Agent 给出 gold 的第一处改动；``mode=wrong`` 给出一个
"改了但没修好"的等价编辑，用来把 not_resolved / 多轮迭代 / 诊断这些分支也走一遍。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codepilot.eval.evaluation import GoldEvaluator  # noqa: E402
from codepilot.eval.synthetic import SyntheticFixture, load_fixture  # noqa: E402
from codepilot.fsutil import remove_tree  # noqa: E402
from codepilot.repo.index import Indexer, RepositoryIndex  # noqa: E402
from codepilot.repo.workspace import WorkspaceManager  # noqa: E402
from codepilot.storage.db import (  # noqa: E402
    create_engine_from_url,
    create_session_factory,
    init_db,
)
from codepilot.storage.repository import Stores  # noqa: E402

PURPOSES = ("localizer_files", "localizer_symbols", "planner", "coder", "diagnoser", "reviewer")
REPEATS = 3


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 bench 的 FakeLLM 脚本")
    parser.add_argument("--suite-root", type=Path, default=ROOT / "benchmarks" / "synthetic")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--task",
        action="append",
        required=True,
        metavar="TASK_ID[:correct|wrong]",
        help="要生成脚本的任务，可重复。缺省 mode=correct。",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=None,
        help="索引用的临时目录；缺省建在 --out 旁边。",
    )
    parser.add_argument(
        "--bench-work",
        type=Path,
        default=ROOT / "artifacts" / "bench-work" / "evaluation",
        help=(
            "bench 推理仓库的材料化根目录（cli.py 用的同一个：work_root/evaluation）。"
            "必须一致：candidate_id 派生自 repository_id，而 repository_id 派生自仓库路径。"
        ),
    )
    return parser.parse_args(argv)


def _parse_task(raw: str) -> tuple[str, str]:
    task_id, _, mode = raw.partition(":")
    if mode not in ("", "correct", "wrong"):
        raise SystemExit(f"未知 mode {mode!r}，只能是 correct 或 wrong")
    return task_id, (mode or "correct")


def _unused_sandbox(_artifacts: Any) -> Any:
    raise AssertionError("生成脚本只需要材料化推理仓库，从不执行测试")


def _index(fixture: SyntheticFixture, repository: Path, work: Path) -> RepositoryIndex:
    """按 bench 装配的同一套方式索引推理仓库。

    定位脚本里的 ``candidate_id`` 必须是索引真实给出的 id，否则 Localizer 会以
    "未知候选"拒掉整次运行——那不是测试，那是脚本写错了。
    """
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    engine = create_engine_from_url(f"sqlite:///{(work / 'index.sqlite3').as_posix()}")
    init_db(engine)
    session = create_session_factory(engine)()
    stores = Stores(session)
    manager = WorkspaceManager(cache_root=work / "cache")
    workspace = manager.create(
        source=str(repository),
        base_commit=fixture.metadata.base_commit,
        target=work / "workspace",
    )
    Indexer().index(stores, workspace)
    session.commit()
    index = RepositoryIndex(stores, workspace.repository_id)
    engine.dispose()
    return index


def _gold_pair(patch: str, path: str) -> tuple[str, str]:
    """取出某个文件在 gold 里的第一处 ``(-行, +行)``。"""
    header = f"diff --git a/{path} b/{path}"
    section = patch.split(header, 1)[1] if header in patch else patch
    removed: str | None = None
    for line in section.splitlines():
        if line.startswith("--- ") or line.startswith("+++ ") or line.startswith("diff --git"):
            if removed is not None:
                break
            continue
        if line.startswith("-") and not line.startswith("---"):
            removed = line[1:]
        elif line.startswith("+") and not line.startswith("+++"):
            if removed is None:
                raise ValueError(f"{path}: gold 里找不到与新增行配对的删除行")
            return removed, line[1:]
    raise ValueError(f"{path}: gold patch 里没有可用的行级改动")


def _build_script(
    fixture: SyntheticFixture, mode: str, index: RepositoryIndex
) -> dict[str, list[Any]]:
    evaluation = fixture.evaluation
    target_file = sorted(evaluation.gold_files)[0]
    removed, added = _gold_pair(evaluation.gold_patch, target_file)
    if mode == "wrong":
        # 行为等价的"改了但没修好"：让 not_resolved、多轮迭代与诊断分支也被走到。
        added = f"{removed}  # bench-smoke: attempted but behaviour-preserving"

    files = {item.path: item for item in index.files()}
    if target_file not in files:
        raise ValueError(f"{fixture.task_id}: 推理仓库索引里没有 {target_file}")
    file_candidate = files[target_file]
    symbols = [item for item in index.symbol_objects() if item.path == target_file]
    function = next((item for item in symbols if item.kind == "function"), None)
    symbol = function or symbols[0]

    tests = sorted(
        path
        for path in files
        if (path.startswith("tests/") and path.endswith(".py")) or "/test_" in path
    )
    if not tests:
        raise ValueError(f"{fixture.task_id}: 推理仓库里没有可见测试文件，无法给 plan 指定目标")

    edit = {
        "tool": "edit_file",
        "arguments": {"path": target_file, "search": removed, "replace": added},
    }
    finish = {"tool": "finish", "arguments": {"summary": f"apply {target_file} change"}}
    coder: list[Any] = []
    for _ in range(REPEATS):
        coder.extend((edit, finish))

    return {
        "localizer_files": [
            {
                "files": [
                    {"candidate_id": file_candidate.id, "reason": f"issue points at {target_file}"}
                ]
            }
        ]
        * REPEATS,
        "localizer_symbols": [
            {
                "symbols": [
                    {
                        "candidate_id": symbol.id,
                        "start_line": symbol.start_line,
                        "end_line": symbol.end_line,
                        "reason": f"the {symbol.qualified_name.rsplit('.', 1)[-1]} definition",
                    }
                ]
            }
        ]
        * REPEATS,
        "planner": [
            item
            for _ in range(REPEATS)
            for item in (
                {"tool": "read_file", "arguments": {"path": target_file}},
                {
                    "root_cause_hypothesis": f"{target_file} no longer matches its contract",
                    "evidence": [f"{target_file}: {removed.strip()}"],
                    "changes": [
                        {
                            "file": target_file,
                            "symbol": symbol.qualified_name,
                            "action": "modify",
                            "description": "restore the documented behaviour",
                        }
                    ],
                    "tests_to_run": tests,
                    "risk": "low",
                    "alternatives": [],
                },
            )
        ],
        "coder": coder,
        "diagnoser": [
            {
                "root_cause": "The edited line still does not satisfy the target test.",
                "evidence": [
                    {
                        "kind": "code",
                        "file": target_file,
                        "line": symbol.start_line,
                        "text": removed.strip(),
                    }
                ],
                "confidence": 0.8,
                "is_patch_caused": False,
                "repair_strategy": "refine",
                "suspected_symbols": [symbol.qualified_name],
            }
        ]
        * REPEATS,
        "reviewer": [
            {
                "approved": True,
                "risk": "low",
                "issues": [],
                "summary": "scripted review: no findings",
            }
        ]
        * REPEATS,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    work_base = args.work_root or out.parent / "bench-script-work"
    # 与 cli.py 的 bench 用同一个材料化根目录：candidate_id 派生自 repository_id，
    # 而 repository_id 派生自（仓库路径, base commit）。换路径索引出来的 id 会被
    # Localizer 当作"未知候选"拒掉，症状是 workflow_error 而不是"脚本写错了"。
    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=args.bench_work / "repo-cache"),
        sandbox_factory=_unused_sandbox,
        work_root=args.bench_work,
    )

    manifest: list[dict[str, Any]] = []
    for raw in args.task:
        task_id, mode = _parse_task(raw)
        fixture = load_fixture(args.suite_root / task_id)
        scratch = work_base / task_id
        remove_tree(scratch)
        scratch.mkdir(parents=True)
        repository = evaluator.materialized_repository(fixture)
        script = _build_script(fixture, mode, _index(fixture, repository, scratch / "index"))
        missing = [key for key in PURPOSES if not script.get(key)]
        if missing:
            raise SystemExit(f"{task_id}: 脚本缺少 purpose {missing}")
        (out / f"{task_id}.json").write_text(
            json.dumps(script, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
        )
        manifest.append(
            {
                "task_id": task_id,
                "mode": mode,
                "oracle_source": "gold patch",
                "candidate_id_bound_to": str(repository),
                "fixture_version": fixture.evaluation.fixture_version,
                "purposes": sorted(script),
            }
        )
        print(f"[script] {task_id} mode={mode}")

    (out / "_manifest.json").write_text(
        json.dumps(
            {
                "warning": (
                    "这些脚本由 gold patch 生成：脚本化 Agent 看得到答案。"
                    "本目录用于验证 bench 链路，任何成功率都不构成模型能力证据。"
                ),
                "tasks": manifest,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(f"脚本目录: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
