"""Provider 三阶段冒烟：普通对话 / 结构化输出 / 函数调用。

02 §5.3 与 docs/llm-config.md 都留了一句话：普通对话成功**不能**推断结构化输出
与工具调用也通过。这个模块就是把那句话变成可执行的检查。

三条不可协商的设计约束：

1. **默认零请求。** ``live=False`` 时连 provider 对象都不构造。"dry-run 也会
   打一次探测请求"是这类工具最常见的事故来源，而它的症状是账单而不是报错。
2. **一切走现有契约。** 这里只使用 :class:`LLMProvider`、:class:`LLMRequest`、
   ``complete_structured`` 与 :class:`ToolRegistry`。不复现一遍 agent 逻辑，
   也不允许出现任何 GLM 专属的调用分支。
3. **失败如实分层。** 认证失败、模型不可用、超时、预算耗尽、解析失败是五种不同
   的结论，报告里分开写；任何一种都不换算成"模型能力不足"，更不回退到 FakeLLM。
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from codepilot.llm.base import LLMProvider
from codepilot.llm.config import LLMConfig, ModelSpec, ProviderSpec
from codepilot.llm.errors import LLMError, StructuredOutputError
from codepilot.llm.glm import glm_readiness
from codepilot.llm.registry import LLMMode, LLMRegistry, transport_provider
from codepilot.llm.structured import complete_structured
from codepilot.llm.types import LLMRequest, LLMResponse, Message, ToolCall
from codepilot.observability.redaction import redact
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.read_tools import read_file_tool
from codepilot.tools.registry import ToolRegistry, ToolRole, UnregisteredToolError

__all__ = [
    "DEFAULT_STAGES",
    "SmokeReport",
    "SmokeRunner",
    "SmokeStage",
    "SmokeStatus",
    "StageResult",
    "probe_registry",
    "run_smoke",
]

TEXT_QUESTION = "只回复两个字：收到"

TOOL_MARKER = "GLM-SMOKE-7QX4"
"""写进临时文件里的暗号。只有真正执行了工具才可能拿到它，因此它同时是
"模型自己编的答案"与"工具返回的答案"的判别器。"""

TOOL_PROMPT = (
    "请用 read_file 工具读取 notes.txt，然后把该文件里那行以 MARKER= 开头的暗号值"
    "原样回复给我，不要添加其他文字。"
)


class SmokeStage(StrEnum):
    TEXT = "text"
    STRUCTURED = "structured"
    TOOLS = "tools"


class SmokeStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


#: 默认三个阶段都跑；``--stage`` 可以只选一个。
DEFAULT_STAGES: tuple[SmokeStage, ...] = (
    SmokeStage.TEXT,
    SmokeStage.STRUCTURED,
    SmokeStage.TOOLS,
)


#: 冒烟用的紧凑 Schema。字段形状刻意对齐真实 Schema（必填 + 枚举 + 嵌套数组），
#: 但足够小，一次真实调用的 token 消耗可以忽略。真实四份 Schema
#: （IssueAnalysis / RepairPlan / Diagnosis / ReviewResult）的离线兼容测试在
#: tests/unit/llm/test_glm_provider.py 里做。
class SmokeVerdict(BaseModel):
    """一次只读检查的结论。"""

    verdict: Literal["pass", "fail", "unknown"]
    summary: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class StageResult:
    """一个阶段的结果。``requests`` 是真实 HTTP 次数，不是模型轮次。"""

    stage: str
    status: SmokeStatus
    detail: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = True
    currency: str = "CNY"
    finish_reason: str | None = None
    model_id: str | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status.value,
            "detail": self.detail,
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost": self.cost if self.cost_known else None,
            "cost_known": self.cost_known,
            "currency": self.currency,
            "finish_reason": self.finish_reason,
            "model_id": self.model_id,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class SmokeReport:
    provider: str
    model_alias: str
    model_id: str
    base_url: str
    currency: str
    live: bool
    stages: tuple[StageResult, ...]
    refusal: str | None = None
    usage_uncertain: bool = False

    @property
    def total_requests(self) -> int:
        return sum(item.requests for item in self.stages)

    @property
    def total_cost(self) -> float:
        return sum(item.cost for item in self.stages)

    @property
    def cost_known(self) -> bool:
        return all(item.cost_known for item in self.stages)

    @property
    def passed(self) -> bool:
        ran = [item for item in self.stages if item.status is not SmokeStatus.NOT_RUN]
        return bool(ran) and all(item.status is SmokeStatus.PASS for item in ran)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_alias": self.model_alias,
            "model_id": self.model_id,
            "base_url": self.base_url,
            "currency": self.currency,
            "live": self.live,
            "passed": self.passed,
            "refusal": self.refusal,
            "usage_uncertain": self.usage_uncertain,
            "total_requests": self.total_requests,
            "total_cost": self.total_cost if self.cost_known else None,
            "cost_known": self.cost_known,
            "stages": [item.as_dict() for item in self.stages],
        }


@dataclass
class _Tally:
    """跨阶段累计的用量与费用。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    requests: int = 0
    cost_known: bool = True

    def add(self, response: LLMResponse, *, requests: int = 1) -> None:
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.cost += response.cost
        self.requests += requests
        self.cost_known = self.cost_known and response.cost_known


class SmokeRunner:
    """把三个阶段跑在同一个 provider 上。

    ``provider_factory`` 而不是 provider 实例：dry-run 必须**一个对象都不建**，
    否则建 client 时就可能触发 DNS 或连接，"不发请求"这件事就成了口头承诺。
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        http_attempts: Callable[[], int],
        unacknowledged: Callable[[], int],
        spec: ModelSpec,
        provider_spec: ProviderSpec,
        currency: str,
        max_output_tokens: int,
        max_cost: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.http_attempts = http_attempts
        self.unacknowledged = unacknowledged
        self.spec = spec
        self.provider_spec = provider_spec
        self.currency = currency
        self.max_output_tokens = max_output_tokens
        self.max_cost = max_cost
        self.sleep = sleep
        self._tally = _Tally()

    # ------------------------------------------------------------------ 阶段

    def run_text(self) -> StageResult:
        before = self.http_attempts()
        notes: list[str] = []
        try:
            response = self.provider.complete(
                LLMRequest(
                    messages=[Message.user(TEXT_QUESTION)],
                    max_output_tokens=self.max_output_tokens,
                    purpose="smoke_text",
                )
            )
        except LLMError as exc:
            return self._failure(SmokeStage.TEXT, exc, before=before)

        attempts = self.http_attempts() - before
        self._tally.add(response, requests=attempts)
        text = (response.content or "").strip()
        if not text:
            notes.append("content 为空：可能是只输出了思考内容，也可能是空响应")
            if response.reasoning_content:
                notes.append(
                    f"reasoning_content 有 {len(response.reasoning_content)} 字符，"
                    "但未当作最终答案使用"
                )
        if response.finish_reason == "length":
            notes.append("输出被 max_tokens 截断")
        status = (
            SmokeStatus.PASS if text and response.finish_reason != "length" else SmokeStatus.FAIL
        )
        return StageResult(
            stage=SmokeStage.TEXT.value,
            status=status,
            detail=f"回复 {len(text)} 字符: {text[:60]!r}" if text else "未取得完整文本回复",
            requests=attempts,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost=response.cost,
            cost_known=response.cost_known,
            currency=self.currency,
            finish_reason=response.finish_reason,
            model_id=response.raw.get("model") if response.raw else self.spec.model,
            notes=tuple(notes),
        )

    def run_structured(self) -> StageResult:
        before = self.http_attempts()
        request = LLMRequest(
            messages=[
                Message.system("你是一个只做结构化输出的检查器。只输出 JSON 对象。"),
                Message.user(
                    "请输出一个 JSON 对象，字段为 verdict（取 pass/fail/unknown 之一）、"
                    "summary（一句非空中文）、evidence（字符串数组，可含一项）、"
                    "confidence（0 到 1 之间的小数）。"
                ),
            ],
            response_schema=SmokeVerdict,
            max_output_tokens=self.max_output_tokens,
            purpose="smoke_structured",
        )
        try:
            response = complete_structured(self.provider, request)
        except (StructuredOutputError, LLMError) as exc:
            return self._failure(SmokeStage.STRUCTURED, exc, before=before)

        attempts = self.http_attempts() - before
        self._tally.add(response, requests=attempts)
        parsed = response.parsed
        notes: list[str] = []
        if not isinstance(parsed, SmokeVerdict):
            return StageResult(
                stage=SmokeStage.STRUCTURED.value,
                status=SmokeStatus.FAIL,
                detail=f"解析结果不是 SmokeVerdict: {type(parsed).__name__}",
                requests=attempts,
                currency=self.currency,
                cost_known=False,
            )
        if response.finish_reason == "length":
            notes.append("输出被 max_tokens 截断；结构化结果可能因此不完整")
        if attempts > 1:
            notes.append(f"HTTP 尝试 {attempts} 次（含重试），费用按已发出请求累计")
        return StageResult(
            stage=SmokeStage.STRUCTURED.value,
            status=SmokeStatus.PASS,
            detail=(
                "解析为 SmokeVerdict: "
                f"verdict={parsed.verdict!r} confidence={parsed.confidence} "
                f"evidence={len(parsed.evidence)} 项"
            ),
            requests=attempts,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost=response.cost,
            cost_known=response.cost_known,
            currency=self.currency,
            finish_reason=response.finish_reason,
            model_id=response.raw.get("model") if response.raw else self.spec.model,
            notes=tuple(notes),
        )

    def run_tools(self) -> StageResult:
        """完整闭环：模型请求 → ToolCall → 校验并执行 → 结果回传 → 模型续答。

        工具用真实 :class:`ToolRegistry`（只注册只读的 ``read_file``），权限按
        PLANNER 角色隔离：模型即使点名要 ``edit_file`` 也会被拒。
        """
        before = self.http_attempts()
        with tempfile.TemporaryDirectory(prefix="codepilot-smoke-") as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text(f"noise line\nMARKER={TOOL_MARKER}\n", encoding="utf-8")
            registry = probe_registry(root)
            messages = [Message.user(TOOL_PROMPT)]
            specs = registry.specs(ToolRole.PLANNER)
            executed: list[str] = []
            rounds = 0
            final: LLMResponse | None = None

            while rounds < 4:
                rounds_start = self.http_attempts()
                rounds += 1
                try:
                    response = self.provider.complete(
                        LLMRequest(
                            messages=messages,
                            tools=specs,
                            max_output_tokens=self.max_output_tokens,
                            purpose="smoke_tools",
                        )
                    )
                except LLMError as exc:
                    return self._failure(SmokeStage.TOOLS, exc, before=before, since=rounds_start)
                final = response
                # 用"这一轮自己发了几次"记账，而不是"到现在为止一共发了几次"：
                # 后者会把前面几轮的请求重复计入，总请求数变成三角数。
                self._tally.add(response, requests=self.http_attempts() - rounds_start)
                if not response.tool_calls:
                    break
                messages.append(
                    Message(
                        role="assistant",
                        content=response.content,
                        tool_calls=response.tool_calls,
                    )
                )
                for call in response.tool_calls:
                    result = _execute_call(registry, call.name, call.arguments, call)
                    executed.append(f"{call.name}:{'ok' if result[0] else 'fail'}")
                    messages.append(Message.tool_result(call.id, result[1]))

        attempts = self.http_attempts() - before
        answer = (final.content or "").strip() if final else ""
        notes = [f"模型轮次 {rounds}", f"工具执行 {', '.join(executed) or '无'}"]
        got_marker = TOOL_MARKER in answer
        status = (
            SmokeStatus.PASS
            if final is not None and executed and any(item.endswith(":ok") for item in executed)
            else SmokeStatus.FAIL
        )
        if final is not None and not executed:
            notes.append("模型没有发起任何工具调用：函数调用契约未被验证")
        if final is not None and executed and not got_marker:
            notes.append("工具已执行但最终回复不含暗号，无法确认结果被正确回传")
            status = SmokeStatus.FAIL
        return StageResult(
            stage=SmokeStage.TOOLS.value,
            status=status,
            detail=f"最终回复 {answer[:60]!r}，暗号命中={got_marker}",
            requests=attempts,
            input_tokens=final.input_tokens if final else 0,
            output_tokens=final.output_tokens if final else 0,
            cost=final.cost if final else 0.0,
            cost_known=final.cost_known if final else True,
            currency=self.currency,
            finish_reason=final.finish_reason if final else None,
            model_id=self.spec.model,
            notes=tuple(notes),
        )

    # ------------------------------------------------------------------ 辅助

    def _failure(
        self, stage: SmokeStage, exc: LLMError, *, before: int, since: int | None = None
    ) -> StageResult:
        """``before`` 是本阶段起点（用于报告），``since`` 是本次调用起点（用于记账）。

        两者在单轮阶段里相同；在多轮工具循环里不同 —— 用阶段起点去累加会把前面几轮
        已经记过的请求再记一遍。
        """
        self._tally.requests += max(0, self.http_attempts() - (before if since is None else since))
        attempts = max(0, self.http_attempts() - before)
        body = redact(exc.body) if exc.body is not None else None
        notes = [f"异常类型 {type(exc).__name__}", f"status={exc.status}"]
        if body is not None:
            notes.append(f"响应体（已脱敏）: {str(body)[:200]}")
        if self.unacknowledged() > 0:
            notes.append(
                f"已发出但结果未知的请求 {self.unacknowledged()} 次："
                "服务端可能已生成并计费，费用按未知处理"
            )
        return StageResult(
            stage=stage.value,
            status=SmokeStatus.FAIL,
            detail=redact(str(exc)),
            requests=attempts,
            currency=self.currency,
            cost_known=False,
            notes=tuple(notes),
        )

    def over_budget(self) -> bool:
        return self.max_cost is not None and self._tally.cost > self.max_cost


def probe_registry(root: Path) -> ToolRegistry:
    """只注册只读工具的最小 registry。

    用真实的 :class:`WorkspacePathPolicy` 而不是自造一个假工具：这样"模型给出的路径
    逃出 workspace"这条防线在冒烟里也是被验证过的，而不只是写在文档里。
    """
    policy = WorkspacePathPolicy(root)
    registry = ToolRegistry()
    registry.register(read_file_tool(policy))
    return registry


def _execute_call(
    registry: ToolRegistry, name: str, arguments: dict[str, Any], call: ToolCall
) -> tuple[bool, str]:
    """校验并执行一次工具调用，返回 (是否成功, 回传给模型的文本)。

    三条拒绝路径都走这里：arguments 非法 JSON、未注册工具名、Pydantic 校验失败。
    拒绝之后仍然把错误**作为 tool result 回传**，让模型有机会改 —— 静默中断会让
    "工具调用失败"和"模型没调用工具"在报告里长一样。
    """
    if call.arguments_error:
        return False, json.dumps(
            {"ok": False, "error": {"code": "invalid_arguments", "message": call.arguments_error}},
            ensure_ascii=False,
        )
    try:
        result = registry.execute(name, arguments, role=ToolRole.PLANNER)
    except UnregisteredToolError as exc:
        return False, json.dumps(
            {"ok": False, "error": {"code": "unregistered_tool", "message": str(exc)}},
            ensure_ascii=False,
        )
    payload: dict[str, Any] = {"ok": bool(result.ok)}
    if result.ok:
        payload["data"] = result.data
    else:
        payload["error"] = result.error.model_dump() if result.error else {"code": "tool_failed"}
    return bool(result.ok), json.dumps(payload, ensure_ascii=False)[:4000]


def run_smoke(
    config: LLMConfig,
    *,
    alias: str,
    live: bool,
    stages: Sequence[SmokeStage] = DEFAULT_STAGES,
    max_output_tokens: int = 512,
    max_cost: float | None = None,
    budget_currency: str | None = None,
) -> SmokeReport:
    """装配并运行冒烟。**``live=False`` 时一个 provider 对象都不建。**

    live 的前置条件全部在这里检查（密钥、模型 ID、单价、币种、端点、费用上限），
    任何一条不满足就把相应阶段标成 NOT_RUN 并给出原因 —— 而不是打一次请求试试看
    会不会失败：那既可能产生费用，也可能把"配置没填"报告成"模型不行"。
    """
    bound = config.bind_all_roles(alias)
    currency = budget_currency or bound.budget_currency
    spec = bound.resolve_role("coder")
    provider_spec = bound.provider_for(spec)

    def report(
        stages_results: Sequence[StageResult],
        *,
        refusal: str | None = None,
        usage_uncertain: bool = False,
    ) -> SmokeReport:
        return SmokeReport(
            provider=provider_spec.name,
            model_alias=spec.alias,
            model_id=spec.model,
            base_url=provider_spec.base_url,
            currency=currency,
            live=live,
            stages=tuple(stages_results),
            refusal=refusal,
            usage_uncertain=usage_uncertain,
        )

    def planned(reason: str) -> tuple[StageResult, ...]:
        return tuple(
            StageResult(
                stage=stage.value,
                status=SmokeStatus.NOT_RUN,
                detail=reason,
                currency=currency,
            )
            for stage in stages
        )

    if not live:
        return report(planned("dry-run：未授权真实调用，本阶段没有发出任何请求"))

    refusal = glm_readiness(bound, alias, budget_currency=currency)
    if refusal is None and max_cost is None:
        refusal = (
            f"live 冒烟必须显式给出费用上限 --max-cost（单位 {currency}）：没有上限的付费调用不接受"
        )
    if refusal is not None:
        return report(planned(f"未执行：{refusal}"), refusal=refusal)

    registry = LLMRegistry(bound, mode=LLMMode.LIVE, budget_currency=currency)
    try:
        chain = registry.for_role("coder")
        inner = transport_provider(chain)
        runner = SmokeRunner(
            provider=chain,
            http_attempts=lambda: inner.attempts,
            unacknowledged=lambda: inner.unacknowledged,
            spec=spec,
            provider_spec=provider_spec,
            currency=currency,
            max_output_tokens=max_output_tokens,
            max_cost=max_cost,
        )
        results: list[StageResult] = []
        for stage in stages:
            if runner.over_budget():
                assert max_cost is not None
                results.append(
                    StageResult(
                        stage=stage.value,
                        status=SmokeStatus.NOT_RUN,
                        detail=f"未执行：累计费用已达上限 {max_cost} {currency}，后续阶段不再派发",
                        currency=currency,
                    )
                )
                continue
            results.append(_STAGE_CALLS[stage](runner))
        return report(results, usage_uncertain=inner.unacknowledged > 0)
    finally:
        registry.close()


_STAGE_CALLS: dict[SmokeStage, Callable[[SmokeRunner], StageResult]] = {
    SmokeStage.TEXT: SmokeRunner.run_text,
    SmokeStage.STRUCTURED: SmokeRunner.run_structured,
    SmokeStage.TOOLS: SmokeRunner.run_tools,
}
