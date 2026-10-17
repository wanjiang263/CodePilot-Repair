"""用量与成本统计（02 §15.1）。

按 **模型** 和按 **角色** 两套聚合同时存在是有意的："贵在哪个模型" 与
"贵在哪个环节" 是两个不同的问题，而 Planner 与 Coder 常常是同一个模型。
只有 ``by_model`` 时，一次真实 GLM 运行里 7 次调用全部塌进一个聚合项，
定位不了到底是谁在花钱。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["ModelUsage", "Usage"]


class ModelUsage(BaseModel):
    """单个聚合键（模型别名或 Agent 角色）的用量。"""

    alias: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost: float = 0.0
    llm_calls: int = 0
    cost_unknown_calls: int = 0
    """服务商没有回传可用用量的调用数。它们的 ``cost`` 记 0，但**不能**被读成
    "这次调用免费"——所以单独计数，让总费用能被声明为下界。"""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Usage(BaseModel):
    """一次运行的累计用量。``cost`` 的单位是 :attr:`currency`。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost: float = 0.0
    llm_calls: int = 0
    cost_unknown_calls: int = 0
    currency: str = "CNY"
    by_model: dict[str, ModelUsage] = Field(default_factory=dict)
    by_role: dict[str, ModelUsage] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """预算比较用的是输入 + 输出。

        缓存命中的输入 token 已经计入 ``input_tokens``，不重复相加。
        """
        return self.input_tokens + self.output_tokens

    @property
    def cost_is_lower_bound(self) -> bool:
        """至少有一次调用的费用没能确认，因此 :attr:`cost` 只是**下界**。"""
        return self.cost_unknown_calls > 0

    def add_call(
        self,
        *,
        alias: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cost: float = 0.0,
        role: str | None = None,
        cost_known: bool = True,
        currency: str | None = None,
    ) -> None:
        """累加一次 LLM 调用的用量。

        重试也计入（02 §5.4：重试不计入 Agent 迭代次数，但计入费用）。

        ``role`` 是 Agent 角色（planner / coder / …）；不传时只做模型聚合，
        但**总量仍然一致**，因为总数与角色数是同一次 ``add_call`` 里各自加同一个数。
        """
        target_currency = currency or self.currency
        if target_currency != self.currency:
            # 不同币种直接相加会得到一个没有口径的数。真实实验里 GLM 以 USD 计价、
            # 预算以 CNY 设定，混算出来的总账两边都不认。
            raise ValueError(f"不能把 {target_currency!r} 的费用计入 {self.currency!r} 的总账")

        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_input_tokens += cached_input_tokens
        self.cost += cost
        self.llm_calls += 1
        if not cost_known:
            self.cost_unknown_calls += 1

        self._accumulate(
            self.by_model,
            alias,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cost=cost,
            cost_known=cost_known,
        )
        if role:
            self._accumulate(
                self.by_role,
                role,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                cost=cost,
                cost_known=cost_known,
            )

    @staticmethod
    def _accumulate(
        bucket: dict[str, ModelUsage],
        key: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
        cost: float,
        cost_known: bool,
    ) -> None:
        item = bucket.get(key)
        if item is None:
            item = ModelUsage(alias=key)
            bucket[key] = item
        item.input_tokens += input_tokens
        item.output_tokens += output_tokens
        item.cached_input_tokens += cached_input_tokens
        item.cost += cost
        item.llm_calls += 1
        if not cost_known:
            item.cost_unknown_calls += 1

    def role_totals_reconcile(self) -> bool:
        """角色视图与模型视图是否都等于总账。恢复与并发记账的一致性自检。"""
        return self._reconciles(self.by_role) and self._reconciles(self.by_model)

    def _reconciles(self, bucket: dict[str, ModelUsage]) -> bool:
        if not bucket:
            return self.llm_calls == 0
        return (
            sum(item.input_tokens for item in bucket.values()) == self.input_tokens
            and sum(item.output_tokens for item in bucket.values()) == self.output_tokens
            and sum(item.llm_calls for item in bucket.values()) == self.llm_calls
        )

    def reset_to(self, other: Usage) -> None:
        """原地对齐到 ``other`` 的快照，保持本对象身份不变。

        Coding Agent / Planner 持有的是 :class:`Usage` 的**引用**；换成新对象会让
        后续调用记到 state 已经不再持有的对象上，恢复之后费用就悄悄丢了。
        """
        self.input_tokens = other.input_tokens
        self.output_tokens = other.output_tokens
        self.cached_input_tokens = other.cached_input_tokens
        self.cost = other.cost
        self.llm_calls = other.llm_calls
        self.cost_unknown_calls = other.cost_unknown_calls
        self.currency = other.currency
        self.by_model = {key: value.model_copy() for key, value in other.by_model.items()}
        self.by_role = {key: value.model_copy() for key, value in other.by_role.items()}
