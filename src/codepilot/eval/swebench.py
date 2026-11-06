"""SWE-bench Lite 离线快照 Loader 与**评测侧**任务对象（E-11 / E-12）。

设计要点（为什么长这样）：

1. **完全离线、fail closed**。生产路径只读 ``cache/swebench/<dataset>__<revision>/``
   下的 ``manifest.json`` 与 ``dev.jsonl`` / ``test.jsonl``。manifest 里记录了每个
   split 的 JSONL sha256/字节数，Loader 逐项核对；对不上就是
   :class:`SWEBenchCacheError`，**不会**悄悄重新下载或换 revision——否则一次网络
   抖动就能让"同一批预测"实际跑在不同数据上。
2. **推理/评测在加载时就分家**。本模块同时持有两套对象：
   :class:`SWEBenchInferenceTask`（来自 :mod:`codepilot.eval.swebench_task`，只有
   可见字段）与 :class:`SWEBenchEvaluationTask`（gold/test）。评测对象只在这里
   存在，推理代码拿不到；两边字段集合除身份字段外严格不相交。
3. **环境规格是保守投影**。任务对象需要一个
   :class:`~codepilot.sandbox.env_spec.EnvSpec` 才能进装配层；官方 SWE-bench 环境
   身份由 Owner C 的 ``SWEBenchEnvironmentSpec`` 从同一份冻结快照读取。这里从
   ``configs/swebench/env-specs-<harness>.json`` 做一个**通用 DockerSandbox 可用**的
   投影：不支持的官方 Python 版本（如 3.6/3.8/3.9）夹到 3.10，conda 规格退化成
   pip 规格。这个投影只用于"任务能离线构造"，不能冒充官方环境身份。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from codepilot.eval.swebench_prediction import (
    PredictionEntry,
    PredictionError,
    PredictionManifest,
    PredictionRecord,
    PredictionStatus,
    PredictionWriter,
)
from codepilot.eval.swebench_task import (
    DATASET_NAME,
    DATASET_REVISION,
    HARNESS_VERSION,
    OFFICIAL_SPLIT_SIZES,
    SWEBenchDatasetRef,
    SWEBenchInferenceTask,
    validate_base_commit,
    validate_dataset_name,
    validate_instance_id,
    validate_repo_slug,
    validate_revision,
    validate_swebench_version,
)
from codepilot.sandbox.env_spec import (
    DEFAULT_TEST_COMMAND,
    EnvSpec,
    EnvSpecError,
    from_swebench_spec,
)

__all__ = [
    "DatasetManifest",
    "DatasetValidation",
    "PredictionEntry",
    "PredictionError",
    "PredictionManifest",
    "PredictionRecord",
    "PredictionStatus",
    "PredictionWriter",
    "SWEBenchCacheError",
    "SWEBenchDatasetError",
    "SWEBenchDatasetManifest",
    "SWEBenchDatasetValidation",
    "SWEBenchEvaluationTask",
    "SWEBenchLoader",
    "SWEBenchSplitManifest",
    "manifest_content_sha256",
]

#: 官方 split 固定为 dev/test；loader 不接受别的 split 名，避免"自定义划分"混进
#: 官方口径。
_SPLITS: tuple[Literal["dev", "test"], ...] = ("dev", "test")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_PARQUET_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.parquet$")

#: 数据集快照里每条 instance 必须带的字段。``hints_text`` / ``created_at`` 刻意
#: 不在必填集合里：它们在推理侧不可见、评测侧也不用，loader 直接丢弃。
_REQUIRED_ROW_FIELDS: tuple[str, ...] = (
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
_REQUIRED_STR_FIELDS: tuple[str, ...] = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "version",
    "environment_setup_commit",
    "patch",
    "test_patch",
)

#: EnvSpec 的基础镜像只覆盖这些 Python 版本（sandbox/docker.py 的 ``_ensure_base_image``）。
_SUPPORTED_PYTHON = frozenset({"3.10", "3.11", "3.12", "3.13"})
#: 官方版本低于 3.10 时夹到 3.10。原因见模块 docstring 第 3 点：官方镜像路径是
#: Owner C 的事，这里的投影只保证通用 DockerSandbox 能构造；不同版本的真实身份
#: 由 ``spec_sha256`` 与官方镜像 key 记录，不会被这个投影覆盖。
_FALLBACK_PYTHON = "3.10"


class SWEBenchDatasetError(ValueError):
    """数据集内容不符合固定 schema——必须失败，不允许跳过某条继续跑。"""


class SWEBenchCacheError(SWEBenchDatasetError):
    """本地快照缺失/损坏（hash 不符、字节数不符）。

    单独成类是为了让调用方能区分"数据本身有问题"（应修数据）与"缓存没准备好"
    （应跑 fetch 脚本）。两者都不允许静默降级。
    """


def manifest_content_sha256(payload: Mapping[str, Any]) -> str:
    """manifest 内容摘要。

    ``content_sha256`` 是摘要本身；``schema_version`` 是生成后才补上的元数据
    （见 ``scripts/fetch_swebench_lite.py`` 与 ``cache/verify/gen_assets.py``），
    因此两者都不参与摘要计算。规则必须与生成器完全一致，否则快照一生成就"自证
    损坏"。
    """
    content = {
        key: value
        for key, value in payload.items()
        if key not in {"content_sha256", "schema_version"}
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _instances_sha256(instance_ids: Sequence[str]) -> str:
    canonical = json.dumps(sorted(instance_ids), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SWEBenchSplitManifest(BaseModel):
    """单个 split 的字节级身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: int = Field(ge=1)
    parquet_filename: str
    parquet_sha256: str
    parquet_bytes: int = Field(ge=1)
    jsonl_sha256: str
    jsonl_bytes: int = Field(ge=1)
    source_url: str

    @field_validator("rows", "parquet_bytes", "jsonl_bytes", mode="before")
    @classmethod
    def _positive_int(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("manifest 计数必须是整数")
        if value < 1:
            raise ValueError("manifest 计数必须为正数")
        return value

    @field_validator("parquet_filename")
    @classmethod
    def _parquet_name(cls, value: str) -> str:
        if _SAFE_PARQUET_NAME.fullmatch(value) is None or "/" in value or "\\" in value:
            raise ValueError(f"parquet_filename 必须是纯文件名: {value!r}")
        return value

    @field_validator("parquet_sha256", "jsonl_sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError(f"sha256 格式非法: {value!r}")
        return value

    @field_validator("source_url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_url 不能为空")
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("source_url 不能含控制字符")
        return value


class DatasetManifest(BaseModel):
    """``manifest.json`` 的强类型视图（schema_version=1）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: str
    revision: str
    source: str
    official_split_sizes: dict[str, int]
    splits: dict[str, SWEBenchSplitManifest]
    content_sha256: str
    schema_version: int = 1
    resolved_at: str = ""

    @field_validator("dataset_name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_revision(value)

    @field_validator("content_sha256")
    @classmethod
    def _content_hash(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("content_sha256 必须是 sha256")
        return value

    @field_validator("official_split_sizes", mode="before")
    @classmethod
    def _sizes(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("official_split_sizes 必须是对象")
        normalized: dict[str, int] = {}
        for key, item in value.items():
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError("official_split_sizes 的值必须是整数（拒绝 bool/字符串）")
            normalized[str(key)] = item
        return normalized

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("schema_version 必须是整数")
        if value != 1:
            raise ValueError(f"不支持的快照 schema_version: {value}")
        return value

    @model_validator(mode="after")
    def _structure(self) -> DatasetManifest:
        if set(self.official_split_sizes) != set(_SPLITS):
            raise ValueError("official_split_sizes 必须恰好包含 dev/test")
        if set(self.splits) != set(_SPLITS):
            raise ValueError("splits 必须恰好包含 dev/test")
        for split in _SPLITS:
            declared = self.official_split_sizes[split]
            if isinstance(declared, bool) or not isinstance(declared, int) or declared < 1:
                raise ValueError(f"official_split_sizes[{split!r}] 必须是正整数")
            if declared != self.splits[split].rows:
                raise ValueError(
                    f"official_split_sizes[{split!r}]={declared} 与 "
                    f"splits[{split!r}].rows={self.splits[split].rows} 不一致"
                )
        if not self.source.strip():
            raise ValueError("source 不能为空")
        return self


class DatasetValidation(BaseModel):
    """``validate_dataset()`` 的成功回执（失败会抛异常，不返回半成品）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: str
    revision: str
    split_rows: dict[str, int]
    jsonl_sha256: dict[str, str]
    instances_sha256: dict[str, str]
    content_sha256: str
    cache_identity: str
    official_sizes_match: bool


def _validate_test_node(value: str) -> str:
    """SWE-bench 测试节点 id。

    格式并不统一：新数据是 ``path/to/test.py::test_name``，老 django 数据是
    ``test_name (module.Class)``。因此只拒绝空值/控制字符，不强行套 pytest 语法。
    """
    if not isinstance(value, str):
        raise ValueError("测试节点 id 必须是字符串")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"测试节点 id 含控制字符: {value!r}")
    if not value.strip():
        raise ValueError("测试节点 id 不能为空")
    return value


class SWEBenchEvaluationTask(BaseModel):
    """评测侧对象：gold patch、test patch 与目标测试清单。

    这个对象**只在** Loader/评测器路径里出现。与推理对象的字段交集严格限定在
    :data:`~codepilot.eval.swebench_task.INFERENCE_IDENTITY_FIELDS`；其余字段
    （patch/test 清单/哈希）推理侧没有对应字段可承载。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    repo: str
    base_commit: str
    version: str
    environment_setup_commit: str
    dataset: SWEBenchDatasetRef
    gold_patch: str
    test_patch: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    gold_patch_sha256: str
    test_patch_sha256: str

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, value: str) -> str:
        return validate_instance_id(value)

    @field_validator("repo")
    @classmethod
    def _repo(cls, value: str) -> str:
        return validate_repo_slug(value)

    @field_validator("base_commit")
    @classmethod
    def _base(cls, value: str) -> str:
        return validate_base_commit(value)

    @field_validator("environment_setup_commit")
    @classmethod
    def _setup_commit(cls, value: str) -> str:
        return validate_base_commit(value)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return validate_swebench_version(value)

    @field_validator("gold_patch", "test_patch")
    @classmethod
    def _patch_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("patch 不能含 NUL")
        if not value.strip():
            raise ValueError("gold_patch / test_patch 不能为空")
        return value

    @field_validator("fail_to_pass", "pass_to_pass", mode="before")
    @classmethod
    def _sequence(cls, value: object) -> object:
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ValueError("FAIL_TO_PASS / PASS_TO_PASS 必须是字符串数组")
        return tuple(value)

    @field_validator("fail_to_pass", "pass_to_pass")
    @classmethod
    def _nodes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_test_node(item) for item in value)

    @field_validator("gold_patch_sha256", "test_patch_sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError(f"patch sha256 格式非法: {value!r}")
        return value

    @model_validator(mode="after")
    def _check(self) -> SWEBenchEvaluationTask:
        if not self.fail_to_pass:
            raise ValueError("FAIL_TO_PASS 不能为空：没有目标测试的实例无法判定")
        for name, values in (
            ("fail_to_pass", self.fail_to_pass),
            ("pass_to_pass", self.pass_to_pass),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{name} 含重复项")
        overlap = set(self.fail_to_pass) & set(self.pass_to_pass)
        if overlap:
            raise ValueError(f"同一测试不能既要求失败又要求通过: {sorted(overlap)[:3]}")
        if not self.instance_id.startswith(f"{self.repo.replace('/', '__')}-"):
            raise ValueError(f"instance_id={self.instance_id!r} 与 repo={self.repo!r} 不一致")
        if self.gold_patch_sha256 != hashlib.sha256(self.gold_patch.encode("utf-8")).hexdigest():
            raise ValueError("gold_patch_sha256 与 gold_patch 内容不符")
        if self.test_patch_sha256 != hashlib.sha256(self.test_patch.encode("utf-8")).hexdigest():
            raise ValueError("test_patch_sha256 与 test_patch 内容不符")
        return self


class _OfficialSpecs:
    """冻结环境规格快照的惰性只读视图。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._specs: dict[str, dict[str, dict[str, Any]]] | None = None

    def load(self) -> dict[str, dict[str, dict[str, Any]]]:
        if self._specs is not None:
            return self._specs
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SWEBenchDatasetError(f"无法读取环境规格快照: {self.path}") from exc
        except JSONDecodeError as exc:
            raise SWEBenchDatasetError(f"环境规格快照不是合法 JSON: {self.path}") from exc
        if not isinstance(payload, dict):
            raise SWEBenchDatasetError("环境规格快照顶层必须是对象")
        harness = payload.get("harness_version")
        if harness != HARNESS_VERSION:
            # 环境规格与 harness 版本强绑定：4.1.0 的 REPO/version 规格不能拿来跑
            # 别的 harness 版本，否则 env image/key 会对不上。
            raise SWEBenchDatasetError(
                f"环境规格快照 harness_version={harness!r}，期望 {HARNESS_VERSION}"
            )
        raw_specs = payload.get("specs")
        if not isinstance(raw_specs, dict):
            raise SWEBenchDatasetError("环境规格快照缺少 specs 对象")
        specs: dict[str, dict[str, dict[str, Any]]] = {}
        for repo, versions in raw_specs.items():
            if not isinstance(repo, str) or not isinstance(versions, dict):
                raise SWEBenchDatasetError("环境规格快照的 specs 结构无效")
            repo_specs: dict[str, dict[str, Any]] = {}
            for version, spec in versions.items():
                if not isinstance(version, str) or not isinstance(spec, dict):
                    raise SWEBenchDatasetError(f"环境规格快照 specs[{repo!r}] 结构无效")
                repo_specs[version] = dict(spec)
            specs[repo] = repo_specs
        self._specs = specs
        return specs


def _official_env_spec(raw: Mapping[str, Any], *, repo: str, version: str) -> EnvSpec:
    """把官方 repo/version 规格投影成通用 EnvSpec（见模块 docstring 第 3 点）。

    ``from_swebench_spec`` 已经是 packages / pip_packages / install / pre_install /
    test_cmd 的唯一解释器（Owner C，契约 §5）。这里**不再**自行追加 pip_packages：
    重复命令会进入 ``EnvSpec.canonical_json``，进而污染镜像 cache key 与 env 身份。
    只有它抛错（例如 conda 规格）时才走本文件的保守降级路径。
    """
    spec = dict(raw)
    python = str(spec.get("python", "") or "").strip()
    if not python:
        python = "3.12"
    if python not in _SUPPORTED_PYTHON:
        spec["python"] = _FALLBACK_PYTHON
    try:
        return from_swebench_spec(spec)
    except EnvSpecError:
        # conda (``environment.yml``) 规格无法映射到 Python slim 后端。官方镜像路径
        # 未实现时这是已知的 deferred 能力；这里退化成"pip + 测试命令"的保守规格，
        # 保证任务对象可构造，绝不声称等价于官方环境。
        return _conservative_env_spec(spec, where=f"{repo}@{version}")


def _conservative_env_spec(spec: Mapping[str, Any], *, where: str) -> EnvSpec:
    python = str(spec.get("python", "") or "3.12").strip()
    if python not in _SUPPORTED_PYTHON:
        python = _FALLBACK_PYTHON
    raw_test_command = spec.get("test_cmd", spec.get("test_command", DEFAULT_TEST_COMMAND))
    test_command = str(raw_test_command).strip() if raw_test_command is not None else ""
    if not test_command:
        test_command = DEFAULT_TEST_COMMAND
    if "\n" in test_command or "\r" in test_command:
        # EnvSpec 要求单行命令；官方规格当前都是一行，出现多行说明快照形态变了，
        # 失败比拼一个会被 shell 截断的命令更安全。
        raise SWEBenchDatasetError("官方 test_cmd 不是单行命令，无法投影为 EnvSpec")
    if "--junitxml" not in test_command:
        test_command = f"{test_command} --junitxml=/out/junit.xml"
    install: list[str] = []
    packages = spec.get("packages", "")
    if (
        isinstance(packages, str)
        and packages
        and not packages.endswith(("environment.yml", "environment.yaml"))
    ):
        if packages in {"requirements.txt", "requirements-dev.txt"}:
            install.append(f"python -m pip install -r {packages}")
        else:
            install.append(f"python -m pip install {packages}")
    # 官方顺序：packages → pip_packages → 仓库 install。降级路径没有别的解释器可用，
    # 因此这里自己按同一顺序补齐；正常路径绝不走到这里。
    pip_packages = spec.get("pip_packages", ())
    if pip_packages:
        if isinstance(pip_packages, str) or not isinstance(pip_packages, Sequence):
            raise SWEBenchDatasetError("官方 pip_packages 必须是字符串数组")
        pip_list = [str(item).strip() for item in pip_packages if str(item).strip()]
        if pip_list:
            install.append("python -m pip install " + " ".join(pip_list))
    raw_install = spec.get("install", "")
    if isinstance(raw_install, str) and raw_install.strip():
        install.append(raw_install.strip())
    elif isinstance(raw_install, Sequence) and not isinstance(raw_install, str):
        if not all(isinstance(item, str) for item in raw_install):
            raise SWEBenchDatasetError("官方 install 数组只能包含字符串")
        install.extend(item.strip() for item in raw_install if item.strip())
    try:
        return EnvSpec(
            python_version=python,
            install=tuple(install),
            test_command=test_command,
        )
    except ValueError as exc:
        raise SWEBenchDatasetError(f"无法为 {where} 投影 EnvSpec: {exc}") from exc


class SWEBenchLoader:
    """从固定 revision 的离线快照加载 SWE-bench Lite。

    ``env_spec_snapshot`` 默认指向仓库内冻结的 4.1.0 规格快照；测试可以显式传一个
    小快照。主 venv **没有** ``datasets`` / ``pyarrow``，因此这里绝不 import 它们。
    """

    def __init__(
        self,
        snapshot_root: Path,
        *,
        dataset_name: str = DATASET_NAME,
        revision: str = DATASET_REVISION,
        env_spec_snapshot: Path | None = None,
    ) -> None:
        self.snapshot_root = Path(snapshot_root)
        self.dataset_name = validate_dataset_name(dataset_name)
        self.revision = validate_revision(revision)
        self._manifest: DatasetManifest | None = None
        self._rows: dict[str, dict[str, dict[str, Any]]] = {}
        self._inference: dict[str, dict[str, SWEBenchInferenceTask]] = {}
        self._evaluation: dict[str, dict[str, SWEBenchEvaluationTask]] = {}
        self._jsonl_sha256: dict[str, str] = {}
        self._env_specs: _OfficialSpecs | None = None
        self._env_spec_cache: dict[tuple[str, str], EnvSpec] = {}

        if env_spec_snapshot is None:
            default = _default_env_spec_snapshot()
            self._env_specs = _OfficialSpecs(default) if default.is_file() else None
        else:
            path = Path(env_spec_snapshot)
            if not path.is_file():
                raise SWEBenchDatasetError(f"环境规格快照不存在: {path}")
            self._env_specs = _OfficialSpecs(path)

    # ------------------------------------------------------------------ 身份

    @property
    def snapshot_dirname(self) -> str:
        return f"{self.dataset_name.replace('/', '__')}__{self.revision}"

    @property
    def snapshot_dir(self) -> Path:
        """快照目录；缺失时直接给"跑 fetch 脚本"的明确错误，不回退别的 revision。"""
        directory = self.snapshot_root / self.snapshot_dirname
        if not directory.is_dir():
            raise SWEBenchCacheError(
                f"SWE-bench 数据快照不存在: {directory}。Loader 只读离线快照、不会自动"
                f"下载；请先运行 `python scripts/fetch_swebench_lite.py`（固定 revision="
                f"{self.revision}），或检查 snapshot_root。不得回退到其他 revision。"
            )
        return directory

    @property
    def manifest_path(self) -> Path:
        return self.snapshot_dir / "manifest.json"

    def _split_jsonl_path(self, split: str) -> Path:
        return self.snapshot_dir / f"{split}.jsonl"

    # ------------------------------------------------------------------ manifest

    def get_dataset_manifest(self) -> DatasetManifest:
        """读取并校验 manifest（身份 + content_sha256），结果缓存。"""
        if self._manifest is not None:
            return self._manifest
        directory = self.snapshot_dir
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise SWEBenchCacheError(
                f"数据快照缺少 manifest.json: {manifest_path}；缓存不完整，"
                "请重新生成快照，不要手工补文件"
            )
        try:
            text = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SWEBenchCacheError(f"无法读取 manifest: {manifest_path}") from exc
        try:
            payload = json.loads(text)
        except JSONDecodeError as exc:
            raise SWEBenchDatasetError(f"manifest.json 不是合法 JSON: {manifest_path}") from exc
        if not isinstance(payload, dict):
            raise SWEBenchDatasetError("manifest.json 顶层必须是对象")
        stored = payload.get("content_sha256")
        if not isinstance(stored, str):
            raise SWEBenchDatasetError("manifest.json 缺少 content_sha256")
        computed = manifest_content_sha256(payload)
        if computed != stored:
            raise SWEBenchCacheError(
                "manifest.json 内容摘要不符：缓存可能被改动或生成器版本不一致 "
                f"(stored={stored[:12]}…, computed={computed[:12]}…)"
            )
        try:
            manifest = DatasetManifest.model_validate(payload)
        except ValidationError as exc:
            raise SWEBenchDatasetError(f"manifest.json 结构无效: {exc}") from exc
        if manifest.dataset_name != self.dataset_name:
            raise SWEBenchDatasetError(
                f"快照 dataset_name={manifest.dataset_name!r} 与请求 {self.dataset_name!r} 不一致"
            )
        if manifest.revision != self.revision:
            raise SWEBenchDatasetError(
                f"快照 revision={manifest.revision!r} 与请求 {self.revision!r} 不一致；"
                "不得把别的 revision 的缓存当成这一版使用"
            )
        if (self.dataset_name, self.revision) == (DATASET_NAME, DATASET_REVISION):
            for split in _SPLITS:
                if manifest.splits[split].rows != OFFICIAL_SPLIT_SIZES[split]:
                    raise SWEBenchDatasetError(
                        f"官方 split {split} 行数 {manifest.splits[split].rows} 与固定事实 "
                        f"{OFFICIAL_SPLIT_SIZES[split]} 不一致"
                    )
        self._manifest = manifest
        return manifest

    @property
    def cache_identity(self) -> str:
        """缓存身份：dataset + revision + manifest 内容 + 各 split JSONL 摘要。

        revision 变化必然改变目录名，因此也必然改变这个摘要。预测/评测 manifest
        绑定它之后，旧产物不可能被误认成新 revision 的结果。
        """
        manifest = self.get_dataset_manifest()
        payload = {
            "dataset_name": manifest.dataset_name,
            "revision": manifest.revision,
            "content_sha256": manifest.content_sha256,
            "splits": {
                split: {
                    "rows": manifest.splits[split].rows,
                    "jsonl_sha256": manifest.splits[split].jsonl_sha256,
                }
                for split in _SPLITS
            },
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ 加载

    @staticmethod
    def _check_split(split: str) -> str:
        if split not in _SPLITS:
            raise SWEBenchDatasetError(f"split 必须是 {_SPLITS} 之一，收到 {split!r}")
        return split

    def load_dataset(self, split: Literal["dev", "test"]) -> None:
        """读取一个 split 并完成全量校验；重复调用是幂等的。"""
        normalized = self._check_split(split)
        if normalized in self._rows:
            return
        manifest = self.get_dataset_manifest()
        split_manifest = manifest.splits[normalized]
        jsonl_path = self._split_jsonl_path(normalized)
        if not jsonl_path.is_file():
            raise SWEBenchCacheError(f"数据快照缺少 {normalized}.jsonl: {jsonl_path}；缓存不完整")
        try:
            raw = jsonl_path.read_bytes()
        except OSError as exc:
            raise SWEBenchCacheError(f"无法读取 {jsonl_path}") from exc
        if len(raw) != split_manifest.jsonl_bytes:
            raise SWEBenchCacheError(
                f"{normalized}.jsonl 字节数 {len(raw)} != manifest 声明 "
                f"{split_manifest.jsonl_bytes}"
            )
        digest = hashlib.sha256(raw).hexdigest()
        if digest != split_manifest.jsonl_sha256:
            raise SWEBenchCacheError(
                f"{normalized}.jsonl sha256 不符（stored={split_manifest.jsonl_sha256[:12]}…, "
                f"actual={digest[:12]}…）；缓存可能被改动"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SWEBenchDatasetError(f"{normalized}.jsonl 不是 UTF-8") from exc
        entries = self._parse_jsonl(text, split=normalized)
        if len(entries) != split_manifest.rows:
            raise SWEBenchDatasetError(
                f"{normalized}.jsonl 行数 {len(entries)} != manifest 声明 {split_manifest.rows}"
            )
        if (self.dataset_name, self.revision) == (DATASET_NAME, DATASET_REVISION):
            expected = OFFICIAL_SPLIT_SIZES[normalized]
            if len(entries) != expected:
                raise SWEBenchDatasetError(
                    f"官方 {normalized} split 应为 {expected} 行，实际 {len(entries)} 行"
                )
        duplicates = _duplicates([row["instance_id"] for row in entries])
        if duplicates:
            raise SWEBenchDatasetError(f"{normalized}.jsonl 含重复 instance_id: {duplicates[:5]}")

        dataset_ref = SWEBenchDatasetRef(
            name=self.dataset_name, revision=self.revision, split=normalized
        )
        inference: dict[str, SWEBenchInferenceTask] = {}
        evaluation: dict[str, SWEBenchEvaluationTask] = {}
        for row in entries:
            inference_task, evaluation_task = self._build_tasks(
                row, split=normalized, dataset=dataset_ref
            )
            inference[inference_task.task_id] = inference_task
            evaluation[evaluation_task.instance_id] = evaluation_task
        self._rows[normalized] = {row["instance_id"]: row for row in entries}
        self._inference[normalized] = inference
        self._evaluation[normalized] = evaluation
        self._jsonl_sha256[normalized] = split_manifest.jsonl_sha256

    def list_instances(self, split: Literal["dev", "test"]) -> tuple[str, ...]:
        """稳定排序的 instance id 列表（字典序），供子集抽样/runner 复现。"""
        self.load_dataset(split)
        return tuple(sorted(self._rows[split]))

    def get_inference_task(
        self, split: Literal["dev", "test"], instance_id: str
    ) -> SWEBenchInferenceTask:
        self.load_dataset(split)
        try:
            return self._inference[split][instance_id]
        except KeyError as exc:
            raise SWEBenchDatasetError(
                f"split={split!r} 中不存在 instance_id={instance_id!r}"
            ) from exc

    def get_evaluation_task(
        self, split: Literal["dev", "test"], instance_id: str
    ) -> SWEBenchEvaluationTask:
        self.load_dataset(split)
        try:
            return self._evaluation[split][instance_id]
        except KeyError as exc:
            raise SWEBenchDatasetError(
                f"split={split!r} 中不存在 instance_id={instance_id!r}"
            ) from exc

    def validate_dataset(self) -> DatasetValidation:
        """对 dev+test 做全量结构/重复/字段校验，返回成功回执。"""
        manifest = self.get_dataset_manifest()
        for split in _SPLITS:
            self.load_dataset(split)
        official = (self.dataset_name, self.revision) == (DATASET_NAME, DATASET_REVISION)
        official_match = official and all(
            manifest.splits[split].rows == OFFICIAL_SPLIT_SIZES[split] for split in _SPLITS
        )
        return DatasetValidation(
            dataset_name=manifest.dataset_name,
            revision=manifest.revision,
            split_rows={split: manifest.splits[split].rows for split in _SPLITS},
            jsonl_sha256={split: self._jsonl_sha256[split] for split in _SPLITS},
            instances_sha256={
                split: _instances_sha256(tuple(self._inference[split])) for split in _SPLITS
            },
            content_sha256=manifest.content_sha256,
            cache_identity=self.cache_identity,
            official_sizes_match=official_match,
        )

    # ------------------------------------------------------------------ 内部

    def _parse_jsonl(self, text: str, *, split: str) -> list[dict[str, Any]]:
        if not text.strip():
            raise SWEBenchDatasetError(f"{split}.jsonl 为空")
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                raise SWEBenchDatasetError(f"{split}.jsonl 第 {line_number} 行为空行")
            try:
                payload = json.loads(line)
            except JSONDecodeError as exc:
                raise SWEBenchDatasetError(
                    f"{split}.jsonl 第 {line_number} 行不是合法 JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise SWEBenchDatasetError(f"{split}.jsonl 第 {line_number} 行必须是 JSON 对象")
            self._validate_row(payload, split=split, line_number=line_number)
            rows.append(payload)
        return rows

    @staticmethod
    def _validate_row(row: Mapping[str, Any], *, split: str, line_number: int) -> None:
        missing = [field for field in _REQUIRED_ROW_FIELDS if field not in row]
        if missing:
            raise SWEBenchDatasetError(f"{split}.jsonl 第 {line_number} 行缺字段: {missing}")
        for field in _REQUIRED_STR_FIELDS:
            value = row[field]
            if not isinstance(value, str):
                raise SWEBenchDatasetError(
                    f"{split}.jsonl 第 {line_number} 行字段 {field!r} 必须是字符串，"
                    f"收到 {type(value).__name__}"
                )
        for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            value = row[field]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise SWEBenchDatasetError(
                    f"{split}.jsonl 第 {line_number} 行字段 {field!r} 必须是字符串数组"
                    "（快照生成时应已把官方 JSON 字符串规范化）"
                )

    def _build_tasks(
        self,
        row: Mapping[str, Any],
        *,
        split: str,
        dataset: SWEBenchDatasetRef,
    ) -> tuple[SWEBenchInferenceTask, SWEBenchEvaluationTask]:
        instance_id = row["instance_id"]
        repo = row["repo"]
        version = row["version"]
        try:
            # 先构造评测对象：它覆盖全部身份字段与 gold 字段校验。这样行里的非法
            # repo/base_commit 会先以清晰的字段错误失败，而不是被"环境规格缺仓库"
            # 这类下游错误掩盖。
            evaluation = SWEBenchEvaluationTask(
                instance_id=instance_id,
                repo=repo,
                base_commit=row["base_commit"],
                version=version,
                environment_setup_commit=row["environment_setup_commit"],
                dataset=dataset,
                gold_patch=row["patch"],
                test_patch=row["test_patch"],
                fail_to_pass=tuple(row["FAIL_TO_PASS"]),
                pass_to_pass=tuple(row["PASS_TO_PASS"]),
                gold_patch_sha256=hashlib.sha256(row["patch"].encode("utf-8")).hexdigest(),
                test_patch_sha256=hashlib.sha256(row["test_patch"].encode("utf-8")).hexdigest(),
            )
            inference = SWEBenchInferenceTask(
                instance_id=instance_id,
                repo=repo,
                base_commit=row["base_commit"],
                problem_statement=row["problem_statement"],
                version=version,
                environment_setup_commit=row["environment_setup_commit"],
                dataset=dataset,
                env_spec=self._env_spec_for(repo, version),
            )
        except ValidationError as exc:
            raise SWEBenchDatasetError(
                f"split={split!r} instance={instance_id!r} 字段无效:\n{exc}"
            ) from exc
        return inference, evaluation

    def _env_spec_for(self, repo: str, version: str) -> EnvSpec:
        key = (repo, version)
        cached = self._env_spec_cache.get(key)
        if cached is not None:
            return cached
        if self._env_specs is None:
            # 不静默用 EnvSpec() 兜底：那会让装配层拿一个"看起来正常、实际与官方
            # 环境无关"的规格去建镜像，问题要到容器里才暴露。环境身份必须可追溯。
            raise SWEBenchDatasetError(
                "环境规格快照缺失，无法为 "
                f"{repo}@{version} 构造 EnvSpec；请恢复 "
                f"configs/swebench/env-specs-{HARNESS_VERSION}.json，或显式传入 "
                "env_spec_snapshot（不能用默认值冒充官方环境）"
            )
        specs = self._env_specs.load()
        repo_specs = specs.get(repo)
        if repo_specs is None:
            raise SWEBenchDatasetError(
                f"环境规格快照缺少仓库 {repo!r}（instance 版本 {version!r}）"
            )
        raw = repo_specs.get(version)
        if raw is None:
            raise SWEBenchDatasetError(
                f"环境规格快照缺少 {repo}@{version}；不能凭默认值假装是官方环境"
            )
        env_spec = _official_env_spec(raw, repo=repo, version=version)
        self._env_spec_cache[key] = env_spec
        return env_spec


def _default_env_spec_snapshot() -> Path:
    """仓库内冻结的官方规格快照（不含 swebench 依赖，纯 JSON）。"""
    from codepilot.config import PROJECT_ROOT

    repo_root = PROJECT_ROOT
    return repo_root / "configs" / "swebench" / f"env-specs-{HARNESS_VERSION}.json"


def _duplicates(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


#: 契约 §3 的规范名字是 ``DatasetManifest`` / ``DatasetValidation``；保留带前缀的
#: 别名，方便调用方一眼看出它们属于 SWE-bench 侧而不是 synthetic benchmark。
SWEBenchDatasetManifest = DatasetManifest
SWEBenchDatasetValidation = DatasetValidation
