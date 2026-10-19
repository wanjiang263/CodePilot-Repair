"""E-11 / E-12：SWE-bench Lite 离线快照 Loader 与信息隔离测试。

测试全部离线：快照由 ``scripts/fetch_swebench_lite.py`` 的纯函数从
``tests/fixtures/swebench/`` 里 5 条真实格式的行重新生成到 ``tmp_path``。真实
``cache/swebench`` 快照的测试只在资产存在时运行（该目录 gitignored，由 fetch 脚本
生成，不是代码交付物）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from codepilot.eval.swebench import (
    SWEBenchCacheError,
    SWEBenchDatasetError,
    SWEBenchEvaluationTask,
    SWEBenchLoader,
)
from codepilot.eval.swebench_task import (
    DATASET_NAME,
    DATASET_REVISION,
    FORBIDDEN_INFERENCE_FIELDS,
    INFERENCE_IDENTITY_FIELDS,
    OFFICIAL_SPLIT_SIZES,
    SWEBenchDatasetRef,
    SWEBenchInferenceTask,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "swebench"
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_SPECS = FIXTURES / "env-specs.json"
REAL_SNAPSHOT_ROOT = REPO_ROOT / "cache" / "swebench"
CUSTOM_DATASET = "fixtures/SWE-bench_Lite"
CUSTOM_REVISION = "b" * 40


def _load_fetch_module() -> Any:
    path = REPO_ROOT / "scripts" / "fetch_swebench_lite.py"
    spec = importlib.util.spec_from_file_location("fetch_swebench_lite_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FETCH = _load_fetch_module()


def _rows(split: str) -> list[dict[str, Any]]:
    text = (FIXTURES / f"{split}.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _snapshot_dir(snapshot_root: Path, dataset_name: str, revision: str) -> Path:
    return snapshot_root / f"{dataset_name.replace('/', '__')}__{revision}"


def _build_snapshot(
    tmp_path: Path,
    *,
    dev_rows: list[dict[str, Any]] | None = None,
    test_rows: list[dict[str, Any]] | None = None,
    dataset_name: str = CUSTOM_DATASET,
    revision: str = CUSTOM_REVISION,
) -> tuple[Path, Path]:
    rows = {
        "dev": _rows("dev") if dev_rows is None else dev_rows,
        "test": _rows("test") if test_rows is None else test_rows,
    }
    files = FETCH.build_snapshot_files(rows, dataset_name=dataset_name, revision=revision)
    snapshot_root = tmp_path / "swebench-cache"
    snapshot_dir = _snapshot_dir(snapshot_root, dataset_name, revision)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (snapshot_dir / name).write_bytes(payload)
    return snapshot_root, snapshot_dir


def _loader(
    snapshot_root: Path,
    *,
    dataset_name: str = CUSTOM_DATASET,
    revision: str = CUSTOM_REVISION,
    env_spec_snapshot: Path | None = ENV_SPECS,
) -> SWEBenchLoader:
    return SWEBenchLoader(
        snapshot_root,
        dataset_name=dataset_name,
        revision=revision,
        env_spec_snapshot=env_spec_snapshot,
    )


def _mutate_manifest(snapshot_dir: Path, mutate: Any) -> None:
    """修改 manifest 并重算 content_sha256，用于构造"结构坏但摘要自洽"的用例。"""
    manifest_path = snapshot_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(payload)
    payload["content_sha256"] = FETCH.manifest_content_sha256(payload)
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _tamper_manifest_without_rehash(snapshot_dir: Path, mutate: Any) -> None:
    manifest_path = snapshot_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(payload)
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 数据模型 / 信息隔离
# ---------------------------------------------------------------------------


def test_dataset_ref_requires_full_revision_and_valid_split() -> None:
    ref = SWEBenchDatasetRef(revision=CUSTOM_REVISION, split="dev")
    assert ref.snapshot_dirname == (f"{DATASET_NAME.replace('/', '__')}__{CUSTOM_REVISION}")
    with pytest.raises(ValidationError, match="revision"):
        SWEBenchDatasetRef(revision="b" * 39, split="dev")
    with pytest.raises(ValidationError):
        SWEBenchDatasetRef(revision=CUSTOM_REVISION, split="train")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SWEBenchDatasetRef.model_validate({"revision": CUSTOM_REVISION, "split": "dev", "extra": 1})


def test_inference_and_evaluation_field_sets_are_disjoint(
    tmp_path: Path,
) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    loader = _loader(snapshot_root)
    inference = loader.get_inference_task("dev", "sqlfluff__sqlfluff-1625")
    evaluation = loader.get_evaluation_task("dev", "sqlfluff__sqlfluff-1625")

    inference_fields = set(type(inference).model_fields)
    evaluation_fields = set(type(evaluation).model_fields)
    shared = inference_fields & evaluation_fields
    # 只允许身份字段同时存在：否则评测侧能拿到推理字段（或反过来）。
    assert shared == set(INFERENCE_IDENTITY_FIELDS)
    assert inference_fields.isdisjoint(FORBIDDEN_INFERENCE_FIELDS)
    # 评测字段里必须有 gold/test 内容，且推理侧没有对应字段。
    payload = evaluation.model_dump()
    for forbidden in ("gold_patch", "test_patch", "fail_to_pass", "pass_to_pass"):
        assert forbidden in payload
        assert forbidden not in inference_fields


def test_task_module_does_not_import_evaluation_module() -> None:
    """推理模块的 import 闭包里不得出现评测模块（静态层硬规则）。"""
    task_module = sys.modules["codepilot.eval.swebench_task"]
    assert task_module.__file__ is not None
    source = Path(task_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name.startswith("codepilot.eval.swebench") for name in imported)
    # 允许依赖 sandbox.env_spec 等基础类型；不允许 swebench/datasets/pyarrow。
    assert not any(name.split(".")[0] in {"swebench", "datasets", "pyarrow"} for name in imported)


def test_loader_import_does_not_pull_optional_ml_dependencies() -> None:
    # 生产 Loader 完全离线；主 venv 没有 swebench/datasets/pyarrow。
    assert "swebench" not in sys.modules
    assert "datasets" not in sys.modules
    assert "pyarrow" not in sys.modules


def test_inference_task_cannot_serialize_gold(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    loader = _loader(snapshot_root)
    inference = loader.get_inference_task("dev", "sqlfluff__sqlfluff-1625")
    evaluation = loader.get_evaluation_task("dev", "sqlfluff__sqlfluff-1625")

    serialized = inference.model_dump_json()
    rendered = repr(inference)
    gold_lines = [
        line[1:].strip()
        for line in evaluation.gold_patch.splitlines()
        if line.startswith("+") and not line.startswith("+++") and len(line.strip()) > 12
    ]
    assert gold_lines, "fixture gold patch 应至少有一行可识别的添加内容"
    for marker in gold_lines:
        assert marker not in serialized
        assert marker not in rendered
    for forbidden in FORBIDDEN_INFERENCE_FIELDS:
        assert forbidden not in serialized

    # extra="forbid" 让"手工塞 patch"在构造期就失败。
    with pytest.raises(ValidationError):
        SWEBenchInferenceTask.model_validate({**inference.model_dump(), "patch": "diff"})
    with pytest.raises(ValidationError):
        SWEBenchInferenceTask.model_validate({**inference.model_dump(), "hints_text": "fix this"})


def test_inference_task_identity_and_properties(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    task = _loader(snapshot_root).get_inference_task("dev", "sqlfluff__sqlfluff-1625")
    assert task.task_id == task.instance_id
    assert task.issue == task.problem_statement
    assert task.repository == "https://github.com/sqlfluff/sqlfluff.git"
    assert task.mode == "swebench"
    assert task.dataset.revision == CUSTOM_REVISION
    assert task.dataset.split == "dev"
    # instance id 必须由 repo 派生。
    with pytest.raises(ValidationError, match="instance_id"):
        SWEBenchInferenceTask.model_validate({**task.model_dump(), "repo": "django/django"})


def test_evaluation_task_carries_gold_and_hashes(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    row = _rows("dev")[0]
    task: SWEBenchEvaluationTask = _loader(snapshot_root).get_evaluation_task(
        "dev", row["instance_id"]
    )
    assert task.gold_patch == row["patch"]
    assert task.test_patch == row["test_patch"]
    assert task.fail_to_pass == tuple(row["FAIL_TO_PASS"])
    assert task.pass_to_pass == tuple(row["PASS_TO_PASS"])
    assert task.gold_patch_sha256 == FETCH._sha256(row["patch"].encode())
    with pytest.raises(ValidationError):
        SWEBenchEvaluationTask.model_validate({**task.model_dump(), "gold_patch_sha256": "0" * 64})


def test_inference_task_does_not_accept_hints_text_in_payload(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    loader = _loader(snapshot_root)
    row = _rows("dev")[0]
    # 原始行里有 hints_text，但任务对象上没有字段能承载它。
    assert "hints_text" in row
    task = loader.get_inference_task("dev", row["instance_id"])
    assert "hints_text" not in type(task).model_fields
    assert "hints_text" not in task.model_dump_json()


# ---------------------------------------------------------------------------
# 加载 / 稳定性
# ---------------------------------------------------------------------------


def test_load_dataset_list_instances_sorted_and_idempotent(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    loader = _loader(snapshot_root)
    assert loader.load_dataset("dev") is None
    assert loader.load_dataset("dev") is None
    ids = loader.list_instances("dev")
    assert ids == tuple(sorted(ids))
    assert ids == tuple(sorted(row["instance_id"] for row in _rows("dev")))
    assert len(loader.list_instances("test")) == len(_rows("test"))


def test_get_unknown_instance_raises_dataset_error(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    loader = _loader(snapshot_root)
    with pytest.raises(SWEBenchDatasetError, match="不存在"):
        loader.get_inference_task("dev", "django__django-99999")
    with pytest.raises(SWEBenchDatasetError, match="split"):
        loader.load_dataset("train")  # type: ignore[arg-type]


def test_validate_dataset_report(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    loader = _loader(snapshot_root)
    report = loader.validate_dataset()
    assert report.split_rows == {"dev": len(_rows("dev")), "test": len(_rows("test"))}
    assert report.official_sizes_match is False
    assert report.cache_identity == loader.cache_identity
    manifest = loader.get_dataset_manifest()
    assert report.jsonl_sha256["dev"] == manifest.splits["dev"].jsonl_sha256
    assert report.content_sha256 == manifest.content_sha256


def test_manifest_identity_and_revision_changes_cache_identity(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    another_revision = "c" * 40
    _build_snapshot(tmp_path, revision=another_revision)
    first = _loader(snapshot_root)
    second = _loader(snapshot_root, revision=another_revision)

    manifest = first.get_dataset_manifest()
    assert manifest.dataset_name == CUSTOM_DATASET
    assert manifest.revision == CUSTOM_REVISION
    assert first.cache_identity != second.cache_identity
    assert (
        first.get_inference_task("dev", "sqlfluff__sqlfluff-1625").dataset.revision
        != second.get_inference_task("dev", "sqlfluff__sqlfluff-1625").dataset.revision
    )


# ---------------------------------------------------------------------------
# 缓存完整性与 revision 绑定（fail closed，不静默重下/换版本）
# ---------------------------------------------------------------------------


def test_missing_snapshot_is_cache_error_with_fetch_hint(tmp_path: Path) -> None:
    loader = _loader(tmp_path / "absent")
    with pytest.raises(SWEBenchCacheError, match="fetch_swebench_lite"):
        loader.get_dataset_manifest()


def test_revision_change_does_not_fall_back(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    loader = _loader(snapshot_root, revision="c" * 40)
    with pytest.raises(SWEBenchCacheError, match="不得回退"):
        loader.get_dataset_manifest()


def test_manifest_content_hash_tamper_is_cache_error(tmp_path: Path) -> None:
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        payload["splits"]["dev"]["rows"] += 1

    _tamper_manifest_without_rehash(snapshot_dir, mutate)
    with pytest.raises(SWEBenchCacheError, match="内容摘要不符"):
        _loader(snapshot_root).get_dataset_manifest()


def test_jsonl_sha256_tamper_is_cache_error(tmp_path: Path) -> None:
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path)
    path = snapshot_dir / "test.jsonl"
    payload = path.read_bytes()
    assert b"django" in payload
    path.write_bytes(payload.replace(b"django", b"djang0", 1))
    with pytest.raises(SWEBenchCacheError, match="sha256 不符"):
        _loader(snapshot_root).load_dataset("test")


def test_jsonl_byte_count_tamper_is_cache_error(tmp_path: Path) -> None:
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path)
    path = snapshot_dir / "dev.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(SWEBenchCacheError, match="字节数"):
        _loader(snapshot_root).load_dataset("dev")


def test_missing_files_are_cache_errors(tmp_path: Path) -> None:
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path)
    (snapshot_dir / "manifest.json").unlink()
    with pytest.raises(SWEBenchCacheError, match=r"manifest\.json"):
        _loader(snapshot_root).get_dataset_manifest()

    snapshot_root, snapshot_dir = _build_snapshot(tmp_path / "second")
    (snapshot_dir / "dev.jsonl").unlink()
    with pytest.raises(SWEBenchCacheError, match=r"dev\.jsonl"):
        _loader(snapshot_root).load_dataset("dev")


def test_manifest_revision_mismatch_is_rejected(tmp_path: Path) -> None:
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path)
    other = "d" * 40

    def mutate(payload: dict[str, Any]) -> None:
        payload["revision"] = other

    _mutate_manifest(snapshot_dir, mutate)
    with pytest.raises(SWEBenchDatasetError, match="revision"):
        _loader(snapshot_root).get_dataset_manifest()


def test_unsupported_manifest_schema_version_is_rejected(tmp_path: Path) -> None:
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        payload["schema_version"] = 2

    # schema_version 不参与 content_sha256，所以摘要仍然自洽，必须由模型层拒绝。
    _mutate_manifest(snapshot_dir, mutate)
    with pytest.raises(SWEBenchDatasetError, match="schema_version"):
        _loader(snapshot_root).get_dataset_manifest()


def test_official_split_sizes_are_enforced(tmp_path: Path) -> None:
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path)
    target = _snapshot_dir(snapshot_root, DATASET_NAME, DATASET_REVISION)
    snapshot_dir.rename(target)

    def mutate(payload: dict[str, Any]) -> None:
        payload["dataset_name"] = DATASET_NAME
        payload["revision"] = DATASET_REVISION

    _mutate_manifest(target, mutate)
    loader = SWEBenchLoader(snapshot_root, env_spec_snapshot=ENV_SPECS)
    with pytest.raises(SWEBenchDatasetError, match="与固定事实"):
        loader.load_dataset("dev")


# ---------------------------------------------------------------------------
# 行级 schema：缺字段 / 类型错 / 重复 / 非法身份
# ---------------------------------------------------------------------------


def _rewrite_split(snapshot_dir: Path, split: str, rows: list[dict[str, Any]]) -> None:
    """直接重写 JSONL 并同步 manifest 摘要，用于构造"摘要自洽但内容非法"的用例。"""
    payload = FETCH.encode_jsonl(rows)
    (snapshot_dir / f"{split}.jsonl").write_bytes(payload)

    def mutate(manifest: dict[str, Any]) -> None:
        manifest["splits"][split]["jsonl_sha256"] = FETCH._sha256(payload)
        manifest["splits"][split]["jsonl_bytes"] = len(payload)
        manifest["splits"][split]["rows"] = len(rows)
        manifest["official_split_sizes"][split] = len(rows)

    _mutate_manifest(snapshot_dir, mutate)


def _load_with_rows(
    tmp_path: Path, *, dev_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]] | None = None
) -> SWEBenchLoader:
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path)
    _rewrite_split(snapshot_dir, "dev", dev_rows)
    if test_rows is not None:
        _rewrite_split(snapshot_dir, "test", test_rows)
    return _loader(snapshot_root)


def test_duplicate_instance_id_rejected(tmp_path: Path) -> None:
    rows = _rows("dev")
    loader = _load_with_rows(tmp_path, dev_rows=[rows[0], dict(rows[0])])
    with pytest.raises(SWEBenchDatasetError, match="重复 instance_id"):
        loader.load_dataset("dev")


def test_invalid_repo_rejected(tmp_path: Path) -> None:
    rows = _rows("dev")
    rows[0]["repo"] = "sqlfluff"  # 缺 owner
    loader = _load_with_rows(tmp_path, dev_rows=rows)
    with pytest.raises(SWEBenchDatasetError, match="repo"):
        loader.load_dataset("dev")


def test_invalid_base_commit_rejected(tmp_path: Path) -> None:
    rows = _rows("dev")
    rows[0]["base_commit"] = "abc123"
    loader = _load_with_rows(tmp_path, dev_rows=rows)
    with pytest.raises(SWEBenchDatasetError, match="base_commit"):
        loader.load_dataset("dev")


def test_missing_required_field_rejected(tmp_path: Path) -> None:
    rows = _rows("dev")
    del rows[0]["patch"]
    loader = _load_with_rows(tmp_path, dev_rows=rows)
    with pytest.raises(SWEBenchDatasetError, match="缺字段"):
        loader.load_dataset("dev")


def test_unnormalized_fail_to_pass_string_rejected(tmp_path: Path) -> None:
    rows = _rows("dev")
    rows[0]["FAIL_TO_PASS"] = json.dumps(rows[0]["FAIL_TO_PASS"])
    loader = _load_with_rows(tmp_path, dev_rows=rows)
    with pytest.raises(SWEBenchDatasetError, match="字符串数组"):
        loader.load_dataset("dev")


def test_wrong_field_type_rejected(tmp_path: Path) -> None:
    rows = _rows("dev")
    rows[0]["problem_statement"] = 123
    loader = _load_with_rows(tmp_path, dev_rows=rows)
    with pytest.raises(SWEBenchDatasetError, match="problem_statement"):
        loader.load_dataset("dev")


def test_row_count_mismatch_rejected(tmp_path: Path) -> None:
    rows = _rows("dev")
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path, dev_rows=rows)
    # 删掉一行但保留 manifest 声明：hash 不再匹配 → CacheError；用重算摘要模拟"生成器
    # 声称 3 行、文件却只有 2 行"的结构不一致 → DatasetError。
    lines = (snapshot_dir / "dev.jsonl").read_text(encoding="utf-8").splitlines()
    payload = ("\n".join(lines[:-1]) + "\n").encode()
    (snapshot_dir / "dev.jsonl").write_bytes(payload)

    def mutate(manifest: dict[str, Any]) -> None:
        manifest["splits"]["dev"]["jsonl_sha256"] = FETCH._sha256(payload)
        manifest["splits"]["dev"]["jsonl_bytes"] = len(payload)

    _mutate_manifest(snapshot_dir, mutate)
    with pytest.raises(SWEBenchDatasetError, match="行数"):
        _loader(snapshot_root).load_dataset("dev")


# ---------------------------------------------------------------------------
# 环境规格投影（EnvSpec 复用，不 import swebench/datasets/pyarrow）
# ---------------------------------------------------------------------------


def test_env_spec_projection_handles_old_python_and_conda(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    loader = _loader(snapshot_root)

    django = loader.get_inference_task("test", "django__django-10914")
    assert django.env_spec.python_version == "3.10"  # 官方 3.6 → 通用 base 的夹取

    sympy = loader.get_inference_task("test", "sympy__sympy-18057")
    joined = " ".join(sympy.env_spec.install)
    assert "mpmath" in joined and "flake8" in joined  # pip_packages 不能丢

    matplotlib = loader.get_inference_task("test", "matplotlib__matplotlib-22711")
    assert matplotlib.env_spec.python_version == "3.11"
    assert "--junitxml=/out/junit.xml" in matplotlib.env_spec.test_command
    assert all("environment.yml" not in command for command in matplotlib.env_spec.install)


def test_missing_env_spec_snapshot_fails_at_init(tmp_path: Path) -> None:
    with pytest.raises(SWEBenchDatasetError, match="环境规格快照不存在"):
        SWEBenchLoader(
            tmp_path,
            dataset_name=CUSTOM_DATASET,
            revision=CUSTOM_REVISION,
            env_spec_snapshot=tmp_path / "missing.json",
        )


def test_env_spec_snapshot_missing_repo_version_fails_closed(tmp_path: Path) -> None:
    payload = json.loads(ENV_SPECS.read_text(encoding="utf-8"))
    del payload["specs"]["matplotlib/matplotlib"]
    spec_path = tmp_path / "env-specs-partial.json"
    spec_path.write_text(json.dumps(payload), encoding="utf-8")

    snapshot_root, _ = _build_snapshot(tmp_path)
    loader = _loader(snapshot_root, env_spec_snapshot=spec_path)
    with pytest.raises(SWEBenchDatasetError, match="环境规格快照缺少"):
        loader.load_dataset("test")


def test_pip_packages_are_installed_exactly_once(tmp_path: Path) -> None:
    """跨 Owner 回归：from_swebench_spec 已负责 pip_packages，loader 不得再追加一次。

    重复命令会进入 EnvSpec.canonical_json（镜像 cache key/env 身份），属于"环境
    身份被污染"，比测试红更难发现。
    """
    payload = json.loads(ENV_SPECS.read_text(encoding="utf-8"))
    astropy_spec = payload["specs"]["astropy/astropy"]["5.1"]
    expected = "python -m pip install " + " ".join(astropy_spec["pip_packages"])

    snapshot_root, _ = _build_snapshot(tmp_path)
    task = _loader(snapshot_root).get_inference_task("test", "astropy__astropy-14182")
    install = task.env_spec.install
    assert install.count(expected) == 1
    # 第一份 pip_packages 里的包不应在第二条命令里再出现。
    first_package = astropy_spec["pip_packages"][0]
    matching = [command for command in install if first_package in command]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# fetch 脚本：--verify-only / 缺 pyarrow / 固定 revision
# ---------------------------------------------------------------------------


def test_fetch_module_does_not_import_pyarrow_at_import_time() -> None:
    # 主 venv 没有 pyarrow；脚本必须能在 import 时正常工作（runpy/import 检查）。
    assert not hasattr(FETCH, "parquet")
    assert not hasattr(FETCH, "pyarrow")


def test_fetch_verify_snapshot_offline(tmp_path: Path) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    report = FETCH.verify_snapshot(
        snapshot_root, revision=CUSTOM_REVISION, dataset_name=CUSTOM_DATASET
    )
    assert report["split_rows"] == {"dev": len(_rows("dev")), "test": len(_rows("test"))}
    assert report["revision"] == CUSTOM_REVISION


def test_fetch_verify_detects_parquet_tamper(tmp_path: Path) -> None:
    snapshot_root, snapshot_dir = _build_snapshot(tmp_path)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    parquet_name = manifest["splits"]["dev"]["parquet_filename"]
    (snapshot_root / parquet_name).write_bytes(b"x")
    with pytest.raises(FETCH.FetchError, match="sha256"):
        FETCH.verify_snapshot(snapshot_root, revision=CUSTOM_REVISION, dataset_name=CUSTOM_DATASET)


def test_fetch_skip_when_snapshot_is_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot_root, _ = _build_snapshot(tmp_path)
    # fetch() 只服务固定官方 revision；这里把两个模块常量改成 fixture 身份，
    # 验证"已存在且校验通过 → 不下载/不 import pyarrow"的幂等分支。
    monkeypatch.setattr(FETCH, "DATASET_NAME", CUSTOM_DATASET)
    monkeypatch.setattr(FETCH, "DATASET_REVISION", CUSTOM_REVISION)
    # verify_snapshot 的默认参数在 def 时绑定官方身份，这里直接替换为"校验通过"，
    # 以测试幂等分支本身；离线校验已由 test_fetch_verify_snapshot_offline 覆盖。
    monkeypatch.setattr(FETCH, "verify_snapshot", lambda *args, **kwargs: {})
    path = FETCH.fetch(snapshot_root, revision=CUSTOM_REVISION)
    assert path.is_dir()


def test_fetch_rejects_non_fixed_revision(tmp_path: Path) -> None:
    with pytest.raises(FETCH.FetchError, match="固定 revision"):
        FETCH.fetch(tmp_path, revision="e" * 40)


def test_fetch_missing_pyarrow_gives_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    with pytest.raises(FETCH.FetchError, match="pyarrow"):
        FETCH._import_pyarrow()  # 专门测试可选依赖错误边界


def test_fetch_main_verify_only_fails_loudly_on_missing_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = FETCH.main(["--verify-only", "--snapshot-root", str(tmp_path / "absent")])
    assert code == 1
    assert "快照不存在" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 真实官方快照（cache 资产存在时才跑；gitignored，由 fetch 脚本生成）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (
        REAL_SNAPSHOT_ROOT
        / f"{DATASET_NAME.replace('/', '__')}__{DATASET_REVISION}"
        / "manifest.json"
    ).is_file(),
    reason="真实缓存快照未生成（gitignored；运行 scripts/fetch_swebench_lite.py 后可用）",
)
def test_real_official_snapshot_has_23_and_300_rows() -> None:
    loader = SWEBenchLoader(REAL_SNAPSHOT_ROOT)
    report = loader.validate_dataset()
    assert report.split_rows == OFFICIAL_SPLIT_SIZES
    assert report.official_sizes_match is True
    assert len(loader.list_instances("dev")) == 23
    assert len(loader.list_instances("test")) == 300
    task = loader.get_inference_task("test", "django__django-10914")
    assert task.dataset.revision == DATASET_REVISION
    assert task.problem_statement.strip()
