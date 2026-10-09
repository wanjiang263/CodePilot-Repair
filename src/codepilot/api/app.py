"""FastAPI 应用（F-18，Phase 0 只做 ``GET /health``）。

三条来自 03 §29 的硬约束，从第一天就生效：

1. **没有配置 API key 就拒绝启动**。不是"启动后拒绝请求"——一个没有鉴权的
   服务只要监听上了端口，就有被扫到的窗口期。
2. **默认只监听 127.0.0.1**。要对外暴露必须显式改配置。
3. Agent 运行**不在请求处理函数里执行**（Phase 13 落地时走后台 worker）。
   本模块现在不包含任何运行入口，因此这条天然成立。

``/health`` 也校验 key。它不泄露任何业务信息，但一个开放的探活端点足以
让扫描器确认服务存在。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status

from codepilot.config import Settings, get_settings

__all__ = ["api_key_dependency", "create_app", "verify_api_key"]


def verify_api_key(provided: str | None, settings: Settings) -> None:
    """校验 ``X-API-Key``，不通过则抛 401。

    用 ``secrets.compare_digest`` 而不是 ``==``：字符串比较会在第一个不同的
    字节处提前返回，把密钥长度和前缀泄露给能测量响应时间的人。
    """
    expected = settings.api_key
    if expected is None:
        # create_app 已经拦住了这种情况，这里是防御性兜底：
        # 如果有人绕开工厂函数直接挂载路由，也不能变成开放服务。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务未配置 API key",
        )

    if provided is None or not secrets.compare_digest(provided, expected.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少或错误的 X-API-Key",
        )


def api_key_dependency(settings: Settings) -> Callable[..., None]:
    """为一份**具体的** Settings 生成校验依赖。

    必须是工厂函数，不能写成 ``Depends(get_settings)``：后者读的是进程级单例，
    于是 ``create_app(my_settings)`` 会用一份配置做启动检查、用另一份做请求校验。
    症状是所有请求都返回 503「服务未配置 API key」，而配置明明传进去了。
    """

    def verify(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        verify_api_key(x_api_key, settings)

    return verify


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用。

    Raises:
        RuntimeError: 未配置 ``CODEPILOT_API_KEY``。拒绝启动而不是降级为
            无鉴权模式——后者会让"忘了配"变成"悄悄对外开放"。
    """
    resolved = settings if settings is not None else get_settings()

    if resolved.api_key is None or not resolved.api_key.get_secret_value().strip():
        raise RuntimeError(
            "未配置 CODEPILOT_API_KEY，拒绝启动。\n"
            "请在 .env 中设置 CODEPILOT_API_KEY=<一个足够长的随机串>。\n"
            "本服务不提供无鉴权模式。"
        )

    app = FastAPI(
        title="CodePilot-Repair API",
        version="0.1.0",
        description=(
            "仓库级自主修复 Agent 的 HTTP 接口。"
            "Phase 0 只提供 /health；任务提交与事件流见 Phase 13。"
        ),
        # 文档页与 OpenAPI 描述本身是**无需鉴权的静态响应**，
        # 会把全部端点、参数与模型结构告诉未授权的人。关掉。
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get(
        "/health",
        summary="健康检查",
        dependencies=[Depends(api_key_dependency(resolved))],
    )
    def health() -> dict[str, str]:
        """返回服务状态。

        刻意只回报常量：任何来自配置或环境的字段（模型名、路径、版本）
        都是给扫描器的免费信息。
        """
        return {"status": "ok"}

    return app
