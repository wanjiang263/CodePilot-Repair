"""E-01：Synthetic fixture Schema、Loader 与推理/评测隔离。

这里的断言只关心一件事：**评测答案到不了推理侧**。所以每个"通过"都必须有一个
对应的"坏样本会失败"，否则测试只是在描述 happy path，挡不住真实的隔离破口。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from codepilot.eval.synthetic import (
    BenchmarkCategory,
    EvalTests,
    EvaluationTask,
    FixtureMetadata,
    FixtureValidationError,
    InferenceTask,
    SyntheticFixture,
    load_fixture,
    patch_added_lines,
    patch_modified_files,
    validate_suite,
)
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.tools.base import PathAccessError, WorkspacePathPolicy
from support.synth import (
    DEFAULT_ENV,
    SENTINEL,
    build_and_load,
    demo_spec,
    gold_sentinel_spec,
    write_fixture,
)


def test_build_and_load_round_trip(tmp_path: Path) -> None:
    fixture = build_and_load(tmp_path, demo_spec())
    assert fixture.task_id == "demo-off-by-one"
    assert fixture.metadata.category is BenchmarkCategory.INCORRECT_RETURN
    assert fixture.evaluation.tests.fail_to_pass == [
        "tests/test_hidden.py::test_normalize_keeps_value"
    ]


def test_json_round_trip_of_both_views(tmp_path: Path) -> None:
    """两个视图都必须能 JSON 往返——报告与断点续跑靠的就是序列化后的它们。"""
    fixture = build_and_load(tmp_path, demo_spec())
    repository = fixture.materialize(tmp_path / "repo-view")
    inference = fixture.inference(repository)
    evaluation = fixture.evaluation

    assert InferenceTask.model_validate_json(inference.model_dump_json()) == inference
    restored = EvaluationTask.model_validate_json(evaluation.model_dump_json())
    assert restored == evaluation
    assert (
        FixtureMetadata.model_validate_json(
            (fixture.directory / "metadata.json").read_text(encoding="utf-8")
        )
        == fixture.metadata
    )
    assert (
        EvalTests.model_validate_json(
            (fixture.directory / "eval" / "tests.json").read_text(encoding="utf-8")
        )
        == evaluation.tests
    )


def test_inference_and_evaluation_field_sets_are_disjoint() -> None:
    """除身份字段外，两侧对象不得共享字段名。

    同名字段是隔离最容易被绕过的方式：一个"task_id 上挂了 gold_files"的实现
    在类型上完全合法，但把 EvaluationTask 传进推理侧就成了顺理成章的事。
    """
    shared = set(InferenceTask.model_fields) & set(EvaluationTask.model_fields)
    assert shared == {"task_id", "base_commit"}
    answer_bearing = {
        "gold_patch",
        "test_patch",
        "tests",
        "gold_files",
        "fail_to_pass",
        "pass_to_pass",
    }
    assert not answer_bearing & set(InferenceTask.model_fields)


def test_inference_task_has_no_answer_paths(tmp_path: Path) -> None:
    fixture = build_and_load(tmp_path, demo_spec())
    inference = fixture.inference(fixture.materialize(tmp_path / "ws"))
    payload = inference.model_dump_json()
    assert SENTINEL not in payload
    assert "gold" not in payload
    assert str(fixture.directory / "eval") not in payload


def test_materialize_excludes_eval_and_keeps_base_source(tmp_path: Path) -> None:
    """workspace 里既没有 ``eval/``，也没有第二个提交。

    只查目录不够：base 之后的提交如果进了对象库，``git show`` 就能把 gold 读出来，
    而它在文件系统上完全不可见。
    """
    fixture = build_and_load(tmp_path, demo_spec())
    repository = fixture.materialize(tmp_path / "ws")
    source = (repository / "src" / "pkg" / "core.py").read_text(encoding="utf-8")
    assert "return value - 1" in source, "materialize 必须给出含 bug 的 base 状态"
    assert not (repository / "eval").exists()
    assert (repository / ".git").is_dir()

    from codepilot.repo.gitcmd import run_git

    revisions = run_git(["rev-list", "--all", "--count"], cwd=repository).stdout.strip()
    assert revisions == "1", "推理仓库只能含 base commit；多一个提交就是答案"
    found = run_git(["grep", "-l", SENTINEL, "--all"], cwd=repository, check=False)
    assert found.returncode != 0, f"gold 哨兵出现在推理仓库的对象库里: {found.stdout}"


def test_materialized_tree_has_no_sentinel_anywhere(tmp_path: Path) -> None:
    """逐字节扫整棵树（含 .git 原始对象），哨兵必须一次都不出现。"""
    fixture = build_and_load(tmp_path, gold_sentinel_spec())
    repository = fixture.materialize(tmp_path / "ws")
    hits = [
        path.relative_to(repository).as_posix()
        for path in repository.rglob("*")
        if path.is_file() and SENTINEL.encode() in path.read_bytes()
    ]
    assert hits == []


def test_tools_cannot_reach_eval_directory(tmp_path: Path) -> None:
    """工具层两道都挡：物理上不存在，策略上也拒绝。"""
    fixture = build_and_load(tmp_path, demo_spec())
    repository = fixture.materialize(tmp_path / "ws")
    policy = WorkspacePathPolicy(
        repository, deny_directories=WorkspacePathPolicy.DEFAULT_DENY_DIRECTORIES | {"eval"}
    )
    with pytest.raises(PathAccessError, match="denied by policy"):
        policy.resolve("eval/gold.patch", must_exist=True)
    with pytest.raises(PathAccessError, match="denied by policy"):
        policy.resolve("eval/tests.json", must_exist=False)
    # 不在 deny 名单里也一样读不到——workspace 里根本没有这个文件。
    plain = WorkspacePathPolicy(repository)
    with pytest.raises(PathAccessError, match="does not exist"):
        plain.resolve("eval/gold.patch", must_exist=True)


def test_parent_traversal_and_absolute_paths_rejected(tmp_path: Path) -> None:
    fixture = build_and_load(tmp_path, demo_spec())
    repository = fixture.materialize(tmp_path / "ws")
    policy = WorkspacePathPolicy(repository, deny_directories={"eval"})
    for attempt in ("../secrets.py", "a/../../b.py", "/etc/passwd", "C:/Windows/x"):
        with pytest.raises(PathAccessError):
            policy.resolve(attempt, must_exist=False)


def test_symlinked_repo_entry_is_rejected(tmp_path: Path) -> None:
    """符号链接把 workspace 变成读外部文件的通道，必须在加载阶段就拒掉。"""
    fixture = build_and_load(tmp_path, demo_spec())
    link = fixture.directory / "repo" / "sneaky.py"
    try:
        link.symlink_to(fixture.directory / "eval" / "gold.patch")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(FixtureValidationError, match="符号链接"):
        load_fixture(fixture.directory)


def test_directory_digests_rejects_symlink(tmp_path: Path) -> None:
    from codepilot.eval.synthetic import directory_digests

    root = tmp_path / "tree"
    root.mkdir()
    (root / "real.txt").write_text("x", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(root / "real.txt")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(FixtureValidationError, match="符号链接"):
        directory_digests(root)


def test_missing_required_files_fail_closed(tmp_path: Path) -> None:
    directory = write_fixture(tmp_path, demo_spec())
    (directory / "issue.md").unlink()
    (directory / "eval" / "gold.patch").unlink()
    with pytest.raises(FixtureValidationError) as excinfo:
        load_fixture(directory)
    reasons = "\n".join(excinfo.value.reasons)
    assert "issue.md" in reasons
    assert "gold.patch" in reasons


def test_task_id_must_match_directory_name(tmp_path: Path) -> None:
    directory = write_fixture(tmp_path, demo_spec())
    payload = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    payload["task_id"] = "some-other-id"
    (directory / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FixtureValidationError, match="目录名不一致"):
        load_fixture(directory)


def test_base_commit_drift_is_detected(tmp_path: Path) -> None:
    """改一个字节 → base_commit 变 → 必须失败，而不是"看起来还能跑"。"""
    directory = write_fixture(tmp_path, demo_spec())
    target = directory / "repo" / "src" / "pkg" / "core.py"
    text = target.read_text(encoding="utf-8").replace("return value - 1", "return value - 2")
    target.write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(FixtureValidationError, match="哈希不符"):
        load_fixture(directory)


def test_unregistered_repo_file_is_detected(tmp_path: Path) -> None:
    directory = write_fixture(tmp_path, demo_spec())
    (directory / "repo" / "extra.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    with pytest.raises(FixtureValidationError, match="未登记"):
        load_fixture(directory)


def test_missing_repo_file_is_detected(tmp_path: Path) -> None:
    directory = write_fixture(tmp_path, demo_spec())
    (directory / "repo" / "tests" / "test_public.py").unlink()
    with pytest.raises(FixtureValidationError, match="缺少文件"):
        load_fixture(directory)


def test_metadata_rejects_unknown_fields(tmp_path: Path) -> None:
    """``extra="forbid"`` 的作用：有人想把答案塞进推理可见的 metadata 就立刻失败。"""
    directory = write_fixture(tmp_path, demo_spec())
    payload = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    payload["gold_files"] = ["src/pkg/core.py"]
    (directory / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FixtureValidationError, match=r"metadata\.json"):
        load_fixture(directory)


def test_metadata_rejects_answer_shaping_fields() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        FixtureMetadata.model_validate(
            {
                "schema_version": 1,
                "task_id": "t-1",
                "category": "off_by_one",
                "difficulty": "easy",
                "multi_file": False,
                "has_traceback": False,
                "base_commit": "0" * 40,
                "repo_files": {"a.py": "0" * 64},
                "fix_hint": "把 -1 去掉",
            }
        )


def test_metadata_rejects_bad_hashes_and_commit() -> None:
    with pytest.raises(ValueError):
        FixtureMetadata.model_validate(
            {
                "schema_version": 1,
                "task_id": "t-1",
                "category": "off_by_one",
                "difficulty": "easy",
                "multi_file": False,
                "has_traceback": False,
                "base_commit": "abc",  # 不是完整 SHA
                "repo_files": {"a.py": "deadbeef"},  # 不是 sha256
            }
        )


def test_metadata_rejects_unsupported_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        FixtureMetadata.model_validate(
            {
                "schema_version": 99,
                "task_id": "t-1",
                "category": "off_by_one",
                "difficulty": "easy",
                "multi_file": False,
                "has_traceback": False,
                "base_commit": "0" * 40,
                "repo_files": {"a.py": "0" * 64},
            }
        )


def test_tests_json_requires_fail_to_pass() -> None:
    with pytest.raises(ValueError, match="fail_to_pass 不能为空"):
        EvalTests.model_validate({"fail_to_pass": [], "pass_to_pass": ["a.py::test_b"]})


def test_tests_json_rejects_overlap() -> None:
    node = "tests/test_x.py::test_y"
    with pytest.raises(ValueError, match="不能既要求失败又要求通过"):
        EvalTests.model_validate({"fail_to_pass": [node], "pass_to_pass": [node]})


def test_tests_json_rejects_duplicate_entries() -> None:
    node = "tests/test_x.py::test_y"
    with pytest.raises(ValueError, match="重复"):
        EvalTests.model_validate({"fail_to_pass": [node, node]})


def test_tests_json_rejects_eval_paths() -> None:
    with pytest.raises(ValueError, match="评测目录"):
        EvalTests.model_validate({"fail_to_pass": ["eval/test_hidden.py::test_y"]})


@pytest.mark.parametrize(
    "node",
    ["tests/test_x.py", "../../etc/passwd::test_y", "/abs/test.py::test_y", "a.py::", "a.py::;rm"],
)
def test_test_node_id_syntax_is_enforced(node: str) -> None:
    with pytest.raises(ValueError):
        EvalTests.model_validate({"fail_to_pass": [node]})


def test_builder_refuses_visible_target_tests() -> None:
    """生成器先把"目标测试必须是隐藏测试"当作构造前提拒绝掉。"""
    with pytest.raises(ValueError, match="hidden_test_files"):
        demo_spec(fail_to_pass=("tests/test_public.py::test_documented_base_behaviour",))


def test_visible_only_fail_to_pass_is_rejected_by_loader(tmp_path: Path) -> None:
    """手工改坏 tests.json 也一样要被 loader 拒掉。

    生成器挡住的是"我们自己的写法"，loader 挡住的是"磁盘上真实存在的东西"——
    fixture 可以被手工编辑，防线必须落在后者。
    """
    directory = write_fixture(tmp_path, demo_spec())
    payload = json.loads((directory / "eval" / "tests.json").read_text(encoding="utf-8"))
    payload["fail_to_pass"] = ["tests/test_public.py::test_documented_base_behaviour"]
    payload["pass_to_pass"] = []
    (directory / "eval" / "tests.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FixtureValidationError, match="不是隐藏评测测试"):
        load_fixture(directory)


def test_gold_patch_touching_tests_is_rejected(tmp_path: Path) -> None:
    directory = write_fixture(tmp_path, demo_spec())
    (directory / "eval" / "gold.patch").write_text(
        _patch_touching("tests/test_public.py"), encoding="utf-8", newline="\n"
    )
    with pytest.raises(FixtureValidationError, match=r"gold\.patch 修改了测试文件"):
        load_fixture(directory)


def test_test_patch_touching_source_is_rejected(tmp_path: Path) -> None:
    directory = write_fixture(tmp_path, demo_spec())
    (directory / "eval" / "test.patch").write_text(
        _patch_touching("src/pkg/core.py"), encoding="utf-8", newline="\n"
    )
    with pytest.raises(FixtureValidationError, match="非测试文件"):
        load_fixture(directory)


def _patch_touching(path: str) -> str:
    """一段只改动 ``path`` 的最小 git 统一 diff。

    刻意手写而不是用生成器：这两条规则要测的是 loader 面对**外部**产出的补丁
    时的行为，而生成器在更早的阶段就把这种 spec 拒了。
    """
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " from pkg.core import normalize\n"
        "-x = 1\n"
        "+x = 2\n"
    )


def test_patch_helpers_parse_git_diff(tmp_path: Path) -> None:
    fixture = build_and_load(tmp_path, demo_spec())
    gold = fixture.evaluation.gold_patch
    assert patch_modified_files(gold) == ("src/pkg/core.py",)
    added = patch_added_lines(gold)
    assert any("return value" in line for line in added)
    # 哨兵不在 gold 的新增行里——E-07 正是拿"新增行"去 issue 里做精确匹配。
    assert all(SENTINEL not in line for line in added)
    assert patch_modified_files(fixture.evaluation.test_patch) == ("tests/test_hidden.py",)


def test_patch_rejects_escaping_paths() -> None:
    with pytest.raises(ValueError, match="越界"):
        patch_modified_files("diff --git a/../x.py b/../x.py\n")
    with pytest.raises(ValueError, match="重命名"):
        patch_modified_files("diff --git a/old.py b/new.py\n")


def test_env_spec_is_strict(tmp_path: Path) -> None:
    directory = write_fixture(tmp_path, demo_spec(env={**DEFAULT_ENV, "surprise": 1}))
    with pytest.raises(FixtureValidationError, match=r"env\.yaml"):
        load_fixture(directory)


def test_env_spec_without_junitxml_is_rejected(tmp_path: Path) -> None:
    directory = write_fixture(
        tmp_path,
        demo_spec(env={**DEFAULT_ENV, "test_command": "python -m pytest -q"}),
    )
    with pytest.raises(FixtureValidationError, match=r"env\.yaml"):
        load_fixture(directory)


def test_inference_task_requires_existing_repository(tmp_path: Path) -> None:
    fixture = build_and_load(tmp_path, demo_spec())
    with pytest.raises(ValueError, match="推理仓库不存在"):
        fixture.inference(tmp_path / "never-created")


def test_validate_suite_reports_invalid_without_hiding_it(tmp_path: Path) -> None:
    """一个坏 fixture 不能被"跳过"；它必须出现在 invalid 清单里被报出来。"""
    good = write_fixture(tmp_path / "ok", demo_spec(task_id="good-one"))
    bad = write_fixture(tmp_path / "bad", demo_spec(task_id="bad-one"))
    (bad / "eval" / "tests.json").unlink()
    suite_root = tmp_path / "suite"
    suite_root.mkdir()
    good.rename(suite_root / "good-one")
    bad.rename(suite_root / "bad-one")
    suite = validate_suite(suite_root)
    assert suite.task_ids == ["good-one"]
    assert list(suite.invalid) == ["bad-one"]
    assert any("tests.json" in reason for reason in suite.invalid["bad-one"])


def test_validate_suite_selects_requested_tasks(tmp_path: Path) -> None:
    for name in ("task-a", "task-b"):
        write_fixture(tmp_path, demo_spec(task_id=name), name=name)
    suite = validate_suite(tmp_path / "suite", task_ids=["task-a"])
    assert suite.task_ids == ["task-a"]
    with pytest.raises(FixtureValidationError, match="不存在"):
        validate_suite(tmp_path / "suite", task_ids=["task-z"])


def test_fixture_version_binds_everything_that_changes_results(tmp_path: Path) -> None:
    """fixture_version 是断点续跑的身份之一，必须对 issue/gold/env 的变化敏感。"""
    base = build_and_load(tmp_path, demo_spec(), name="v0")
    changed_issue = build_and_load(tmp_path, demo_spec(issue="完全不同的描述\n"), name="v1")
    changed_env = build_and_load(
        tmp_path,
        demo_spec(env={**DEFAULT_ENV, "timeout_seconds": 90}, task_id="demo-env"),
        name="v2",
    )
    assert base.evaluation.fixture_version != changed_issue.evaluation.fixture_version
    assert base.evaluation.fixture_version != changed_env.evaluation.fixture_version
    assert base.evaluation.fixture_version.startswith("synthetic-v1-")
    assert isinstance(base.env_spec, EnvSpec)


def test_synthetic_fixture_is_frozen(tmp_path: Path) -> None:
    fixture = build_and_load(tmp_path, demo_spec())
    with pytest.raises(ValidationError):
        fixture.issue = "改写"
    assert isinstance(fixture, SyntheticFixture)


def test_leak_sentinel_absent_from_prompt_inputs(tmp_path: Path) -> None:
    """Agent 提示词的原料只有 issue + 仓库内容，两者都不该含哨兵。

    图级"提示词里真的没有答案"的断言在 ``test_benchmark_isolation.py`` 里用
    FakeLLM 录到的真实请求做；这里守住的是它的两个输入源。
    先确认哨兵**确实在评测侧**，否则下面的"到处都找不到"会因为夹具建错而空过。
    """
    fixture = build_and_load(tmp_path, gold_sentinel_spec())
    assert SENTINEL in fixture.evaluation.gold_patch
    assert any(SENTINEL in line for line in patch_added_lines(fixture.evaluation.gold_patch))
    assert SENTINEL in fixture.evaluation.test_patch

    repository = fixture.materialize(tmp_path / "ws")
    inference = fixture.inference(repository)
    assert SENTINEL not in inference.issue
    texts = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.parts
    ]
    assert texts, "materialize 之后仓库里应该有文件"
    assert all(SENTINEL not in text for text in texts)
    assert SENTINEL not in inference.model_dump_json()
