"""用量与成本统计（02 §15.1）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["ModelUsage", "Usage"]


class ModelUsage(BaseModel):
    """单个模型别名的用量。

    按模型分开统计是必要的：一次运行里 Planner 和 Coder 可能用不同模型，
    合并之后就无法回答"贵在哪个环节"。
    """

    alias: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost: float = 0.0
    llm_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Usage(BaseModel):
    """一次运行的累计用量。``cost`` 的单位是 ``RepairBudget.budget_currency``。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    llm_calls: int = 0
    by_model: dict[str, ModelUsage] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """预算比较用的是输入 + 输出。

        缓存命中的输入 token 已经计入 ``input_tokens``，不重复相加。
        """
        return self.input_tokens + self.output_tokens

    def add_call(
        self,
        *,
        alias: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        """累加一次 LLM 调用的用量。

        重试也计入（02 §5.4：重试不计入 Agent 迭代次数，但计入费用）。
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost += cost
        self.llm_calls += 1

        per_model = self.by_model.get(alias)
        if per_model is None:
            per_model = ModelUsage(alias=alias)
            self.by_model[alias] = per_model
        per_model.input_tokens += input_tokens
        per_model.output_tokens += output_tokens
        per_model.cached_input_tokens += cached_input_tokens
        per_model.cost += cost
        per_model.llm_calls += 1
