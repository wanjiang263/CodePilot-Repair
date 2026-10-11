"""Token 计量（02 §9.1，C-05）。

两条计数路径，语义**严格分开**：

* **精确**：模型对应的 tiktoken 编码在本地可用时，用真实 tokenizer 计数。
* **估算**：字符数 / 3.5，再留出至少 10% 的预算余量。它是启发式，
  :class:`TokenCount` 用 ``method`` 与 ``exact`` 把它标出来，下游（C-06）因此
  可以在元数据里写明"这是估算"，而不是假装已经验证过模型的 token 上限。

预算判定一律用 :attr:`TokenCount.charged`：估算路径的 charged 已经含余量，
精确路径的 charged 就是 tokens 本身。混用两个数会让"没超预算"这个结论失去含义。

离线与依赖：本模块不会安装依赖，也不发起任何 API 调用。只有调用方**显式**给出
``model`` 或 ``encoding`` 时才尝试加载对应编码；本地 tokenizer 数据缺失时
tiktoken 会自己去取它那份 BPE 文件（不是模型调用，也不是依赖安装），离线环境
设 ``CODEPILOT_DISABLE_TIKTOKEN=1`` 即强制走估算路径。仓库测试一律注入
loader，不触网。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Protocol

__all__ = [
    "CHARS_PER_TOKEN",
    "MIN_ESTIMATE_MARGIN",
    "TIKTOKEN_DISABLE_ENV",
    "CountMethod",
    "EncoderLoader",
    "EncoderResolution",
    "TokenCount",
    "TokenCounter",
    "load_tiktoken_encoder",
]

CHARS_PER_TOKEN = 3.5
"""02 §9.1 规定的估算口径：字符数 / 3.5。"""

MIN_ESTIMATE_MARGIN = 0.10
"""估算路径的最小预算余量。低于它就不算遵守 02 §9.1。"""

TIKTOKEN_DISABLE_ENV = "CODEPILOT_DISABLE_TIKTOKEN"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_UNSAFE_ENCODER_CHARS = re.compile(r"[\ud800-\udfff]")


class CountMethod(StrEnum):
    """计数口径。``estimated`` 的输出永远不该被称作"真实 token 数"。"""

    EXACT = "tiktoken"
    ESTIMATED = "chars/3.5"


@dataclass(frozen=True, slots=True)
class TokenCount:
    """一次计数的完整结论。

    ``tokens`` 是原始读数，``charged`` 是计入预算的读数（估算已含余量）。
    两者分开，是为了让"模型实际会看到多少 token"和"我们按多少预算放行"
    这两个问题各自有据可查。
    """

    tokens: int
    charged: int
    method: CountMethod
    encoding: str | None = None
    note: str | None = None

    @property
    def exact(self) -> bool:
        return self.method is CountMethod.EXACT


class Encoder(Protocol):
    """tiktoken 的 ``Encoding`` 结构：能给出 token 序列即可。"""

    def encode(self, text: str) -> Sequence[int]: ...


@dataclass(frozen=True, slots=True)
class EncoderResolution:
    """loader 的结果：要么给出可用编码器，要么给出**为什么不能用**。"""

    encoding: str | None = None
    encoder: Encoder | None = None
    note: str | None = None


EncoderLoader = Callable[[str | None, str | None], EncoderResolution]
"""``(model, encoding) -> EncoderResolution``。测试注入它即可完全离开 tiktoken。"""


def load_tiktoken_encoder(model: str | None, encoding: str | None) -> EncoderResolution:
    """默认 loader：按 model 或显式编码名解析 tiktoken 编码器。

    任何失败（依赖缺失、模型名未知、编码不可用、代理环境关闭）都返回带 ``note``
    的空结果，由 :class:`TokenCounter` 降级到估算路径——计数失败不该让整条
    流水线停下来，但必须留下能审计的原因。
    """
    if os.environ.get(TIKTOKEN_DISABLE_ENV, "").strip().lower() in _TRUTHY:
        return EncoderResolution(
            note=f"{TIKTOKEN_DISABLE_ENV}=1，离线模式：按字符数 / {CHARS_PER_TOKEN} 估算"
        )
    if model is None and encoding is None:
        return EncoderResolution(note="未指定模型或编码，无法确定 tokenizer，按估算计数")
    try:
        import tiktoken
    except Exception as exc:  # pragma: no cover - 取决于本地依赖状态
        return EncoderResolution(note=f"tiktoken 不可用（{type(exc).__name__}），按估算计数")
    try:
        name = encoding or tiktoken.encoding_name_for_model(model or "")
        return EncoderResolution(encoding=name, encoder=tiktoken.get_encoding(name))
    except Exception as exc:
        return EncoderResolution(
            note=f"无法为 {model or encoding} 建立编码器（{type(exc).__name__}），按估算计数"
        )


class TokenCounter:
    """统一计数入口。

    ``model`` 与 ``encoding`` 至少要有一个才会尝试精确计数；都不给就是显式的
    估算模式（确定性、零依赖）。``margin`` 只作用于估算路径。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        encoding: str | None = None,
        margin: float = MIN_ESTIMATE_MARGIN,
        loader: EncoderLoader | None = None,
    ) -> None:
        self._margin = _validated_margin(margin)
        resolution = (loader or load_tiktoken_encoder)(model, encoding)
        self._encoder = resolution.encoder
        self._encoding = resolution.encoding if resolution.encoder is not None else None
        self.note = resolution.note
        if resolution.encoder is None and resolution.note is None:
            self.note = "未提供可用编码器，按估算计数"

    @property
    def exact(self) -> bool:
        return self._encoder is not None

    @property
    def method(self) -> CountMethod:
        return CountMethod.EXACT if self._encoder is not None else CountMethod.ESTIMATED

    @property
    def encoding(self) -> str | None:
        return self._encoding

    def count(self, text: str) -> TokenCount:
        """计数一段文本。空串是 0，不是 ``None``。"""
        if not isinstance(text, str):
            raise TypeError(f"只能计数 str，收到 {type(text).__name__}")
        if self._encoder is None:
            return self._estimated(text)
        try:
            tokens = len(self._encoder.encode(text))
        except Exception as exc:
            # 少数模型对代理对/特殊标记会抛错；降级估算比让整条链路崩更有用。
            count = self._estimated(text)
            return TokenCount(
                tokens=count.tokens,
                charged=count.charged,
                method=count.method,
                note=f"编码 {self._encoding} 失败（{type(exc).__name__}），已降级估算",
            )
        return TokenCount(
            tokens=tokens, charged=tokens, method=CountMethod.EXACT, encoding=self._encoding
        )

    def charged(self, *texts: str) -> int:
        """预算判定口径：若干片段合计后要占用的 token 数。"""
        return sum(self.count(text).charged for text in texts)

    def fits(self, budget: int, *texts: str) -> bool:
        return self.charged(*texts) <= _validated_budget(budget)

    def _estimated(self, text: str) -> TokenCount:
        # 估算按码点计数；lone surrogate 记 2 个字符（与 UTF-16 宽度一致），宁偏保守。
        characters = len(_UNSAFE_ENCODER_CHARS.sub("??", text)) if text else 0
        raw = math.ceil(characters / CHARS_PER_TOKEN)
        # 余量用十进制算：float 的 1.1 实际是 1.100000000000000088817841970012523233890533447265625，
        # 直接乘再向上取整会把 10% 变成 11%，让"留了多少余量"这个问题失去确定答案。
        exact = Decimal(raw) * (Decimal(1) + Decimal(str(self._margin)))
        charged = int(exact.quantize(Decimal(1), rounding=ROUND_CEILING))
        return TokenCount(tokens=raw, charged=charged, method=CountMethod.ESTIMATED, note=self.note)


def _validated_margin(margin: float) -> float:
    if isinstance(margin, bool) or not isinstance(margin, (int, float)):
        raise ValueError("margin 必须是数值")
    if not math.isfinite(margin):
        raise ValueError("margin 必须是有限数")
    if margin < MIN_ESTIMATE_MARGIN:
        raise ValueError(f"估算路径的余量不得小于 {MIN_ESTIMATE_MARGIN}（02 §9.1）")
    if margin > 1.0:
        raise ValueError("margin 不得超过 1.0")
    return float(margin)


def _validated_budget(budget: int) -> int:
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise ValueError("budget 必须是整数")
    if budget < 0:
        raise ValueError("budget 不得为负")
    return budget
