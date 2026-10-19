"""下载固定 revision 的 SWE-bench Lite parquet 并生成离线快照（E-11 基础设施）。

为什么需要这个脚本：

* 主 venv **故意不安装** ``datasets`` / ``pyarrow``（生产 Loader 完全离线、不 import
  它们），所以"下载 parquet"必须是独立入口，且缺失依赖时给出明确错误；
* 快照必须可复现：固定 revision、固定 parquet sha256、固定 split 行数，manifest 里
  记录 JSONL 的 sha256/字节数，让 Loader 能证明"读的就是这一版"；
* ``--verify-only`` 不联网、不 import pyarrow，只做 manifest/JSONL/字段校验，供 CI
  与离线环境确认缓存没有损坏。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - 仅在脚本运行时执行
    sys.path.insert(0, str(REPO_ROOT / "src"))

from codepilot.eval.swebench import (  # noqa: E402
    SWEBenchDatasetError,
    SWEBenchLoader,
    manifest_content_sha256,
)
from codepilot.eval.swebench_task import (  # noqa: E402
    DATASET_NAME,
    DATASET_REVISION,
    OFFICIAL_SPLIT_SIZES,
)

__all__ = [
    "PARQUET_SHA256",
    "SPLIT_FILES",
    "FetchError",
    "build_snapshot_files",
    "encode_jsonl",
    "fetch",
    "main",
    "normalize_instance",
    "verify_snapshot",
]

#: 固定 revision 的官方 parquet 文件名与 sha256（契约 §0 的固定事实）。
SPLIT_FILES: dict[str, str] = {
    "dev": "dev-00000-of-00001.parquet",
    "test": "test-00000-of-00001.parquet",
}
PARQUET_SHA256: dict[str, str] = {
    "dev": "8312f321838051849d2fa7c6ca071244733a3e90bb517ff6ca8186f722199b5c",
    "test": "7a21f37b8bc179c7db5beeb14e88ac538ba283455c776e6b2535bbfb6e3551b4",
}
_SPLITS: tuple[str, ...] = ("dev", "test")
_MIN_ROW_FIELDS: tuple[str, ...] = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "version",
    "environment_setup_commit",
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
)
_ROW_STRING_FIELDS: tuple[str, ...] = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "version",
    "environment_setup_commit",
    "patch",
    "test_patch",
)


class FetchError(RuntimeError):
    """快照生成失败（缺依赖、下载失败、数据形态不符）。"""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)  # noqa: PTH105 - 契约要求的就是 os.replace 语义
    finally:
        tmp_path.unlink(missing_ok=True)


def normalize_instance(row: Mapping[str, Any]) -> dict[str, Any]:
    """把 parquet 的一行规范化为快照 JSONL 行。

    官方 parquet 里 ``FAIL_TO_PASS`` / ``PASS_TO_PASS`` 是 **JSON 字符串**；
    快照统一转成字符串数组，Loaders 因此不需要在读取路径上再猜类型。
    """
    missing = [field for field in _MIN_ROW_FIELDS if field not in row]
    if missing:
        raise FetchError(f"parquet 行缺字段: {missing}")
    normalized: dict[str, Any] = dict(row)
    for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        value = normalized[field]
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise FetchError(f"{field} 不是合法 JSON 字符串: {value[:80]!r}") from exc
            value = decoded
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise FetchError(f"{field} 必须是字符串数组")
        normalized[field] = value
    for field in _ROW_STRING_FIELDS:
        if not isinstance(normalized[field], str):
            raise FetchError(f"{field} 必须是字符串")
    return normalized


def encode_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """稳定编码：key 排序、UTF-8、每行一个 JSON、结尾换行。"""
    lines = [json.dumps(dict(row), ensure_ascii=False, sort_keys=True) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _default_parquet_meta(split: str, *, dataset_name: str, revision: str) -> dict[str, Any]:
    filename = SPLIT_FILES.get(split, f"{split}.parquet")
    return {
        "parquet_filename": filename,
        "parquet_sha256": _sha256(f"fixture:{dataset_name}@{revision}:{split}".encode()),
        "parquet_bytes": 1,
        "source_url": (
            f"https://huggingface.co/datasets/{dataset_name}/resolve/{revision}/data/{filename}"
        ),
    }


def build_snapshot_files(
    rows_by_split: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    dataset_name: str = DATASET_NAME,
    revision: str = DATASET_REVISION,
    parquet_meta: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, bytes]:
    """生成 ``{split}.jsonl`` 与 ``manifest.json`` 的字节内容。

    与 ``cache/verify/gen_assets.py`` 的生成顺序保持一致：先算 content_sha256，
    再补 schema_version——所以 Loader 计算摘要时排除这两个 key。
    """
    if set(rows_by_split) != set(_SPLITS):
        raise FetchError(f"rows_by_split 必须恰好包含 {_SPLITS}")
    files: dict[str, bytes] = {}
    manifest: dict[str, Any] = {
        "dataset_name": dataset_name,
        "revision": revision,
        "source": f"https://huggingface.co/datasets/{dataset_name}",
        "resolved_at": "2026-10-19",
        "official_split_sizes": {},
        "splits": {},
    }
    for split in _SPLITS:
        rows = [normalize_instance(row) for row in rows_by_split[split]]
        if not rows:
            raise FetchError(f"{split} split 为空")
        files[f"{split}.jsonl"] = encode_jsonl(rows)
        meta = (
            dict(parquet_meta[split])
            if parquet_meta and split in parquet_meta
            else (_default_parquet_meta(split, dataset_name=dataset_name, revision=revision))
        )
        manifest["splits"][split] = {
            "rows": len(rows),
            "parquet_filename": str(meta["parquet_filename"]),
            "parquet_sha256": str(meta["parquet_sha256"]),
            "parquet_bytes": int(meta["parquet_bytes"]),
            "jsonl_sha256": _sha256(files[f"{split}.jsonl"]),
            "jsonl_bytes": len(files[f"{split}.jsonl"]),
            "source_url": str(meta["source_url"]),
        }
        manifest["official_split_sizes"][split] = len(rows)
    if (dataset_name, revision) == (DATASET_NAME, DATASET_REVISION):
        for split in _SPLITS:
            if manifest["official_split_sizes"][split] != OFFICIAL_SPLIT_SIZES[split]:
                raise FetchError(
                    f"官方 {split} split 行数 {manifest['official_split_sizes'][split]} != "
                    f"固定事实 {OFFICIAL_SPLIT_SIZES[split]}"
                )
    manifest["content_sha256"] = manifest_content_sha256(manifest)
    manifest["schema_version"] = 1
    files["manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return files


def _import_pyarrow() -> Any:
    """延迟 import：主 venv 没有 pyarrow 时给出可执行的错误提示。"""
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise FetchError(
            "缺少 pyarrow：生成 SWE-bench 快照需要 pyarrow；请在已安装可选依赖的环境"
            "（例如 .venv-swebench）运行，或 `pip install pyarrow`。主 venv 故意不安装"
            "datasets/pyarrow，生产 Loader 只读离线快照。"
        ) from exc
    return parquet


def _download(url: str, destination: Path, *, timeout: float = 120.0) -> bytes:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read()
    except OSError as exc:
        raise FetchError(f"下载失败: {url}: {exc}") from exc
    _atomic_write(destination, payload)
    return payload


def _source_url(split: str, *, dataset_name: str, revision: str) -> str:
    filename = SPLIT_FILES[split]
    return f"https://huggingface.co/datasets/{dataset_name}/resolve/{revision}/data/{filename}"


def _snapshot_root_loader(snapshot_root: Path, revision: str, dataset_name: str) -> SWEBenchLoader:
    return SWEBenchLoader(snapshot_root, dataset_name=dataset_name, revision=revision)


def verify_snapshot(
    snapshot_root: Path,
    *,
    revision: str = DATASET_REVISION,
    dataset_name: str = DATASET_NAME,
) -> dict[str, Any]:
    """离线校验快照；不 import pyarrow、不联网。失败时抛异常。"""
    root = Path(snapshot_root)
    loader = _snapshot_root_loader(root, revision, dataset_name)
    validation = loader.validate_dataset()
    manifest = loader.get_dataset_manifest()
    parquet_checks: dict[str, Any] = {}
    for split in _SPLITS:
        entry = manifest.splits[split]
        expected = PARQUET_SHA256.get(split)
        if (
            (dataset_name, revision) == (DATASET_NAME, DATASET_REVISION)
            and expected is not None
            and entry.parquet_sha256 != expected
        ):
            raise FetchError(
                f"manifest 记录的 {split} parquet sha256 与固定事实不符: "
                f"{entry.parquet_sha256} != {expected}"
            )
        local = root / entry.parquet_filename
        if local.is_file():
            payload = local.read_bytes()
            actual = _sha256(payload)
            if actual != entry.parquet_sha256:
                raise FetchError(f"本地 {local.name} sha256 与 manifest 不符")
            parquet_checks[split] = {"present": True, "sha256": actual, "bytes": len(payload)}
        else:
            parquet_checks[split] = {"present": False}
    return {
        "dataset_name": validation.dataset_name,
        "revision": validation.revision,
        "split_rows": validation.split_rows,
        "cache_identity": validation.cache_identity,
        "instances_sha256": validation.instances_sha256,
        "parquet": parquet_checks,
    }


def fetch(
    snapshot_root: Path,
    *,
    revision: str = DATASET_REVISION,
    force: bool = False,
) -> Path:
    """下载并生成快照；已存在且校验通过时幂等跳过。"""
    if revision != DATASET_REVISION:
        raise FetchError(
            f"只允许固定 revision={DATASET_REVISION}；收到 {revision!r}。"
            "换 revision 必须同时更新契约里的 parquet 哈希/行数与所有下游身份。"
        )
    root = Path(snapshot_root)
    snapshot_dir = root / f"{DATASET_NAME.replace('/', '__')}__{revision}"
    if not force:
        try:
            verify_snapshot(root, revision=revision)
        except (SWEBenchDatasetError, FetchError):
            pass
        else:
            print(f"[fetch] 快照已存在且校验通过，跳过下载: {snapshot_dir}")
            return snapshot_dir
    parquet = _import_pyarrow()
    rows_by_split: dict[str, list[dict[str, Any]]] = {}
    parquet_meta: dict[str, dict[str, Any]] = {}
    for split in _SPLITS:
        filename = SPLIT_FILES[split]
        local = root / filename
        expected = PARQUET_SHA256[split]
        if not local.is_file() or _sha256(local.read_bytes()) != expected:
            payload = _download(
                _source_url(split, dataset_name=DATASET_NAME, revision=revision), local
            )
            if _sha256(payload) != expected:
                raise FetchError(
                    f"{filename} sha256 不符：期望 {expected}，实际 {_sha256(payload)}。"
                    "固定 revision 的内容不应变化，拒绝生成身份错误的快照。"
                )
        payload = local.read_bytes()
        table = parquet.read_table(local)
        rows = [dict(row) for row in table.to_pylist()]
        if len(rows) != OFFICIAL_SPLIT_SIZES[split]:
            raise FetchError(f"{split} 行数 {len(rows)} != 固定事实 {OFFICIAL_SPLIT_SIZES[split]}")
        rows_by_split[split] = rows
        parquet_meta[split] = {
            "parquet_filename": filename,
            "parquet_sha256": _sha256(payload),
            "parquet_bytes": len(payload),
            "source_url": _source_url(split, dataset_name=DATASET_NAME, revision=revision),
        }
    files = build_snapshot_files(rows_by_split, parquet_meta=parquet_meta)
    for name, content in files.items():
        _atomic_write(snapshot_dir / name, content)
    verify_snapshot(root, revision=revision)
    print(f"[fetch] 快照生成并通过离线校验: {snapshot_dir}")
    return snapshot_dir


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="下载/校验固定 revision 的 SWE-bench Lite 离线快照"
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=REPO_ROOT / "cache" / "swebench",
        help="快照根目录（默认 cache/swebench）",
    )
    parser.add_argument(
        "--revision",
        default=DATASET_REVISION,
        help=f"固定数据集 revision（只允许 {DATASET_REVISION}）",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="不联网/不 import pyarrow，只校验已有快照",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使已有快照校验通过也重新下载生成",
    )
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            report = verify_snapshot(args.snapshot_root, revision=args.revision)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            print("[verify] 快照校验通过")
            return 0
        fetch(args.snapshot_root, revision=args.revision, force=args.force)
        return 0
    except FetchError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except (SWEBenchDatasetError, OSError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
