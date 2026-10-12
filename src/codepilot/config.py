"""应用级配置（03 §24）。

职责边界：这里只放**进程级**配置（路径、数据库、日志、API 监听地址）。
LLM 相关的配置（provider、模型、单价、角色映射）在 ``configs/llm.yaml``，
由 ``codepilot.llm.config`` 负责加载。分开的理由：前者随部署环境变化，
后者随模型版本变化，混在一起会导致换个模型要动环境变量。

密钥不在这里。``.env`` 里存的是 key 的**值**，配置文件里只写 key 的**环境变量名**
（如 ``api_key_env: DEEPSEEK_API_KEY``），由 provider 在调用时自行读取。
这样配置可以安全地提交和打印。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings", "read_secret_env", "reset_settings_cache"]

#: 仓库根目录。src/codepilot/config.py → parents[2]。
#: 只用于定位随包分发的默认配置（``configs/``），不用于运行产物路径。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """从环境变量与 ``.env`` 读取的进程级配置。

    所有字段都可以用 ``CODEPILOT_`` 前缀的环境变量覆盖。
    ``DATABASE_URL`` 与 ``REDIS_URL`` 额外接受不带前缀的写法，因为它们更可能
    由部署环境（容器、CI）统一注入。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="CODEPILOT_",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 路径 ----
    config_dir: Path = Field(
        default=PROJECT_ROOT / "configs",
        description="LLM 配置、消融配置、子集清单所在目录。",
    )
    llm_config: Path | None = Field(
        default=None,
        description="LLM 配置文件。None 表示使用 config_dir/llm.yaml。",
    )
    default_config: Path | None = Field(
        default=None,
        description="默认运行配置（预算等）。None 表示使用内置默认值。",
    )
    artifacts_dir: Path = Field(
        default=Path("artifacts"),
        description="运行产物根目录。相对路径按当前工作目录解析。",
    )
    cache_dir: Path = Field(
        default=Path("cache"),
        description="仓库 bare clone 与 embedding 缓存的根目录。",
    )

    # ---- 存储 ----
    database_url: str = Field(
        default="sqlite:///codepilot.db",
        validation_alias=AliasChoices("DATABASE_URL", "CODEPILOT_DATABASE_URL"),
        description="SQLAlchemy URL。默认 SQLite；PostgreSQL 为可选后端。",
    )
    redis_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("REDIS_URL", "CODEPILOT_REDIS_URL"),
        description="仅用于 API 的跨进程任务队列。None 表示使用进程内队列。",
    )

    # ---- API ----
    api_key: SecretStr | None = Field(
        default=None,
        description="``X-API-Key`` 的期望值。为 None 时服务拒绝启动（03 §29）。",
    )
    api_host: str = Field(default="127.0.0.1", description="默认只监听回环地址。")
    api_port: int = 8000

    # ---- LLM 录制与回放 ----
    record_llm: bool = Field(
        default=False,
        description="为 True 时用 RecordingLLM 包装 provider，写 llm_calls.jsonl。",
    )
    replay_llm_path: Path | None = Field(
        default=None,
        description="指定后使用 ReplayLLM 从该文件回放，不访问网络。",
    )

    # ---- 观测 ----
    log_level: str = "INFO"

    # ---- Sandbox ----
    sandbox_log_tail_bytes: int = Field(
        default=16_384,
        ge=0,
        le=10_000_000,
        description="ExecutionResult 内 stdout/stderr 尾部的 UTF-8 字节上限。",
    )

    @property
    def resolved_llm_config(self) -> Path:
        """LLM 配置文件的实际路径。"""
        return self.llm_config or (self.config_dir / "llm.yaml")

    def ensure_dirs(self) -> None:
        """创建运行期需要的目录。

        ``config_dir`` 不在这里创建——它属于版本管理的内容，缺了应该报错
        而不是被静默建出一个空目录。
        """
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def runs_dir(self) -> Path:
        return self.artifacts_dir / "runs"

    def repo_cache_dir(self) -> Path:
        return self.cache_dir / "repos"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。

    用 ``lru_cache`` 而不是模块级常量，是为了让测试能用
    ``reset_settings_cache()`` + 环境变量构造隔离的配置。
    """
    return Settings()


def reset_settings_cache() -> None:
    """清空单例缓存。仅供测试与 CLI 重新加载配置时使用。"""
    get_settings.cache_clear()


@lru_cache(maxsize=8)
def _dotenv_values(path: str) -> dict[str, str]:
    """读取 ``.env`` 文件。

    ``Settings`` 的 ``env_file`` 只把值读进它自己的字段，**不会**写回
    ``os.environ``，所以密钥不能只靠 pydantic-settings。这里单独读一次，
    否则"把密钥写进 .env"这条指引会失效，表现为 provider 一直报未配置。
    """
    from dotenv import dotenv_values

    env_path = Path(path)
    if not env_path.exists():
        return {}
    return {key: value for key, value in dotenv_values(env_path).items() if value is not None}


def read_secret_env(name: str, *, dotenv_path: Path | None = None) -> str | None:
    """按环境变量名读取密钥。

    LLM 配置里只写变量名，真正的值在这里取。返回 ``None`` 而不是抛异常，
    由调用方决定是"该 provider 未配置"还是"必须配置"。

    查找顺序：真实环境变量优先于 ``.env``。这与常见约定一致，也让 CI 能用
    环境变量临时覆盖本地文件，而不必去改 ``.env``。
    """
    value = os.environ.get(name)
    if value is None:
        value = _dotenv_values(str(dotenv_path or (PROJECT_ROOT / ".env"))).get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None
