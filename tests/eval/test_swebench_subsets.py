"""E-18 §12.4：固定 30/50 子集的 10 条单测（逐条对应任务书条目）。

10 条分别验证：

1. 开发集恰好 30 条；
2. 测试集恰好 50 条；
3. 两组交集为空；
4. 全部 Instance ID 都能在固定 revision 的 test split 快照里查到；
5. 无重复；
6. 全部来自声明的数据源和版本（并且确实做了 repo 分层，不被单仓库主导）；
7. 抽样可重复（同 seed 同输出、逐字节一致）；
8. 换 seed 会按预期改变选取，但仍 30/50、无交集；
9. 列表文件 hash 可核验（manifest 的 sha256 与实际文件一致）；
10. 数据集版本变化不会静默沿用旧列表（revision 不匹配时拒绝）。

除第 4 条必须读真实 3.6MB 快照外，其余测试只依赖已提交文件或临时合成快照。
快照是 gitignored 缓存，缺失时第 4 条明确 skip，绝不伪造通过。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections import Counter
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "make_swebench_subsets.py"
SUBSET_DIR = ROOT / "configs" / "subsets"
SNAPSHOT_DIR = (
    ROOT
    / "cache"
    / "swebench"
    / "princeton-nlp__SWE-bench_Lite__6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
)

DEV_FILE = "swebench-lite-dev30.json"
TEST_FILE = "swebench-lite-test50.json"
OFFICIAL_DEV_FILE = "swebench-lite-official-dev23.json"
MANIFEST_FILE = "manifest.json"

DEV_COUNT = 30
TEST_COUNT = 50
OFFICIAL_DEV_COUNT = 23
SEED = 20261019
DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
DATASET_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"

#: 官方 Lite test split 的全部 12 个仓库；用于验证"repo 分层"不是只覆盖头部仓库。
TEST_REPOS = {
    "django/django",
    "sympy/sympy",
    "matplotlib/matplotlib",
    "scikit-learn/scikit-learn",
    "pytest-dev/pytest",
    "sphinx-doc/sphinx",
    "astropy/astropy",
    "psf/requests",
    "pylint-dev/pylint",
    "pydata/xarray",
    "mwaskom/seaborn",
    "pallets/flask",
}


def _load_script() -> ModuleType:
    """按文件路径加载生成脚本：``scripts/`` 不是包，也不该为了测试变成包。"""
    if "make_swebench_subsets" in sys.modules:
        return sys.modules["make_swebench_subsets"]
    spec = importlib.util.spec_from_file_location("make_swebench_subsets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


subsets = _load_script()


# --------------------------------------------------------------------------- 共享读取


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _committed_ids(filename: str) -> list[str]:
    return [str(item) for item in _load_json(SUBSET_DIR / filename)["instance_ids"]]


def _manifest() -> dict[str, Any]:
    return _load_json(SUBSET_DIR / MANIFEST_FILE)


def _repo_of(instance_id: str) -> str:
    """``django__django-11099`` -> ``django/django``。

    owner 里可能带连字符（``pylint-dev``），因此只能从**最后一个** ``-`` 切编号，
    再从 ``__`` 切 owner/repo。
    """
    base, _, _number = instance_id.rpartition("-")
    owner, _, name = base.partition("__")
    return f"{owner}/{name}"


def _synthetic_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """构造与真实分布同形的 300+23 条合成记录，供不依赖缓存的算法测试使用。

    仓库规模取自真实 test split（django 114、sympy 77……），每个仓库拆成两个
    version 组，用来验证"同 version 连续编号任务被分散"。
    """
    test_counts = {
        "django/django": 114,
        "sympy/sympy": 77,
        "matplotlib/matplotlib": 23,
        "scikit-learn/scikit-learn": 23,
        "pytest-dev/pytest": 17,
        "sphinx-doc/sphinx": 16,
        "astropy/astropy": 6,
        "psf/requests": 6,
        "pylint-dev/pylint": 6,
        "pydata/xarray": 5,
        "mwaskom/seaborn": 4,
        "pallets/flask": 3,
    }
    test_rows: list[dict[str, Any]] = []
    for repo, count in test_counts.items():
        owner, name = repo.split("/", 1)
        for index in range(count):
            test_rows.append(
                {
                    "instance_id": f"{owner}__{name}-{index}",
                    "repo": repo,
                    "version": "1.0" if index % 2 == 0 else "2.0",
                }
            )
    dev_repos = [
        "marshmallow-code/marshmallow",
        "pvlib/pvlib-python",
        "pydicom/pydicom",
        "pylint-dev/astroid",
        "pyvista/pyvista",
        "sqlfluff/sqlfluff",
    ]
    dev_rows: list[dict[str, Any]] = []
    for offset, repo in enumerate(dev_repos):
        owner, name = repo.split("/", 1)
        for index in range(4 if offset else 3):
            dev_rows.append(
                {"instance_id": f"{owner}__{name}-{index}", "repo": repo, "version": "1.0"}
            )
    assert len(test_rows) == 300
    assert len(dev_rows) == 23
    return test_rows, dev_rows


def _write_synthetic_snapshot(root: Path, *, revision: str = DATASET_REVISION) -> Path:
    """把合成行写成与真实快照同格式的 manifest/jsonl。"""
    root.mkdir(parents=True, exist_ok=True)
    test_rows, dev_rows = _synthetic_rows()
    for split, rows in (("dev", dev_rows), ("test", test_rows)):
        text = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
        (root / f"{split}.jsonl").write_text(text + "\n", encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": 1,
        "dataset_name": DATASET_NAME,
        "revision": revision,
        "official_split_sizes": {"dev": 23, "test": 300},
        "splits": {
            split: {
                "jsonl_sha256": hashlib.sha256((root / f"{split}.jsonl").read_bytes()).hexdigest()
            }
            for split in ("dev", "test")
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return root


# --------------------------------------------------------------------------- 1/2/3 数量与划分


def test_dev_subset_has_exactly_30_instances() -> None:
    """条目 1：开发集恰好 30 条，且 count 字段与列表一致。"""
    ids = _committed_ids(DEV_FILE)
    payload = _load_json(SUBSET_DIR / DEV_FILE)
    assert len(ids) == DEV_COUNT
    assert payload["count"] == DEV_COUNT
    assert payload["name"] == "swebench-lite-dev30"
    assert payload["source_split"] == "test"


def test_test_subset_has_exactly_50_instances() -> None:
    """条目 2：测试集恰好 50 条，且 count 字段与列表一致。"""
    ids = _committed_ids(TEST_FILE)
    payload = _load_json(SUBSET_DIR / TEST_FILE)
    assert len(ids) == TEST_COUNT
    assert payload["count"] == TEST_COUNT
    assert payload["name"] == "swebench-lite-test50"
    assert payload["source_split"] == "test"


def test_dev_and_test_subsets_are_disjoint() -> None:
    """条目 3：两组交集为空；官方 dev23 也不许混入这两个自定义子集。"""
    dev_ids = set(_committed_ids(DEV_FILE))
    test_ids = set(_committed_ids(TEST_FILE))
    official_ids = set(_committed_ids(OFFICIAL_DEV_FILE))
    assert dev_ids & test_ids == set()
    assert len(dev_ids | test_ids) == DEV_COUNT + TEST_COUNT
    assert dev_ids & official_ids == set()
    assert test_ids & official_ids == set()
    manifest = _manifest()
    assert manifest["overlap"][f"{DEV_FILE}∩{TEST_FILE}"]["count"] == 0


# --------------------------------------------------------------------------- 4/5/6 来源与唯一性


@pytest.mark.skipif(
    not SNAPSHOT_DIR.is_dir(),
    reason="真实数据集快照是 gitignored 缓存；缺失时无法验证成员资格（不伪造通过）",
)
def test_every_instance_exists_in_fixed_revision_test_split() -> None:
    """条目 4：每个 Instance ID 都真实存在于固定 revision 的官方 test split。"""
    official: set[str] = set()
    for line in (SNAPSHOT_DIR / "test.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            official.add(str(json.loads(line)["instance_id"]))
    assert len(official) == 300
    for filename in (DEV_FILE, TEST_FILE):
        missing = set(_committed_ids(filename)) - official
        assert not missing, f"{filename} 含快照里不存在的实例: {sorted(missing)[:3]}"


def test_no_duplicate_instance_ids() -> None:
    """条目 5：三个清单内部都无重复，且 count 与唯一数一致。"""
    for filename in (DEV_FILE, TEST_FILE, OFFICIAL_DEV_FILE):
        ids = _committed_ids(filename)
        assert len(ids) == len(set(ids)), f"{filename} 有重复实例"
        assert ids == sorted(ids), f"{filename} 未按 instance_id 升序（便于 diff）"
        assert _load_json(SUBSET_DIR / filename)["count"] == len(set(ids))


def test_all_instances_come_from_declared_dataset_and_split() -> None:
    """条目 6：数据源/版本/split 全部与声明一致，并且确实做了 repo 分层轮转。"""
    manifest = _manifest()
    assert manifest["dataset"]["name"] == DATASET_NAME
    assert manifest["dataset"]["revision"] == DATASET_REVISION
    assert manifest["dataset"]["official_split_sizes"] == {"dev": 23, "test": 300}
    assert manifest["seed"] == SEED
    assert manifest["source_split"] == "test"

    for filename in (DEV_FILE, TEST_FILE, OFFICIAL_DEV_FILE):
        payload = _load_json(SUBSET_DIR / filename)
        assert payload["dataset"] == {"name": DATASET_NAME, "revision": DATASET_REVISION}
        expected_split = "dev" if filename == OFFICIAL_DEV_FILE else "test"
        assert payload["source_split"] == expected_split
        # instance_id 自带 repo 前缀，必须与 repo_distribution 对得上。
        distribution = Counter(_repo_of(iid) for iid in payload["instance_ids"])
        assert dict(distribution) == payload["repo_distribution"]

    # 分层轮转的可见结果：两个自定义子集都覆盖官方 test 的全部 12 个仓库，
    # 且没有单仓库主导（django 在 test50 里 6/50，低于 20% 上限）。
    for filename, count in ((DEV_FILE, DEV_COUNT), (TEST_FILE, TEST_COUNT)):
        distribution = Counter(_repo_of(iid) for iid in _committed_ids(filename))
        assert set(distribution) == TEST_REPOS, f"{filename} 未覆盖全部仓库"
        assert max(distribution.values()) <= count * 0.2, f"{filename} 被单仓库主导"


# --------------------------------------------------------------------------- 7/8 可重复性与 seed


def test_sampling_is_reproducible_byte_for_byte(tmp_path: Path) -> None:
    """条目 7：同 seed 同输入生成逐字节相同的文件；轮转顺序也保持稳定。"""
    snapshot = _write_synthetic_snapshot(tmp_path / "snapshot")
    first = tmp_path / "first"
    second = tmp_path / "second"
    subsets.write_subsets(first, snapshot, seed=SEED, generated_at="2026-10-19T00:00:00Z")
    subsets.write_subsets(second, snapshot, seed=SEED, generated_at="2026-10-19T00:00:00Z")
    for filename in (DEV_FILE, TEST_FILE, OFFICIAL_DEV_FILE, MANIFEST_FILE):
        assert (first / filename).read_bytes() == (second / filename).read_bytes(), filename
    # 换生成时间只应影响 manifest，不应影响三个子集文件。
    third = tmp_path / "third"
    subsets.write_subsets(third, snapshot, seed=SEED, generated_at="2027-01-01T12:00:00Z")
    for filename in (DEV_FILE, TEST_FILE, OFFICIAL_DEV_FILE):
        assert (first / filename).read_bytes() == (third / filename).read_bytes(), filename
    # check_committed 应确认这批文件与快照重算一致。
    assert subsets.check_committed(first, snapshot) == []


def test_changing_seed_changes_selection(tmp_path: Path) -> None:
    """条目 8：换 seed 会改变选取，但两组仍 30/50、互不重叠、仓库仍分层。"""
    snapshot = _write_synthetic_snapshot(tmp_path / "snapshot")
    base = tmp_path / "seed-base"
    other = tmp_path / "seed-other"
    subsets.write_subsets(base, snapshot, seed=SEED, generated_at="2026-10-19T00:00:00Z")
    subsets.write_subsets(other, snapshot, seed=SEED + 1, generated_at="2026-10-19T00:00:00Z")
    base_dev = [str(item) for item in _load_json(base / DEV_FILE)["instance_ids"]]
    other_dev = [str(item) for item in _load_json(other / DEV_FILE)["instance_ids"]]
    base_test = [str(item) for item in _load_json(base / TEST_FILE)["instance_ids"]]
    other_test = [str(item) for item in _load_json(other / TEST_FILE)["instance_ids"]]
    assert base_dev != other_dev, "换 seed 后开发集完全没有变化，说明 seed 没有生效"
    assert base_test != other_test, "换 seed 后测试集完全没有变化，说明 seed 没有生效"
    for dev_ids, test_ids in ((base_dev, base_test), (other_dev, other_test)):
        assert len(dev_ids) == DEV_COUNT and len(test_ids) == TEST_COUNT
        assert set(dev_ids) & set(test_ids) == set()
        assert {_repo_of(iid) for iid in dev_ids} == TEST_REPOS
    # 同时验证轮转顺序：全局相邻条目来自不同仓库（近重复任务被分散）。
    test_rows, dev_rows = _synthetic_rows()
    plan = subsets.build_plan(test_rows, dev_rows, seed=SEED)
    rotation = list(plan.selection_order)
    assert all(_repo_of(left) != _repo_of(right) for left, right in pairwise(rotation))


# --------------------------------------------------------------------------- 9/10 hash 与版本迁移


def test_manifest_hashes_match_actual_files() -> None:
    """条目 9：manifest 里每个文件的 sha256 / 实例列表 hash 都能重算核对。"""
    manifest = _manifest()
    recorded = manifest["files"]
    assert set(recorded) == {DEV_FILE, TEST_FILE, OFFICIAL_DEV_FILE}
    assert manifest["generated_at"].endswith("Z")
    assert manifest["algorithm"]["model_results_used"] is False
    for filename, record in recorded.items():
        raw = (SUBSET_DIR / filename).read_bytes()
        assert record["sha256"] == hashlib.sha256(raw).hexdigest(), filename
        ids = [str(item) for item in json.loads(raw)["instance_ids"]]
        assert record["count"] == len(ids)
        assert (
            record["instance_list_sha256"]
            == hashlib.sha256(("\n".join(ids) + "\n").encode("utf-8")).hexdigest()
        )
    # 官方 split 与自定义划分的区别必须写在 manifest 里（E-18 验收点）。
    difference = manifest["official_split_vs_custom"]
    for key in ("official_test", "official_dev", "custom_dev30", "custom_test50"):
        assert key in difference
    assert difference["not_selected_by_model_success"]


def test_revision_mismatch_is_rejected_not_silently_reused(tmp_path: Path) -> None:
    """条目 10：数据集 revision 变化时必须拒绝，不能静默沿用旧 30/50 清单。"""
    wrong_revision = "0" * 40
    stale_snapshot = _write_synthetic_snapshot(tmp_path / "stale-snapshot", revision=wrong_revision)
    with pytest.raises(subsets.SubsetError):
        subsets.write_subsets(tmp_path / "out", stale_snapshot, generated_at="2026-10-19T00:00:00Z")

    # 已提交清单被迁移到新 revision、但快照仍是旧 revision 时，check_committed
    # 必须报出不一致，而不是"重算一致"。
    good_snapshot = _write_synthetic_snapshot(tmp_path / "good-snapshot")
    out = tmp_path / "migrated"
    subsets.write_subsets(out, good_snapshot, generated_at="2026-10-19T00:00:00Z")
    manifest_path = out / MANIFEST_FILE
    manifest = _load_json(manifest_path)
    manifest["dataset"]["revision"] = wrong_revision
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    problems = subsets.check_committed(out, good_snapshot)
    assert any("revision" in problem for problem in problems), problems
