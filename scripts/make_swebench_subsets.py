"""从官方 SWE-bench Lite test split 生成固定 30/50 子集（E-18）。

设计目标只有三条，按优先级排列：

1. **可重复**：同一份数据集快照 + 固定 seed 必须得到逐字节相同的子集文件。
   因此这里不使用进程级随机数、不依赖 ``random`` 的版本行为，也不用"按当前时间
   打散"这类隐性输入；实例顺序全部由 ``sha256(seed|key)`` 决定。
2. **不重叠**：开发子集 30 题与测试子集 50 题从同一份轮转序列里先后切分，
   交集恒为空，不需要事后去重。
3. **不被单仓库主导、相邻任务尽量分散**：先在仓库之间轮转，再在每个仓库内部
   先在 version 组之间轮转、组内按哈希序取，避免同一 repo 同一 version 的
   连续编号任务成串进入子集。抽样与任何模型运行结果无关（明确不做 GLM 成功率挑选）。

官方 split 与自定义划分的区别写在 ``manifest.json`` 的 ``official_split_vs_custom``
里：官方 test 300 条是完整榜单口径，官方 dev 23 条只作参考保留；自定义 dev30 /
test50 是为了在固定实验预算下得到可复现的小批量口径，**不能**冒充官方 split。

用法::

    python scripts/make_swebench_subsets.py
    python scripts/make_swebench_subsets.py --check   # 只校验已提交文件是否与快照一致
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # pragma: no cover - 仅直接执行脚本时需要
    sys.path.insert(0, str(ROOT / "src"))

DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
DATASET_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
OFFICIAL_SPLIT_SIZES = {"dev": 23, "test": 300}

#: 固定 seed。改动它等于换一套实验口径，必须同步更新 manifest 与实验计划。
DEFAULT_SEED = 20261019
DEV_COUNT = 30
TEST_COUNT = 50

DEV_FILE = "swebench-lite-dev30.json"
TEST_FILE = "swebench-lite-test50.json"
OFFICIAL_DEV_FILE = "swebench-lite-official-dev23.json"
MANIFEST_FILE = "manifest.json"

ALGORITHM_ID = "repo-stratified-round-robin-v1"
SCHEMA_VERSION = 1

DEFAULT_SNAPSHOT_ROOT = (
    ROOT / "cache" / "swebench" / f"princeton-nlp__SWE-bench_Lite__{DATASET_REVISION}"
)
DEFAULT_OUT_DIR = ROOT / "configs" / "subsets"


# --------------------------------------------------------------------------- 基础工具


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def instance_list_sha256(instance_ids: Sequence[str]) -> str:
    """实例列表的稳定摘要：一行一个 id，带结尾换行。

    不带结尾换行会让 ``["a"]`` 与 ``["a\\n"]`` 之类的边界难以区分；用行列表
    而不是 JSON 是为了让"手工用 sha256sum 核对"这件事保持简单。
    """
    return _sha256_text("\n".join(instance_ids) + "\n")


def _stable_key(*parts: object) -> str:
    """跨平台、跨 Python 版本的确定性伪随机序。"""
    joined = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _normalize_text(value: object) -> str:
    return str(value).strip()


# --------------------------------------------------------------------------- 读快照


class SubsetError(RuntimeError):
    """快照或现有子集文件不满足生成前提。"""


def load_manifest(snapshot_root: Path) -> dict[str, Any]:
    path = Path(snapshot_root) / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SubsetError(f"无法读取数据集快照 manifest: {path}") from exc
    if not isinstance(payload, dict):
        raise SubsetError(f"{path} 顶层必须是对象")
    if payload.get("revision") != DATASET_REVISION:
        raise SubsetError(
            f"快照 revision 不符：{payload.get('revision')!r} != {DATASET_REVISION!r}；"
            "子集必须来自固定 revision，不能混用"
        )
    return payload


def load_split(snapshot_root: Path, split: str) -> list[dict[str, Any]]:
    """读取 ``<split>.jsonl`` 并做最小结构校验（fail closed）。"""
    path = Path(snapshot_root) / f"{split}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SubsetError(f"无法读取数据集快照: {path}") from exc
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise SubsetError(f"{path}:{line_no} 不是合法 JSON") from exc
        if not isinstance(row, dict):
            raise SubsetError(f"{path}:{line_no} 必须是对象")
        instance_id = _normalize_text(row.get("instance_id", ""))
        repo = _normalize_text(row.get("repo", ""))
        version = _normalize_text(row.get("version", ""))
        if not instance_id or not repo or not version:
            raise SubsetError(f"{path}:{line_no} 缺 instance_id/repo/version")
        if instance_id in seen:
            raise SubsetError(f"{path} 出现重复 instance_id: {instance_id}")
        seen.add(instance_id)
        rows.append({**row, "instance_id": instance_id, "repo": repo, "version": version})
    expected = OFFICIAL_SPLIT_SIZES.get(split)
    if expected is not None and len(rows) != expected:
        raise SubsetError(f"{split} split 行数不符：期望 {expected}，实际 {len(rows)}")
    return rows


# --------------------------------------------------------------------------- 抽样算法


def _repo_sequence(rows: Sequence[Mapping[str, Any]], seed: int) -> list[str]:
    """把一个仓库的实例排成"相邻条目尽量不是同 version"的顺序。

    先按 version 分组：组内条目按 ``sha256(seed|instance_id)`` 排序（相邻编号因此
    被哈希打散），再把各 version 组按轮转方式交错。这样同一仓库的连续选择会先在
    不同 version 之间轮转，而不是把某个 version 的相邻任务成串带走。
    """
    by_version: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        by_version[str(row["version"])].append(str(row["instance_id"]))
    if not by_version:
        return []
    ordered_versions = sorted(by_version, key=lambda version: _stable_key(seed, version))
    queues = {
        version: sorted(by_version[version], key=lambda iid: (_stable_key(seed, iid), iid))
        for version in ordered_versions
    }
    positions = dict.fromkeys(ordered_versions, 0)
    sequence: list[str] = []
    remaining = sum(len(items) for items in queues.values())
    while len(sequence) < remaining:
        for version in ordered_versions:
            position = positions[version]
            if position >= len(queues[version]):
                continue
            sequence.append(queues[version][position])
            positions[version] = position + 1
    return sequence


def build_rotation(rows: Sequence[Mapping[str, Any]], seed: int) -> list[str]:
    """全体实例的仓库级轮转序列。

    仓库顺序取"实例数降序、repo 升序"：确定、可解释，而且不会让最小仓库在轮转
    尾部只剩它的条目。真正决定单仓库占比上限的是轮转本身（每轮每仓库至多一条），
    与这个顺序无关。
    """
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["repo"])].append(row)
    repo_order = sorted(grouped, key=lambda repo: (-len(grouped[repo]), repo))
    sequences = {repo: _repo_sequence(grouped[repo], seed) for repo in repo_order}
    positions = dict.fromkeys(repo_order, 0)
    rotation: list[str] = []
    total = sum(len(items) for items in sequences.values())
    while len(rotation) < total:
        for repo in repo_order:
            position = positions[repo]
            if position >= len(sequences[repo]):
                continue
            rotation.append(sequences[repo][position])
            positions[repo] = position + 1
    return rotation


@dataclass(frozen=True, slots=True)
class SubsetPlan:
    """一次生成的全部结果。

    ``selection_order`` 保留轮转顺序（= 抽取顺序），文件里则按 ``instance_id``
    升序输出。两者都重要：前者证明"相邻任务被分散"，后者让 diff 稳定。
    """

    dev30: tuple[str, ...]
    test50: tuple[str, ...]
    official_dev23: tuple[str, ...]
    selection_order: tuple[str, ...]
    repo_distribution: Mapping[str, Mapping[str, int]]

    def by_name(self) -> dict[str, tuple[str, ...]]:
        return {
            DEV_FILE: self.dev30,
            TEST_FILE: self.test50,
            OFFICIAL_DEV_FILE: self.official_dev23,
        }


def _repo_distribution(
    rows: Sequence[Mapping[str, Any]], instance_ids: Iterable[str]
) -> dict[str, int]:
    known = {str(row["instance_id"]): str(row["repo"]) for row in rows}
    counts: Counter[str] = Counter()
    for instance_id in instance_ids:
        counts[known[instance_id]] += 1
    return dict(sorted(counts.items()))


def build_plan(
    test_rows: Sequence[Mapping[str, Any]],
    dev_rows: Sequence[Mapping[str, Any]],
    *,
    seed: int = DEFAULT_SEED,
    dev_count: int = DEV_COUNT,
    test_count: int = TEST_COUNT,
) -> SubsetPlan:
    """按固定算法切出 dev/test 子集；不读任何模型运行结果。"""
    if dev_count <= 0 or test_count <= 0:
        raise SubsetError("dev_count 与 test_count 必须为正")
    rotation = build_rotation(test_rows, seed)
    needed = dev_count + test_count
    if needed > len(rotation):
        raise SubsetError(f"样本不足：需要 {needed} 条，官方 test split 只有 {len(rotation)} 条")
    dev30 = tuple(sorted(rotation[:dev_count]))
    test50 = tuple(sorted(rotation[dev_count:needed]))
    overlap = set(dev30) & set(test50)
    if overlap:  # 轮转切分天然不重叠；保留断言防止将来改成独立抽样时静默回归
        raise SubsetError(f"dev/test 子集出现重叠: {sorted(overlap)[:5]}")
    official_dev23 = tuple(sorted(str(row["instance_id"]) for row in dev_rows))
    return SubsetPlan(
        dev30=dev30,
        test50=test50,
        official_dev23=official_dev23,
        selection_order=tuple(rotation[:needed]),
        repo_distribution={
            DEV_FILE: _repo_distribution(test_rows, dev30),
            TEST_FILE: _repo_distribution(test_rows, test50),
            OFFICIAL_DEV_FILE: _repo_distribution(dev_rows, official_dev23),
        },
    )


# --------------------------------------------------------------------------- 写文件


def _portable_path(path: Path) -> str:
    """manifest 里记录相对仓库根的路径（无法相对化时保留 POSIX 绝对路径）。

    绝对路径写进版本库会让 manifest 在另一台机器上指向不存在的快照目录，看起来像
    "数据集丢了"，其实只是路径不是自己的。相对路径 + revision 才能复现。
    """
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode(
        "utf-8"
    )


def _subset_payload(
    *,
    name: str,
    description: str,
    source_split: str,
    instance_ids: Sequence[str],
    repo_distribution: Mapping[str, int],
    seed: int | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "description": description,
        "dataset": {"name": DATASET_NAME, "revision": DATASET_REVISION},
        "source_split": source_split,
        "seed": seed,
        "count": len(instance_ids),
        "instance_ids": list(instance_ids),
        "instance_list_sha256": instance_list_sha256(instance_ids),
        "repo_distribution": dict(repo_distribution),
    }


def _repo_distribution_desc(distribution: Mapping[str, int]) -> str:
    return "、".join(f"{repo}={count}" for repo, count in distribution.items())


def write_subsets(
    out_dir: Path,
    snapshot_root: Path,
    *,
    seed: int = DEFAULT_SEED,
    dev_count: int = DEV_COUNT,
    test_count: int = TEST_COUNT,
    generated_at: str | None = None,
) -> Path:
    """生成并原子写出三个子集文件与 manifest，返回 manifest 路径。

    ``generated_at`` 可显式传入固定值：可重复性测试需要逐字节比对 manifest，
    而真实"生成时间"每次都会变。子集文件本身不含时间戳，因此始终逐字节稳定。
    """
    snapshot_root = Path(snapshot_root)
    out_dir = Path(out_dir)
    snapshot_manifest = load_manifest(snapshot_root)
    test_rows = load_split(snapshot_root, "test")
    dev_rows = load_split(snapshot_root, "dev")
    plan = build_plan(test_rows, dev_rows, seed=seed, dev_count=dev_count, test_count=test_count)

    timestamp = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SubsetError(f"generated_at 必须是 UTC ISO8601（...Z）: {timestamp!r}") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        DEV_FILE: _subset_payload(
            name="swebench-lite-dev30",
            description=(
                "自定义开发子集：从官方 test split 固定 seed 分层轮转抽出 30 题；"
                "与 test50 不重叠。不是官方 dev split。"
            ),
            source_split="test",
            instance_ids=plan.dev30,
            repo_distribution=plan.repo_distribution[DEV_FILE],
            seed=seed,
        ),
        TEST_FILE: _subset_payload(
            name="swebench-lite-test50",
            description=(
                "自定义测试子集：从官方 test split 固定 seed 分层轮转抽出 50 题；"
                "与 dev30 不重叠。官方榜单口径仍是完整 300 题。"
            ),
            source_split="test",
            instance_ids=plan.test50,
            repo_distribution=plan.repo_distribution[TEST_FILE],
            seed=seed,
        ),
        OFFICIAL_DEV_FILE: _subset_payload(
            name="swebench-lite-official-dev23",
            description=(
                "官方 dev split 全量 23 题的参考清单，仅作兼容/对照；"
                "不参与自定义抽样，也不与 dev30/test50 混用。"
            ),
            source_split="dev",
            instance_ids=plan.official_dev23,
            repo_distribution=plan.repo_distribution[OFFICIAL_DEV_FILE],
            seed=None,
        ),
    }

    file_records: dict[str, dict[str, Any]] = {}
    for filename, payload in payloads.items():
        data = _json_bytes(payload)
        target = out_dir / filename
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        file_records[filename] = {
            "count": payload["count"],
            "sha256": _sha256_bytes(data),
            "instance_list_sha256": payload["instance_list_sha256"],
            "source_split": payload["source_split"],
            "repo_distribution": payload["repo_distribution"],
        }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp,
        "dataset": {
            "name": DATASET_NAME,
            "revision": DATASET_REVISION,
            "official_split_sizes": dict(OFFICIAL_SPLIT_SIZES),
            "snapshot_root": _portable_path(snapshot_root),
            "snapshot_jsonl_sha256": {
                split: str(
                    snapshot_manifest.get("splits", {}).get(split, {}).get("jsonl_sha256", "")
                )
                for split in ("dev", "test")
            },
        },
        "seed": seed,
        "source_split": "test",
        "algorithm": {
            "id": ALGORITHM_ID,
            "summary": (
                "仓库间轮转 + 仓库内 version 组轮转；实例顺序由 sha256 决定，"
                "全局轮转序列前 30 条为 dev30、随后 50 条为 test50"
            ),
            "steps": [
                "读取官方 test split 300 条，instance_id 升序作为稳定输入",
                "按 repo 分组；repo 轮转顺序 = 实例数降序、repo 名升序",
                "每个 repo 内按 version 分组，version 组顺序与组内实例顺序均由 sha256(seed|x) 决定",
                "同一 repo 内先在 version 组之间轮转，避免同 version 连续编号任务成串",
                "全局按 repo 轮转取实例，前 30 条为 dev30，随后 50 条为 test50",
                "两组各自按 instance_id 升序写盘，并断言交集为空",
            ],
            "determinism": "仅依赖快照内容与固定 seed；不使用 random 模块与系统时间（时间只写 manifest）",
            "model_results_used": False,
        },
        "files": file_records,
        "overlap": {
            f"{DEV_FILE}∩{TEST_FILE}": {
                "count": len(set(plan.dev30) & set(plan.test50)),
                "instance_ids": sorted(set(plan.dev30) & set(plan.test50)),
            }
        },
        "official_split_vs_custom": {
            "official_test": "官方 test split 300 条，是完整榜单/全量实验口径",
            "official_dev": "官方 dev split 23 条，仅保留为参考清单，不参与任何抽样",
            "custom_dev30": "从官方 test split 抽 30 条作为开发子集（调参/冒烟），不是官方 dev",
            "custom_test50": "从官方 test split 另抽 50 条作为测试子集（E-18 固定口径）",
            "difference": (
                "自定义 dev30/test50 是官方 test 300 条的固定子集：口径更小、可重复、"
                "仓库均衡，但不等于官方 split，任何指标必须标明实际分母与抽取清单，"
                "不能写成官方的 dev/test 结果"
            ),
            "not_selected_by_model_success": (
                "抽样只依赖 instance_id/repo/version 与固定 seed；未使用任何模型运行结果，"
                "因此不存在按 GLM 成功率挑题"
            ),
        },
        "reproduce": (f"python scripts/make_swebench_subsets.py --generated-at {timestamp}"),
    }
    manifest_target = out_dir / MANIFEST_FILE
    manifest_temporary = manifest_target.with_name(manifest_target.name + ".tmp")
    manifest_temporary.write_bytes(_json_bytes(manifest))
    manifest_temporary.replace(manifest_target)
    return manifest_target


# --------------------------------------------------------------------------- 校验/CLI


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SubsetError(f"无法读取 {path}") from exc
    if not isinstance(payload, dict):
        raise SubsetError(f"{path} 顶层必须是对象")
    return payload


def check_committed(out_dir: Path, snapshot_root: Path) -> list[str]:
    """校验已提交文件与快照重算结果一致；返回问题列表（空 = 通过）。"""
    out_dir = Path(out_dir)
    problems: list[str] = []
    manifest_path = out_dir / MANIFEST_FILE
    manifest = _load_json(manifest_path)
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str):
        return [f"{manifest_path} 缺少 generated_at"]
    # 确认快照身份（revision/行数）与生成时一致，否则"重算一致"没有意义。
    snapshot_manifest = load_manifest(snapshot_root)
    recorded_revision = str(manifest.get("dataset", {}).get("revision", ""))
    if recorded_revision != str(snapshot_manifest.get("revision", "")):
        # 数据集升级后旧清单不能静默沿用：必须显式迁移并重新生成。
        problems.append(
            f"manifest 记录的 revision={recorded_revision!r} 与快照 "
            f"revision={snapshot_manifest.get('revision')!r} 不一致；"
            "需要显式迁移并重新生成子集"
        )
    if recorded_revision != DATASET_REVISION:
        problems.append(
            f"manifest revision={recorded_revision!r} 不是当前固定的 {DATASET_REVISION!r}"
        )
    plan = build_plan(
        load_split(snapshot_root, "test"),
        load_split(snapshot_root, "dev"),
        seed=int(manifest.get("seed", DEFAULT_SEED)),
    )
    expected = {
        DEV_FILE: plan.dev30,
        TEST_FILE: plan.test50,
        OFFICIAL_DEV_FILE: plan.official_dev23,
    }
    for filename, expected_ids in expected.items():
        path = out_dir / filename
        try:
            payload = _load_json(path)
        except SubsetError as exc:
            problems.append(str(exc))
            continue
        recorded = tuple(str(item) for item in payload.get("instance_ids", []))
        if recorded != expected_ids:
            problems.append(f"{filename} 的 instance_ids 与重算结果不一致")
        if payload.get("instance_list_sha256") != instance_list_sha256(expected_ids):
            problems.append(f"{filename} 的 instance_list_sha256 不一致")
        record = manifest.get("files", {}).get(filename, {})
        if record.get("sha256") != _sha256_bytes(_json_bytes(payload)):
            problems.append(f"{filename} 的 sha256 与 manifest 记录不一致")
    overlap = set(plan.dev30) & set(plan.test50)
    if overlap:
        problems.append(f"dev30/test50 重叠: {sorted(overlap)[:5]}")
    return problems


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成固定 SWE-bench Lite 30/50 子集")
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=DEFAULT_SNAPSHOT_ROOT,
        help="数据集快照目录（manifest.json + dev.jsonl + test.jsonl）。",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="子集配置输出目录。")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--dev-count", type=int, default=DEV_COUNT)
    parser.add_argument("--test-count", type=int, default=TEST_COUNT)
    parser.add_argument(
        "--generated-at",
        default=None,
        metavar="YYYY-MM-DDTHH:MM:SSZ",
        help="manifest 生成时间；可重复性测试用固定值。缺省为当前 UTC 时间。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验已提交文件是否与快照重算结果一致，不写盘。",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.check:
        problems = check_committed(args.out_dir, args.snapshot_root)
        if problems:
            for problem in problems:
                print(f"[SUBSET] {problem}", file=sys.stderr)
            return 1
        print(f"[SUBSET] 已提交子集与快照重算一致: {args.out_dir}")
        return 0
    manifest_path = write_subsets(
        args.out_dir,
        args.snapshot_root,
        seed=args.seed,
        dev_count=args.dev_count,
        test_count=args.test_count,
        generated_at=args.generated_at,
    )
    manifest = _load_json(manifest_path)
    for filename, record in manifest["files"].items():
        print(
            f"[SUBSET] {filename}: {record['count']} 题 "
            f"sha256={record['sha256'][:12]} "
            f"repos={_repo_distribution_desc(record['repo_distribution'])}"
        )
    print(f"[SUBSET] manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
