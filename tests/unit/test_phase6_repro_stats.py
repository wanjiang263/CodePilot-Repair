"""D-08：复现成功率评测脚本的离线部分。

真实执行需要 Docker，脚本本身在 daemon 不可用时直接报告 NOT RUN。这里覆盖的是
不需要容器的部分：信息隔离守卫、指标口径、聚合与落盘、gold 侧的短路与校验。

候选测试内容属于 FakeLLM 的脚本，因此这些数字衡量的是**复现链路**，不是模型能力；
测试还专门检查候选内容没有从 gold patch 里抄来任何一行。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from codepilot.agents.reproducer import screen_reproduction_test, write_reproduction_test
from codepilot.retrieval.dataset import load_dataset
from codepilot.schemas.localization import LocalizationResult
from codepilot.schemas.reproduction import REPRO_TEST_DIRECTORY
from codepilot.tools.base import WorkspacePathPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "phase6_repro_stats.py"
DATASET = REPO_ROOT / "tests" / "fixtures" / "retrieval_cases.jsonl"
DATASET_ROOT = DATASET.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase6_repro_stats", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # slots dataclass 在解析延迟注解时按 __module__ 回查 sys.modules；先注册再执行。
    sys.modules["phase6_repro_stats"] = module
    spec.loader.exec_module(module)
    return module


stats = _load()
CASES = load_dataset(DATASET)


def _record(task: str, **updates: object) -> stats.TaskReproRecord:  # type: ignore[valid-type]
    record = stats.TaskReproRecord(task_id=task)
    for key, value in updates.items():
        setattr(record, key, value)
    return record


def test_dataset_scale_is_reported_from_the_existing_fixtures_not_invented() -> None:
    assert len(CASES) == 12
    assert set(stats.CANDIDATES) == {case.task_id for case in CASES}


def test_inference_inputs_expose_only_issue_and_base_snapshot() -> None:
    for case in CASES:
        repo_dir, base_commit, issue = stats.build_inference_inputs(case, DATASET_ROOT)
        assert repo_dir.is_dir()
        assert base_commit == "snapshot"
        assert issue == case.issue
        assert "gold" not in str(repo_dir).lower()


def test_isolation_guard_rejects_a_leaking_inference_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CASES[0]

    class _Leaky:
        gold_patch = case.gold_patch

        def inference(self, root: Path) -> object:
            from codepilot.retrieval.input import RetrievalInput

            return RetrievalInput(
                task_id=case.task_id,
                repo=str(root / case.repo),
                base_commit="snapshot",
                issue=f"{case.issue} 答案见 retrieval_gold/01-expiry-boundary.patch",
            )

    with pytest.raises(AssertionError, match="泄漏"):
        stats.build_inference_inputs(_Leaky(), DATASET_ROOT)  # type: ignore[arg-type]


def test_no_candidate_borrows_a_line_from_the_gold_patch() -> None:
    """候选是**测试**，gold 是**源码改动**；出现逐行相同就说明生成侧看过答案。"""
    for case in CASES:
        patch_text = (DATASET_ROOT / case.gold_patch).read_text(encoding="utf-8")
        added = [
            line[1:].strip()
            for line in patch_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        candidate = stats.CANDIDATES[case.task_id]
        for line in added:
            if len(line) > 10:
                assert line not in candidate.content, f"{case.task_id} 抄了 gold 行: {line}"
        assert "gold" not in candidate.content.lower()


@pytest.mark.parametrize("task_id", sorted(stats.CANDIDATES))
def test_every_scripted_candidate_is_a_collectable_reproduction_test(task_id: str) -> None:
    candidate = stats.CANDIDATES[task_id]
    screening = screen_reproduction_test(candidate.content)
    assert screening.ok is True, (task_id, screening.rejected)
    assert screening.warnings == ()
    assert candidate.reply()["filename"] == candidate.filename


@pytest.mark.parametrize("task_id", sorted(stats.CANDIDATES))
def test_every_candidate_only_touches_the_fixture_package(task_id: str) -> None:
    """候选只能 import 夹具自己的模块：真正跑起来的路径由 Docker 评测覆盖。"""
    candidate = stats.CANDIDATES[task_id]
    top_level = {name.split(".", 1)[0] for name in _imported_modules(candidate.content)}
    assert top_level <= {"pytest", "miniauth"}, (task_id, top_level)
    for name in top_level - {"pytest"}:
        assert (DATASET_ROOT / "repos/mini_auth/src" / name).is_dir(), name


def _imported_modules(content: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(content)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_localization_is_built_from_retrieval_not_from_gold_labels() -> None:
    case = CASES[0]
    root, base_commit, issue_text = stats.build_inference_inputs(case, DATASET_ROOT)
    assert base_commit == "snapshot" and issue_text == case.issue
    localization, snapshot_id = stats.build_localization(
        Path(root), issue_text, task_id=case.task_id
    )
    assert isinstance(localization, LocalizationResult)
    assert localization.files, "BM25 在夹具上必须给出候选文件"
    assert localization.file_decisions[0].reason.startswith("BM25")
    assert snapshot_id
    assert "gold" not in localization.model_dump_json().lower()


def test_degraded_targets_are_the_existing_repo_tests(tmp_path: Path) -> None:
    base = tmp_path / "base"
    (base / "tests").mkdir(parents=True)
    (base / "tests/test_session.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    write_reproduction_test(
        WorkspacePathPolicy(base, repro_write=True),
        "test_repro_session.py",
        "def test_y():\n    assert 1\n",
    )
    assert stats._existing_test_node_ids(base) == ["tests/test_session.py"]


def test_metrics_have_explicit_denominators_and_hand_computed_rates() -> None:
    """手算：8 个任务里 6 个生成、4 个 base 有效失败、3 个 gold 通过。"""
    records = [
        _record(
            "a",
            generated=True,
            base_valid_failure=True,
            gold_passed=True,
            attempts=1,
            status="reproduced",
            kind="valid_assertion",
            attempt_kinds=["valid_assertion"],
        ),
        _record(
            "b",
            generated=True,
            base_valid_failure=True,
            gold_passed=True,
            attempts=2,
            status="reproduced",
            kind="valid_issue_exception",
            attempt_kinds=["invalid_pass", "valid_issue_exception"],
        ),
        _record(
            "c",
            generated=True,
            base_valid_failure=True,
            gold_passed=False,
            attempts=3,
            status="reproduced",
            kind="valid_assertion",
            attempt_kinds=["invalid_undeclared_exception", "invalid_pass", "valid_assertion"],
        ),
        _record(
            "d",
            generated=True,
            base_valid_failure=False,
            gold_passed=None,
            attempts=4,
            status="failed",
            kind="invalid_pseudo_reproduction",
            attempt_kinds=["invalid_pseudo_reproduction"] * 4,
        ),
        _record(
            "e",
            generated=True,
            base_valid_failure=False,
            gold_passed=None,
            attempts=1,
            status="env_error",
            kind="env_error",
            attempt_kinds=["env_error"],
        ),
        _record(
            "f",
            generated=True,
            base_valid_failure=True,
            gold_passed=True,
            attempts=1,
            status="reproduced",
            kind="valid_assertion",
            attempt_kinds=["valid_assertion"],
        ),
        _record(
            "g",
            generated=False,
            status="env_error",
            kind="workspace_error",
            attempts=0,
            attempt_kinds=[],
        ),
        _record(
            "h",
            generated=True,
            base_valid_failure=False,
            gold_passed=None,
            attempts=2,
            status="failed",
            kind="invalid_import_error",
            attempt_kinds=["invalid_import_error", "invalid_pass"],
        ),
    ]
    metrics = stats.aggregate(records, simulated=False)

    assert metrics["denominators"] == {
        "total_tasks": 8,
        "tasks_with_generated_candidate": 7,
        "tasks_with_valid_base_failure": 4,
        "valid_base_failures_evaluated_on_gold": 4,
    }
    rates = metrics["rates"]
    assert rates["generation_rate"] == pytest.approx(7 / 8, abs=1e-4)
    assert rates["base_valid_failure_rate"] == pytest.approx(4 / 7, abs=1e-4)
    assert rates["gold_pass_rate"] == pytest.approx(3 / 4, abs=1e-4)
    assert rates["reproduction_success_rate"] == pytest.approx(3 / 8, abs=1e-4)
    assert metrics["counts"]["environment_errors"] == 2
    assert metrics["counts"]["retries_exhausted"] == 1
    assert metrics["attempts"]["mean"] == pytest.approx(
        (1 + 2 + 3 + 4 + 1 + 1 + 0 + 2) / 8, abs=1e-4
    )
    assert metrics["conditions"]["simulated_sandbox"] is False
    assert metrics["conditions"]["execution_backend"] == "docker"
    assert metrics["conditions"]["not_a_model_capability_measurement"] is True
    assert metrics["failure_kind_distribution"]["invalid_pass"] == 3


def test_empty_denominators_do_not_become_zero_percent_or_hundred_percent() -> None:
    metrics = stats.aggregate([], simulated=False)
    assert metrics["denominators"]["total_tasks"] == 0
    assert all(value is None for value in metrics["rates"].values())
    assert metrics["attempts"]["mean"] is None


def test_outputs_are_written_with_conditions_and_per_task_rows(tmp_path: Path) -> None:
    records = [
        _record(
            "a",
            generated=True,
            base_valid_failure=True,
            gold_passed=True,
            attempts=1,
            status="reproduced",
            kind="valid_assertion",
            repro_path="tests/_codepilot_repro/x.py",
            attempt_kinds=["valid_assertion"],
            detail="AssertionError: 边界秒",
        ),
        _record(
            "b",
            generated=True,
            base_valid_failure=False,
            attempts=4,
            status="failed",
            kind="invalid_pass",
            attempt_kinds=["invalid_pass"] * 4,
            detail="三轮都通过",
        ),
    ]
    metrics = stats.aggregate(records, simulated=False)
    paths = stats.write_outputs(tmp_path / "phase6", records, metrics)

    assert set(paths) == {"results", "metrics", "summary"}
    rows = [json.loads(line) for line in paths["results"].read_text(encoding="utf-8").splitlines()]
    assert [row["task_id"] for row in rows] == ["a", "b"]
    assert rows[0]["gold_passed"] is True and rows[0]["base_valid_failure"] is True
    payload_metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    assert payload_metrics["counts"]["reproduction_successes"] == 1
    assert payload_metrics["counts"]["degraded_to_existing_tests"] == 1
    payload = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    assert payload["denominators"]["total_tasks"] == 2
    summary = paths["summary"].read_text(encoding="utf-8")
    assert "FakeLLM" in summary and "docker" in summary
    assert "不是任何真实模型的复现能力" in summary
    assert "| `a` | reproduced |" in summary
    assert "base FAIL" in summary


def test_gold_evaluation_rejects_a_digest_mismatch_before_touching_the_sandbox(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    (base / "src").mkdir(parents=True)
    (base / "src/pkg.py").write_text("VALUE = 1\n", encoding="utf-8")
    patch = tmp_path / "gold.patch"
    patch.write_text("--- a/src/pkg.py\n+++ b/src/pkg.py\n", encoding="utf-8")
    evaluator = stats.GoldReproEvaluator(  # type: ignore[attr-defined]
        validator_factory=lambda directory: pytest.fail("digest 校验失败前不得执行任何测试")
    )

    passed, detail = evaluator.passes_after_gold(
        base_dir=base,
        gold_dir=tmp_path / "gold",
        gold_patch=patch,
        expected_sha256="0" * 64,
        filename="test_repro_x.py",
        content="def test_x():\n    assert 1 == 1\n",
        test_names=("test_x",),
    )
    assert passed is False
    assert "digest" in detail


def test_gold_workspace_is_a_copy_of_base_with_the_repro_dir_removed(tmp_path: Path) -> None:
    base = tmp_path / "base"
    (base / "src").mkdir(parents=True)
    (base / "src/pkg.py").write_text("VALUE = 1\n", encoding="utf-8")
    write_reproduction_test(
        WorkspacePathPolicy(base, repro_write=True),
        "test_repro_x.py",
        "def test_x():\n    assert 1 == 1\n",
    )
    gold = stats._prepare_gold_workspace(base_dir=base, gold_dir=tmp_path / "gold")  # type: ignore[attr-defined]

    assert (gold / "src/pkg.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (gold / REPRO_TEST_DIRECTORY).exists()
    # base 本身不被改动，复现文件仍在那里。
    assert (base / REPRO_TEST_DIRECTORY / "test_repro_x.py").is_file()


def test_docker_unavailable_is_reported_as_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stats, "_docker_available", lambda: False)
    out = tmp_path / "out"
    assert stats.main(["--out", str(out)]) == 2
    assert not (out / "metrics.json").exists()
