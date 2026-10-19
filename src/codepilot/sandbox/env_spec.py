"""环境规格及其三个受支持来源（02 §13.2）。"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from codepilot.observability.redaction import is_sensitive_key

__all__ = [
    "EnvSpec",
    "EnvSpecError",
    "EnvSpecInferenceError",
    "from_swebench_spec",
    "infer_user_env_spec",
    "load_env_spec",
    "load_swebench_env_spec",
    "resolve_env_spec",
]

DEFAULT_TEST_COMMAND = "python -m pytest -p no:cacheprovider --junitxml=/out/junit.xml"
_PYTHON_VERSION = re.compile(r"^3\.(?:10|11|12|13)$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvSpecError(ValueError):
    """环境规格存在不受支持或不安全的内容。"""


class EnvSpecInferenceError(EnvSpecError):
    """仓库没有足够信息让 CodePilot 可靠推断环境。"""


class EnvSpec(BaseModel):
    """可复现的 Python 测试环境描述。

    命令必须是单行非空字符串。它们只会在 Docker 容器中执行；绝不会交给宿主
    shell。额外字段被拒绝，避免拼错配置后静默采用默认值。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    python_version: str = "3.12"
    pre_install: tuple[str, ...] = ()
    install: tuple[str, ...] = ()
    test_command: str = DEFAULT_TEST_COMMAND
    env: dict[str, str] = Field(default_factory=dict)
    # strict：YAML 1.1 会把 `yes`/`no` 解析成布尔，宽松模式下 `True` 会静默变成
    # 1 秒超时，把一个本来能通过的仓库变成 TIMEOUT。
    timeout_seconds: int = Field(default=300, ge=1, le=3600, strict=True)

    @field_validator("python_version")
    @classmethod
    def _supported_python(cls, value: str) -> str:
        normalized = value.strip()
        if _PYTHON_VERSION.fullmatch(normalized) is None:
            raise ValueError("python_version 仅支持 3.10、3.11、3.12、3.13")
        return normalized

    @field_validator("pre_install", "install", mode="before")
    @classmethod
    def _commands_are_a_sequence(cls, value: object) -> object:
        if value is None:
            return ()
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ValueError("命令列表必须是字符串数组，不能是单个字符串")
        return value

    @field_validator("pre_install", "install")
    @classmethod
    def _valid_commands(cls, commands: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_command(command) for command in commands)

    @field_validator("test_command")
    @classmethod
    def _valid_test_command(cls, command: str) -> str:
        return _validate_command(command)

    @field_validator("env")
    @classmethod
    def _valid_environment(cls, env: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in env.items():
            if _ENV_NAME.fullmatch(key) is None:
                raise ValueError(f"非法环境变量名: {key!r}")
            if is_sensitive_key(key):
                raise ValueError(f"EnvSpec 不得包含敏感值: {key!r}")
            if "\x00" in value:
                raise ValueError(f"环境变量 {key!r} 包含 NUL")
            normalized[key] = value
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def _junit_is_mandatory(self) -> EnvSpec:
        if "--junitxml" not in self.test_command:
            raise ValueError("test_command 必须通过 --junitxml 写入 /out/junit.xml")
        if "/out/junit.xml" not in self.test_command:
            raise ValueError("JUnit XML 路径必须是 /out/junit.xml")
        return self

    def canonical_json(self) -> str:
        """稳定、无空白的缓存键输入。"""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _validate_command(command: str) -> str:
    if not isinstance(command, str):
        raise ValueError("命令必须是字符串")
    normalized = command.strip()
    if not normalized:
        raise ValueError("命令不能为空")
    if "\x00" in normalized or "\r" in normalized or "\n" in normalized:
        raise ValueError("命令必须是无 NUL 的单行字符串")
    return normalized


def load_env_spec(path: Path) -> EnvSpec:
    """从 synthetic fixture 的 ``env.yaml`` 读取严格 EnvSpec。"""
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EnvSpecError(f"无法读取 EnvSpec: {path}") from exc
    if not isinstance(payload, dict):
        raise EnvSpecError("env.yaml 顶层必须是对象")
    try:
        return EnvSpec.model_validate(payload)
    except ValueError as exc:
        raise EnvSpecError(f"env.yaml 无效: {exc}") from exc


def from_swebench_spec(spec: Mapping[str, Any]) -> EnvSpec:
    """转换 SWE-bench 官方 harness 的单个 repo/version 规格。

    不支持 conda ``environment.yml``。当前基础镜像是 Python slim，静默把 conda
    配置当 pip requirements 会制造一个看似成功、实则错误的环境。
    """
    python_version = str(spec.get("python", spec.get("python_version", "3.12")))
    packages = spec.get("packages", "")
    install: list[str] = []
    if isinstance(packages, str) and packages:
        if packages in {"requirements.txt", "requirements-dev.txt"}:
            install.append(f"python -m pip install -r {packages}")
        elif packages.endswith(("environment.yml", "environment.yaml")):
            raise EnvSpecError("该 SWE-bench 规格需要 conda，Python slim 后端暂不支持")
        elif packages != "requirements.txt":
            install.append(f"python -m pip install {packages}")
    elif isinstance(packages, Sequence):
        if not all(isinstance(item, str) for item in packages):
            raise EnvSpecError("SWE-bench packages 数组只能包含字符串")
        package_list = [item.strip() for item in packages if item.strip()]
        if package_list:
            install.append("python -m pip install " + " ".join(package_list))
    elif packages:
        raise EnvSpecError("SWE-bench packages 必须是字符串或字符串数组")

    raw_install = spec.get("install", "")
    # swebench 4.1.0 的官方规格用 ``pip_packages`` 表示在仓库安装前必须固定的依赖
    # （如 astropy 的 numpy==1.25.2 / pytest==7.4.0）。它和 ``packages`` 是两种来源：
    # 前者是 pip 包清单，后者可能是 requirements 文件或 conda environment.yml。
    # 顺序与官方环境镜像一致：先推导 packages，再装 pip_packages，最后装仓库本身。
    pip_packages = spec.get("pip_packages", ())
    if pip_packages:
        if isinstance(pip_packages, str) or not isinstance(pip_packages, Sequence):
            raise EnvSpecError("SWE-bench pip_packages 必须是字符串数组")
        if not all(isinstance(item, str) for item in pip_packages):
            raise EnvSpecError("SWE-bench pip_packages 数组只能包含字符串")
        pip_list = [item.strip() for item in pip_packages if item.strip()]
        if pip_list:
            install.append("python -m pip install " + " ".join(pip_list))

    if isinstance(raw_install, str) and raw_install.strip():
        install.append(raw_install)
    elif isinstance(raw_install, Sequence) and not isinstance(raw_install, str):
        if not all(isinstance(item, str) for item in raw_install):
            raise EnvSpecError("SWE-bench install 数组只能包含字符串")
        install.extend(raw_install)
    elif raw_install:
        raise EnvSpecError("SWE-bench install 必须是字符串或字符串数组")

    pre_install = spec.get("pre_install", ())
    if isinstance(pre_install, str):
        pre_install = [pre_install]
    elif not isinstance(pre_install, Sequence) or not all(
        isinstance(item, str) for item in pre_install
    ):
        raise EnvSpecError("SWE-bench pre_install 必须是字符串或字符串数组")
    raw_test_command = spec.get("test_cmd", spec.get("test_command", DEFAULT_TEST_COMMAND))
    if not isinstance(raw_test_command, str) or not raw_test_command.strip():
        raise EnvSpecError("SWE-bench test_cmd 必须是非空字符串")
    test_command = raw_test_command.strip()
    if "--junitxml" not in test_command:
        test_command = f"{test_command} --junitxml=/out/junit.xml"
    raw_timeout = spec.get("timeout_seconds", 300)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
        raise EnvSpecError("SWE-bench timeout_seconds 必须是整数秒")
    return EnvSpec(
        python_version=python_version,
        pre_install=tuple(str(item) for item in pre_install),
        install=tuple(install),
        test_command=test_command,
        timeout_seconds=raw_timeout,
    )


def load_swebench_env_spec(repository: str, version: str) -> EnvSpec:
    """按 repo/version 读取已安装 SWE-bench 包的官方规格；不下载数据集。"""
    try:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    except ImportError as exc:  # pragma: no cover - 可选依赖边界
        raise EnvSpecError("未安装 swebench 可选依赖，无法读取官方环境规格") from exc
    try:
        raw = MAP_REPO_VERSION_TO_SPECS[repository][version]
    except KeyError as exc:
        raise EnvSpecInferenceError(f"SWE-bench 官方规格不存在: {repository}@{version}") from exc
    if not isinstance(raw, Mapping):
        raise EnvSpecError("SWE-bench 官方规格结构无效")
    return from_swebench_spec(raw)


def infer_user_env_spec(repository: Path) -> EnvSpec:
    """从常见 Python 元数据做保守推断；信息不足时明确失败。"""
    root = Path(repository)
    if not root.is_dir():
        raise EnvSpecInferenceError(f"仓库目录不存在: {root}")

    commands: list[str] = []
    requirements = sorted(
        path.name
        for path in root.glob("requirements*.txt")
        if path.is_file() and path.parent == root
    )
    preferred = [
        name
        for name in ("requirements.txt", "requirements-test.txt", "requirements-dev.txt")
        if name in requirements
    ]
    for name in preferred or requirements:
        commands.append(f"python -m pip install -r {name}")

    pyproject = root / "pyproject.toml"
    setup_py = root / "setup.py"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise EnvSpecInferenceError("pyproject.toml 无法解析，请提供 env.yaml") from exc
        project = data.get("project", {})
        if not isinstance(project, dict):
            raise EnvSpecInferenceError("pyproject.toml 的 project 必须是对象，请提供 env.yaml")
        optional = project.get("optional-dependencies", {})
        if not isinstance(optional, dict):
            raise EnvSpecInferenceError(
                "pyproject.toml 的 optional-dependencies 必须是对象，请提供 env.yaml"
            )
        extra = next((name for name in ("test", "tests", "dev") if name in optional), None)
        commands.append(
            f"python -m pip install -e .[{extra}]" if extra else "python -m pip install -e ."
        )
    elif setup_py.is_file():
        commands.append("python -m pip install -e .")

    if not commands:
        raise EnvSpecInferenceError(
            "无法从 pyproject.toml、setup.py 或 requirements*.txt 推断环境；请提供严格的 env.yaml"
        )
    return EnvSpec(install=tuple(dict.fromkeys(commands)))


def resolve_env_spec(repository: Path, *, explicit: Path | None = None) -> EnvSpec:
    """CLI 使用的来源优先级：显式文件 → 仓库 env.yaml → 保守推断。"""
    if explicit is not None:
        return load_env_spec(explicit)
    fixture_spec = Path(repository) / "env.yaml"
    if fixture_spec.is_file():
        return load_env_spec(fixture_spec)
    return infer_user_env_spec(repository)
