"""合成任务的宿主包源码（**干净状态**，无 bug）。

每个任务的做法是：从这里的干净源码出发，把一处 ``fixed`` 换成 ``buggy`` 得到
base；gold patch 就是换回来。这样做的理由不是省事，而是可核查：

* base 与 gold 之间**只差那一处**，所以"gold 是修复"这件事由构造保证，而不是靠
  作者自述；
* 可见测试按**模块分文件**书写，且只断言与本任务种子缺陷无关的行为，于是
  PASS_TO_PASS 在 base 上必然通过（02 §17.2 的要求）。把整个包塞进一个测试文件
  的话，种子缺陷会连带打挂同文件里的无关断言，那批 fixture 的 base 就会带着
  一堆"本来就红的测试"，回归判定也随之失去意义；
* 每处 ``fixed`` 只允许在源码里出现一次，替换前会断言，写错会立刻报错而不是
  静默改掉别处。

全部只用标准库：环境构建因此只需要 ``pip install pytest``。32 个 fixture 各自要
构建一个镜像，任何第三方依赖都会让这件事变成"今天能跑、下周复现不出来"。
"""

from __future__ import annotations

__all__ = ["COMMON_ENV", "HOSTS"]

# --------------------------------------------------------------------------- budget

BUDGET_RULES = '''"""支出分类与折扣规则。"""

from __future__ import annotations

RECURRING_KEYWORDS = ("rent", "subscription", "insurance")
MEMBER_DISCOUNT = 0.10
LARGE_EXPENSE = 500.0


def categorise(amount: float, note: str) -> str:
    """把一笔支出归到固定类别里。

    先看备注关键词，再看金额门槛，最后兜底到 ``general``。演示库的约定，不是
    现实账务规则。
    """
    lowered = note.casefold()
    if "rent" in lowered or "insurance" in lowered:
        return "housing"
    if "coffee" in lowered or "lunch" in lowered:
        return "food"
    if amount > LARGE_EXPENSE:
        return "large"
    return "general"


def is_recurring(note: str) -> bool:
    """备注是否指向一项周期性支出。"""
    lowered = note.casefold()
    return any(keyword in lowered for keyword in RECURRING_KEYWORDS)


def apply_discount(total: float, *, member: bool) -> float:
    """会员打折，非会员原价。"""
    if member:
        return round(total * (1 - MEMBER_DISCOUNT), 2)
    return total
'''

BUDGET_LEDGER = '''"""账本：持有分录列表并派生汇总。"""

from __future__ import annotations

from dataclasses import dataclass, field

from budget.rules import categorise


class InvalidAmount(Exception):
    """金额不合法。"""


@dataclass
class Entry:
    """一笔分录。"""

    amount: float
    note: str
    category: str


@dataclass
class Ledger:
    """一个可变的账本。"""

    entries: list[Entry] = field(default_factory=list)

    def add(self, amount: float, note: str) -> Entry:
        """记一笔。金额必须为正——退款走 :meth:`refund`，不混在这里。"""
        if amount <= 0:
            raise InvalidAmount(f"amount must be positive, got {amount}")
        entry = Entry(amount=amount, note=note, category=categorise(amount, note))
        self.entries.append(entry)
        return entry

    def refund(self, index: int, amount: float) -> Entry:
        """对第 ``index`` 笔做部分退款，记一条负分录。"""
        if amount <= 0:
            raise InvalidAmount(f"refund amount must be positive, got {amount}")
        original = self.entries[index]
        entry = Entry(amount=-amount, note=f"refund of {original.note}", category=original.category)
        self.entries.append(entry)
        return entry

    def remove(self, index: int) -> Entry:
        """删除第 ``index`` 笔并返回它。"""
        return self.entries.pop(index)

    def total(self) -> float:
        """所有分录之和。"""
        return round(sum(entry.amount for entry in self.entries), 2)

    def by_category(self) -> dict[str, float]:
        """按类别汇总。"""
        totals: dict[str, float] = {}
        for entry in self.entries:
            totals[entry.category] = round(totals.get(entry.category, 0.0) + entry.amount, 2)
        return dict(sorted(totals.items()))

    def snapshot(self) -> list[Entry]:
        """当前分录的**副本**列表，供调用方安全持有。"""
        return list(self.entries)
'''

BUDGET_CSV = '''"""CSV 导出的列定义。

列顺序在 :data:`COLUMNS` 里唯一定义，:func:`budget.report.build_rows` 依赖它决定
每行有哪些字段。所以"给导出加一列"必须同时改这两个文件——少改一个，导出的表头
与数据就会错位，而 :func:`render_row` 会按下标取到一个根本不存在的键。
"""

from __future__ import annotations

COLUMNS = ("category", "note", "currency", "amount")


def headers() -> tuple[str, ...]:
    """导出文件的列名，顺序即写入顺序。"""
    return COLUMNS


def render_row(values: dict[str, object]) -> str:
    """按列顺序把一条记录拼成一行 CSV。"""
    return ",".join(str(values[column]) for column in COLUMNS)
'''

BUDGET_REPORT = '''"""把账本渲染成 CSV 文本。"""

from __future__ import annotations

from budget.csv_writer import headers, render_row
from budget.ledger import Ledger

EXPORT_CURRENCY = "CNY"


def build_rows(ledger: Ledger) -> list[dict[str, object]]:
    """把账本转成"列名 -> 值"的记录列表，键集合与 :func:`headers` 一致。"""
    rows: list[dict[str, object]] = []
    for entry in ledger.entries:
        rows.append(
            {
                "category": entry.category,
                "note": entry.note,
                "currency": EXPORT_CURRENCY,
                "amount": entry.amount,
            }
        )
    return rows


def render(ledger: Ledger) -> str:
    """整个导出文件的内容，含表头行。"""
    lines = [",".join(headers())]
    lines.extend(render_row(row) for row in build_rows(ledger))
    return "\\n".join(lines) + "\\n"
'''

BUDGET_TEST_RULES = '''"""``budget.rules`` 的现有测试。"""

from __future__ import annotations

from budget.rules import apply_discount, categorise, is_recurring


def test_categorise_detects_insurance() -> None:
    assert categorise(120.0, "Insurance renewal") == "housing"


def test_categorise_uses_coffee_keyword() -> None:
    assert categorise(12.0, "Coffee") == "food"


def test_categorise_large_fallback() -> None:
    assert categorise(900.0, "desk") == "large"
    assert categorise(20.0, "desk") == "general"


def test_is_recurring_rejects_unrelated_note() -> None:
    assert is_recurring("one off gift") is False


def test_discount_is_only_about_membership() -> None:
    """不检查两个分支的具体归属，只确认它是纯函数且原价可回显。

    ``apply_discount`` 的会员分支正是本 suite 的一个种子缺陷（条件写反），把两个
    分支都写死在可见测试里会让 base 一上来就是红的，而 base 必须是"缺陷尚未被
    任何现有测试抓到"的状态。
    """
    assert apply_discount(0.0, member=True) == 0.0
'''

BUDGET_TEST_LEDGER = '''"""``budget.ledger`` 的现有测试。"""

from __future__ import annotations

from budget.ledger import Entry, Ledger


def test_add_records_an_entry() -> None:
    ledger = Ledger()
    entry = ledger.add(20.0, "Coffee")
    assert ledger.entries == [entry]
    assert entry.category == "food"


def test_total_sums_entries() -> None:
    ledger = Ledger()
    ledger.add(20.0, "Coffee")
    ledger.add(30.5, "Lunch")
    assert ledger.total() == 50.5


def test_by_category_groups() -> None:
    ledger = Ledger()
    ledger.add(20.0, "Coffee")
    ledger.add(5.0, "Coffee")
    assert ledger.by_category() == {"food": 25.0}


def test_remove_pops_the_entry() -> None:
    ledger = Ledger()
    ledger.add(1.0, "Coffee")
    ledger.add(2.0, "Lunch")
    assert ledger.remove(0).amount == 1.0
    assert len(ledger.entries) == 1


def test_refund_adds_an_entry() -> None:
    ledger = Ledger()
    ledger.add(50.0, "Lunch")
    ledger.refund(0, 10.0)
    assert isinstance(ledger.entries[-1], Entry)
'''

BUDGET_TEST_REPORT = '''"""``budget.report`` / ``budget.csv_writer`` 的现有测试。

这些断言刻意**不把列名写死**：加一列是本库允许的演进（见 cross-module 任务），
写死列名的可见测试会在修复后变红，于是那个修复被自己的测试挡住了。
"""

from __future__ import annotations

from budget.csv_writer import headers
from budget.ledger import Ledger
from budget.report import build_rows, render


def _ledger() -> Ledger:
    ledger = Ledger()
    ledger.add(20.0, "Coffee")
    return ledger


def test_headers_is_a_tuple_of_names() -> None:
    names = headers()
    assert isinstance(names, tuple) and len(names) >= 3
    assert all(isinstance(name, str) for name in names)


def test_build_rows_matches_headers() -> None:
    rows = build_rows(_ledger())
    assert set(rows[0]) == set(headers())


def test_render_starts_with_header_line() -> None:
    text = render(_ledger())
    assert text.splitlines()[0] == ",".join(headers())
'''

# -------------------------------------------------------------------------- textkit

TEXTKIT_SLICES = '''"""窗口与切片工具。"""

from __future__ import annotations


def first_n(values: list[int], count: int) -> list[int]:
    """前 ``count`` 个；不足时返回全部。"""
    return values[:count]


def window_start(page: int, size: int) -> int:
    """第 ``page`` 页（从 0 开始）的起始下标。"""
    return page * size


def tail(text: str, count: int) -> str:
    """结尾 ``count`` 个字符；``count`` 为 0 时返回空串。"""
    return text[len(text) - count :] if count else ""
'''

TEXTKIT_NORMALISE = '''"""文本清洗。"""

from __future__ import annotations


def split_fields(line: str, separator: str = ",", *, max_split: int = -1) -> list[str]:
    """按分隔符切列并去掉每列两端空白。空行返回空列表。"""
    if not line.strip():
        return []
    return [part.strip() for part in line.split(separator, max_split)]


def collapse_whitespace(text: str) -> str:
    """把连续空白压成单个空格，并去掉首尾空白。"""
    return " ".join(text.split())


def title(value: str) -> str:
    """首字母大写，其余原样。"""
    return value[:1].upper() + value[1:] if value else value
'''

TEXTKIT_STATS = '''"""小统计库。"""

from __future__ import annotations


class EmptySample(Exception):
    """样本为空，统计量没有定义。"""


def mean(values: list[float]) -> float:
    """算术平均。空输入没有定义，必须报错而不是除零。"""
    if not values:
        raise EmptySample("mean of an empty sample")
    return sum(values) / len(values)


def running_unique(items: list[str]) -> list[str]:
    """按首次出现顺序去重。"""
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def spread(values: list[float]) -> float:
    """极差（最大减负）。"""
    return max(values) - min(values)
'''

TEXTKIT_PAGING = '''"""分页：偏移量只能来自 :func:`textkit.slices.window_start`。

在 ``page`` 里另写一遍偏移量算术是这个库出现过错位的直接原因：两处一旦不同步，
调用方看到的就是"第 2 页从第 2 条开始"。
"""

from __future__ import annotations

from textkit.slices import window_start


def page(values: list[int], number: int, size: int) -> list[int]:
    """第 ``number`` 页（从 0 开始）的元素。"""
    start = window_start(number, size)
    return values[start : start + size]
'''

TEXTKIT_TABLE = '''"""把纯文本表格解析成行。

单元格清洗一律走 :func:`textkit.normalise.split_fields`；这里再切一遍就等于
第二套规则，而两套规则不会同时修好。
"""

from __future__ import annotations

from textkit.normalise import split_fields


def parse_row(line: str) -> list[str]:
    """一行 -> 去掉两端空白的单元格列表。"""
    return split_fields(line)


def parse_table(text: str) -> list[list[str]]:
    """整个文本块 -> 行列表；空行被跳过。"""
    return [parse_row(line) for line in text.splitlines() if line.strip()]
'''

TEXTKIT_TEST_SLICES = '''"""``textkit.slices`` 的现有测试。"""

from __future__ import annotations

from textkit.slices import first_n


def test_first_n_with_short_input() -> None:
    assert first_n([1, 2], 5) == [1, 2]


def test_first_n_with_two_of_four() -> None:
    assert first_n([1, 2, 3, 4], 2) == [1, 2]
'''

TEXTKIT_TEST_NORMALISE = '''"""``textkit.normalise`` 的现有测试。"""

from __future__ import annotations

from textkit.normalise import collapse_whitespace, split_fields, title


def test_split_fields_blank_line() -> None:
    assert split_fields("   ") == []


def test_split_fields_honours_max_split() -> None:
    assert split_fields("a,b,c", ",", max_split=1) == ["a", "b,c"]


def test_collapse_whitespace() -> None:
    assert collapse_whitespace("  a\\tb\\n c ") == "a b c"


def test_title_of_word() -> None:
    assert title("hello") == "Hello"
'''

TEXTKIT_TEST_STATS = '''"""``textkit.stats`` 的现有测试。"""

from __future__ import annotations

from textkit.stats import mean, running_unique


def test_mean_of_sample() -> None:
    assert mean([1.0, 2.0, 3.0]) == 2.0


def test_running_unique_keeps_first_occurrence_order() -> None:
    assert running_unique(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
'''

# ----------------------------------------------------------------------- httpclient

HTTP_ERRORS = '''"""HTTP 客户端的异常层级。"""

from __future__ import annotations


class HttpError(Exception):
    """本库所有错误的基类。"""


class ConfigError(HttpError):
    """配置不合法：缺超时、重试次数为负等。"""


class TransportError(HttpError):
    """底层传输失败。"""
'''

HTTP_CONFIG = '''"""客户端配置。"""

from __future__ import annotations

from httpclient.errors import ConfigError

MIN_TIMEOUT = 0.001


def validate_timeout(value: float) -> float:
    """超时必须是正数。"""
    if value < MIN_TIMEOUT:
        raise ConfigError(f"timeout must be >= {MIN_TIMEOUT}, got {value}")
    return value


def validate_retries(value: int) -> int:
    """重试次数必须是非负整数。"""
    if value < 0:
        raise ConfigError(f"retries must be >= 0, got {value}")
    return value


class Config:
    """一次客户端配置的取值。"""

    def __init__(self, *, base_url: str, timeout: float = 5.0, retries: int = 2) -> None:
        self.base_url = base_url
        self.timeout = validate_timeout(timeout)
        self.retries = validate_retries(retries)

    def as_dict(self) -> dict[str, object]:
        """把配置摊平成可序列化的字典。"""
        return {"base_url": self.base_url, "timeout": self.timeout, "retries": self.retries}
'''

HTTP_TRANSPORT = '''"""可替换的传输层。

测试注入假的 ``sender``，所以这一层不需要网络——benchmark 的 fixture 必须能在
断网容器里执行（03 §13）。

发送协议是 ``(url, timeout, retries)`` 三个参数：重试次数由配置决定、由传输层
执行，所以它必须一路传下来。少传一个参数，传输层就会用自己那份默认值。
"""

from __future__ import annotations

from collections.abc import Callable


class Recorder:
    """记录每次调用并返回预设响应。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, float, int]] = []

    def __call__(self, url: str, timeout: float, retries: int) -> str:
        self.calls.append((url, timeout, retries))
        if not self.responses:
            raise AssertionError("Recorder 的预设响应已用尽")
        return self.responses.pop(0)


def build_sender(*, responses: list[str]) -> Callable[[str, float, int], str]:
    """构造一个可注入的发送函数。"""
    return Recorder(responses=responses)
'''

HTTP_CLIENT = '''"""客户端：把配置与传输拼起来。"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urljoin

from httpclient.config import Config

Sender = Callable[[str, float, int], str]


class Client:
    """一个最小的 HTTP 客户端。"""

    def __init__(self, config: Config, *, sender: Sender) -> None:
        self.config = config
        self.sender = sender

    def request(self, path: str) -> str:
        """发一次请求。超时与重试次数都取自配置，不在这里重写。"""
        url = urljoin(self.config.base_url, path)
        return self.sender(url, self.config.timeout, self.config.retries)

    def request_with_retries(self, path: str) -> str:
        """按配置的重试次数重发，直到成功。"""
        attempts = self.config.retries + 1
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return self.request(path)
            except Exception as exc:  # 传输层抛什么异常不由本库规定
                last = exc
        assert last is not None
        raise last


def new_client(*, base_url: str, responses: list[str], **kwargs: object) -> Client:
    """便捷构造：注入一个回放 ``responses`` 的 sender，并把其余配置转给 :class:`Config`。"""
    from httpclient.transport import build_sender

    config = Config(base_url=base_url, **kwargs)  # type: ignore[arg-type]
    return Client(config, sender=build_sender(responses=list(responses)))
'''

HTTP_TEST_ERRORS = '''"""``httpclient.errors`` 的现有测试。"""

from __future__ import annotations

from httpclient.errors import ConfigError, HttpError, TransportError


def test_hierarchy() -> None:
    assert issubclass(ConfigError, HttpError)
    assert issubclass(TransportError, HttpError)


def test_messages_survive() -> None:
    assert "bad" in str(ConfigError("bad input"))
'''

HTTP_TEST_CONFIG = '''"""``httpclient.config`` 的现有测试。"""

from __future__ import annotations

from httpclient.config import Config, validate_retries


def test_defaults() -> None:
    config = Config(base_url="https://example.invalid/")
    assert config.timeout == 5.0
    assert config.retries == 2


def test_as_dict_carries_base_url() -> None:
    config = Config(base_url="https://example.invalid/", timeout=1.5)
    assert config.as_dict()["base_url"] == "https://example.invalid/"


def test_zero_retries_is_allowed() -> None:
    assert validate_retries(0) == 0


def test_positive_retries_pass_through() -> None:
    assert validate_retries(3) == 3
'''

HTTP_TEST_CLIENT = '''"""``httpclient.client`` 的现有测试。

这些测试刻意写成**不依赖 sender 的参数个数**，也不依赖 ``new_client`` 是否转发
配置关键字。本库允许"把重试次数一路传到传输层"这类跨文件演进（见 cross-module
任务），把签名写死的可见测试会在正确修复之后变红，于是修复被自己的测试挡住。
"""

from __future__ import annotations

import pytest

from httpclient.client import Client, new_client
from httpclient.config import Config
from httpclient.transport import build_sender


def failing_sender(*args: object, **kwargs: object) -> str:
    raise ConnectionError("nope")


def test_request_returns_response() -> None:
    client = new_client(base_url="https://example.invalid/api/", responses=["ok"])
    assert client.request("users") == "ok"


def test_request_uses_the_configured_timeout() -> None:
    sender = build_sender(responses=["ok"])
    client = Client(Config(base_url="https://example.invalid/", timeout=2.5), sender=sender)
    client.request("users")
    assert sender.calls[0][1] == 2.5


def test_retries_exhaust_and_raise() -> None:
    client = Client(Config(base_url="https://example.invalid/", retries=1), sender=failing_sender)
    with pytest.raises(ConnectionError):
        client.request_with_retries("a")


def test_build_sender_returns_a_callable() -> None:
    assert callable(build_sender(responses=["a", "b"]))
'''

# ----------------------------------------------------------------------- taskqueue

TASKQUEUE_QUEUE = '''"""FIFO 任务队列。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Task:
    """一项任务。"""

    name: str
    payload: dict[str, object] = field(default_factory=dict)


class TaskQueue:
    """一个可观察长度的队列。"""

    def __init__(self, tasks: list[Task] | None = None) -> None:
        self._items: deque[Task] = deque(tasks or [])
        self.history: list[str] = []

    def push(self, task: Task) -> None:
        """入队，并记录一次操作。"""
        self._items.append(task)
        self.history.append(f"push:{task.name}")

    def pop(self) -> Task | None:
        """出队；空队列返回 ``None`` 而不是抛异常。"""
        if not self._items:
            return None
        task = self._items.popleft()
        self.history.append(f"pop:{task.name}")
        return task

    def peek(self) -> Task | None:
        """看队首但不取出。"""
        return self._items[0] if self._items else None

    def drain(self) -> list[Task]:
        """取出全部任务，并且把队列清空。"""
        items = list(self._items)
        self._items.clear()
        return items

    def __len__(self) -> int:
        return len(self._items)
'''

TASKQUEUE_PRIORITY = '''"""任务优先级。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from taskqueue.queue import Task

WEIGHTS = {"urgent": 0, "normal": 1, "low": 2}


@dataclass(frozen=True)
class Ranked:
    """带权重的任务。"""

    task: Task
    weight: int


def rank(task: Task) -> int:
    """按 payload 里的 ``level`` 给出权重；未知等级按 normal。"""
    level = str(task.payload.get("level", "normal"))
    return WEIGHTS.get(level, WEIGHTS["normal"])


def order(tasks: Sequence[Task]) -> list[Task]:
    """按权重升序；权重相同的保持原顺序。"""
    return sorted(tasks, key=rank)


def top(tasks: Sequence[Task]) -> Task | None:
    """优先级最高的一项；空输入返回 ``None``。"""
    if not tasks:
        return None
    return min(tasks, key=rank)
'''

TASKQUEUE_STORE = '''"""带缓存的键值存储。

``_rendered`` 缓存 :meth:`get` 的结果。**写路径必须让对应的缓存失效**，否则读到的
还是旧值。这类缺陷只在第二次读之后出现，也因此最容易躲过小范围的冒烟测试。
"""

from __future__ import annotations


class MissingKey(Exception):
    """键不存在。"""


class Store:
    """内存键值存储，带一层渲染缓存。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._rendered: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        """写入。缓存里这一条必须一起清掉。"""
        self._data[key] = value
        self._rendered.pop(key, None)

    def get(self, key: str) -> str:
        """读取渲染结果并缓存它。"""
        cached = self._rendered.get(key)
        if cached is not None:
            return cached
        if key not in self._data:
            raise MissingKey(key)
        rendered = self._data[key].upper()
        self._rendered[key] = rendered
        return rendered

    def delete(self, key: str) -> None:
        """删除一条记录。"""
        self._data.pop(key, None)
        self._rendered.pop(key, None)

    def keys(self) -> list[str]:
        """排序后的键列表。"""
        return sorted(self._data)
'''

TASKQUEUE_RETRY = '''"""重试策略。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


class Exhausted(Exception):
    """所有尝试都失败了。"""


@dataclass(frozen=True)
class RetryPolicy:
    """最多尝试 ``attempts`` 次（含第一次）。"""

    attempts: int = 3

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts 至少为 1")


def run(function: Callable[[], str], policy: RetryPolicy) -> str:
    """执行 ``function``，失败时按策略重试。"""
    last: Exception | None = None
    for _ in range(policy.attempts):
        try:
            return function()
        except Exception as exc:
            last = exc
    raise Exhausted(f"all {policy.attempts} attempts failed") from last
'''

TASKQUEUE_TEST_QUEUE = '''"""``taskqueue.queue`` 的现有测试。"""

from __future__ import annotations

from taskqueue.queue import Task, TaskQueue


def test_push_then_pop_is_fifo() -> None:
    queue = TaskQueue()
    queue.push(Task("a"))
    queue.push(Task("b"))
    assert queue.pop() is not None
    assert queue.pop() is not None
    assert len(queue) == 0


def test_pop_on_empty_returns_none() -> None:
    assert TaskQueue().pop() is None


def test_peek_does_not_remove() -> None:
    queue = TaskQueue([Task("only")])
    assert queue.peek() is not None
    assert len(queue) == 1


def test_drain_returns_every_task() -> None:
    queue = TaskQueue([Task("a"), Task("b")])
    assert len(queue.drain()) == 2


def test_history_records_operations() -> None:
    queue = TaskQueue()
    queue.push(Task("x"))
    queue.pop()
    assert queue.history == ["push:x", "pop:x"]
'''

TASKQUEUE_TEST_PRIORITY = '''"""``taskqueue.priority`` 的现有测试。"""

from __future__ import annotations

from taskqueue.priority import order, rank, top
from taskqueue.queue import Task


def test_rank_defaults_to_normal() -> None:
    assert rank(Task("x")) == 1


def test_rank_reads_payload() -> None:
    assert rank(Task("x", payload={"level": "urgent"})) == 0
    assert rank(Task("x", payload={"level": "bogus"})) == 1


def test_top_of_empty_is_none() -> None:
    assert top([]) is None


def test_top_picks_urgent() -> None:
    tasks = [Task("n"), Task("u", payload={"level": "urgent"})]
    assert top(tasks) is not None
    assert top(tasks).name == "u"
'''

TASKQUEUE_TEST_STORE = '''"""``taskqueue.store`` 的现有测试。"""

from __future__ import annotations

from taskqueue.store import Store


def test_set_then_get() -> None:
    store = Store()
    store.set("a", "value")
    assert store.get("a") == "VALUE"


def test_delete_removes_the_key() -> None:
    store = Store()
    store.set("a", "x")
    store.delete("a")
    assert store.keys() == []


def test_keys_are_sorted() -> None:
    store = Store()
    store.set("b", "1")
    store.set("a", "2")
    assert store.keys() == ["a", "b"]
'''

TASKQUEUE_TEST_RETRY = '''"""``taskqueue.retry`` 的现有测试。"""

from __future__ import annotations

import pytest

from taskqueue.retry import Exhausted, RetryPolicy, run


def test_policy_accepts_positive_attempts() -> None:
    assert RetryPolicy(attempts=1).attempts == 1


def test_run_succeeds_eventually() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("nope")
        return "ok"

    assert run(flaky, RetryPolicy(attempts=3)) == "ok"


def test_run_exhausts_and_wraps_cause() -> None:
    def always_fails() -> str:
        raise RuntimeError("nope")

    with pytest.raises(Exhausted) as info:
        run(always_fails, RetryPolicy(attempts=2))
    assert isinstance(info.value.__cause__, RuntimeError)
'''

# ----------------------------------------------------------------------- inventory

INVENTORY_STOCK = '''"""库存：持有数量状态。"""

from __future__ import annotations

from dataclasses import dataclass, field


class OutOfStock(Exception):
    """库存不足。"""


class InvalidQuantity(Exception):
    """数量不合法。"""


@dataclass
class Line:
    """订单里的一行。"""

    sku: str
    quantity: int


@dataclass
class Stock:
    """若干 SKU 的库存台账。"""

    quantities: dict[str, int] = field(default_factory=dict)

    def restock(self, sku: str, amount: int) -> None:
        """入库。数量必须为正。"""
        if amount <= 0:
            raise InvalidQuantity(f"restock amount must be positive, got {amount}")
        self.quantities[sku] = self.quantities.get(sku, 0) + amount

    def reserve(self, line: Line) -> None:
        """预留：扣减可用数量。不足时抛 :class:`OutOfStock`。"""
        available = self.quantities.get(line.sku, 0)
        if line.quantity > available:
            raise OutOfStock(f"{line.sku}: need {line.quantity}, have {available}")
        self.quantities[line.sku] = available - line.quantity

    def available(self, sku: str) -> int:
        """某 SKU 的可用数量。"""
        return self.quantities.get(sku, 0)

    def value_at(self, prices: dict[str, float]) -> float:
        """按单价表给全部库存估价。"""
        total = 0.0
        for sku, quantity in sorted(self.quantities.items()):
            total += quantity * prices.get(sku, 0.0)
        return round(total, 2)
'''

INVENTORY_PRICING = '''"""定价。"""

from __future__ import annotations

TAX_RATE = 0.05
BULK_THRESHOLD = 10
BULK_DISCOUNT = 0.08


def subtotal(quantity: int, unit_price: float) -> float:
    """小计（不含税）。数量达到门槛时打批量折扣。"""
    raw = quantity * unit_price
    if quantity >= BULK_THRESHOLD:
        raw = raw * (1 - BULK_DISCOUNT)
    return round(raw, 2)


def with_tax(amount: float) -> float:
    """含税价。"""
    return round(amount * (1 + TAX_RATE), 2)


def price_for(quantity: int, unit_price: float) -> float:
    """一行商品的含税总价。"""
    return with_tax(subtotal(quantity, unit_price))
'''

INVENTORY_SHIPPING = '''"""装箱与可运判定。"""

from __future__ import annotations

import math

from inventory.stock import Line


def box_count(weight_kg: float, capacity_kg: float) -> int:
    """需要几个箱子。必须向上取整——半箱的货发不出去。"""
    if capacity_kg <= 0:
        raise ValueError(f"capacity_kg must be positive, got {capacity_kg}")
    return math.ceil(weight_kg / capacity_kg)


def fits(order: list[Line], limits: dict[str, int]) -> bool:
    """订单里每一行的数量都不超过该 SKU 的单次上限。"""
    for line in order:
        if line.quantity > limits.get(line.sku, 0):
            return False
    return True


def can_ship(
    order: list[Line],
    *,
    limits: dict[str, int],
    weight_kg: float,
    capacity_kg: float,
    max_boxes: int,
) -> bool:
    """整单可运：既不超过行上限，也不需要比 ``max_boxes`` 更多的箱子。

    行上限的判定**只能**来自 :func:`fits`。在这里另写一遍比较逻辑，就等于给
    同一个规则留了第二个副本，而两个副本不会同时被修好。
    """
    if not fits(order, limits):
        return False
    return box_count(weight_kg, capacity_kg) <= max_boxes


def total_weight(lines: list[Line], per_unit: dict[str, float]) -> float:
    """订单总重。"""
    return round(sum(line.quantity * per_unit.get(line.sku, 0.0) for line in lines), 3)
'''

INVENTORY_CATALOG = '''"""商品目录与描述。"""

from __future__ import annotations

from collections.abc import Mapping

CATALOG: dict[str, str] = {"A-1": "Widget", "B-2": "Gadget", "C-3": "Sprocket"}
UNKNOWN_NAME = "unknown"


def describe(sku: str, *, catalog: Mapping[str, str] | None = None) -> str:
    """返回 ``"SKU - 名称"``。未知 SKU 用占位名，调用方不必自己挡 KeyError。"""
    table = catalog or CATALOG
    name = table.get(sku, UNKNOWN_NAME)
    return f"{sku} - {name}"


def skus(catalog: Mapping[str, str] | None = None) -> list[str]:
    """排序后的 SKU 列表。"""
    table = catalog or CATALOG
    return sorted(table)
'''

INVENTORY_TEST_STOCK = '''"""``inventory.stock`` 的现有测试。"""

from __future__ import annotations

from inventory.stock import Line, Stock


def test_restock_accumulates() -> None:
    stock = Stock()
    stock.restock("A-1", 3)
    stock.restock("A-1", 2)
    assert stock.available("A-1") == 5


def test_reserve_reduces_available() -> None:
    stock = Stock()
    stock.restock("A-1", 5)
    stock.reserve(Line("A-1", 2))
    assert stock.available("A-1") == 3


def test_unknown_sku_is_zero() -> None:
    assert Stock().available("Z-9") == 0


def test_value_at_uses_price_table() -> None:
    stock = Stock()
    stock.restock("A-1", 3)
    assert stock.value_at({"A-1": 2.5}) == 7.5
'''

INVENTORY_TEST_PRICING = '''"""``inventory.pricing`` 的现有测试。"""

from __future__ import annotations

from inventory.pricing import price_for, subtotal, with_tax


def test_subtotal_below_threshold() -> None:
    assert subtotal(5, 10.0) == 50.0


def test_subtotal_at_threshold_gets_discount() -> None:
    assert subtotal(10, 10.0) == 92.0


def test_tax_rounds_to_cents() -> None:
    assert with_tax(100.0) == 105.0


def test_price_for_combines_both() -> None:
    assert price_for(5, 10.0) == 52.5
'''

INVENTORY_TEST_SHIPPING = '''"""``inventory.shipping`` 的现有测试。"""

from __future__ import annotations

import pytest

from inventory.shipping import box_count, fits, total_weight
from inventory.stock import Line


def test_box_count_exact_division() -> None:
    assert box_count(10.0, 5.0) == 2


def test_box_count_rejects_zero_capacity() -> None:
    with pytest.raises(ValueError):
        box_count(10.0, 0.0)


def test_fits_within_limits() -> None:
    assert fits([Line("A-1", 2)], {"A-1": 5}) is True


def test_fits_rejects_over_limit() -> None:
    assert fits([Line("A-1", 9)], {"A-1": 5}) is False


def test_total_weight() -> None:
    lines = [Line("A-1", 2), Line("B-2", 3)]
    assert total_weight(lines, {"A-1": 1.5, "B-2": 0.5}) == 4.5
'''

INVENTORY_TEST_CATALOG = '''"""``inventory.catalog`` 的现有测试。"""

from __future__ import annotations

from inventory.catalog import describe, skus


def test_describe_known_sku() -> None:
    assert describe("A-1") == "A-1 - Widget"


def test_describe_with_custom_catalog() -> None:
    assert describe("X", catalog={"X": "Doohickey"}) == "X - Doohickey"


def test_skus_are_sorted() -> None:
    assert skus() == ["A-1", "B-2", "C-3"]


def test_skus_of_custom_catalog() -> None:
    assert skus(catalog={"b": "2", "a": "1"}) == ["a", "b"]
'''

HOSTS: dict[str, dict[str, str]] = {
    "budget": {
        "pyproject.toml": "[project]\nname='budget'\nversion='0.1.0'\n",
        "src/budget/__init__.py": '"""Budget demo package."""\n',
        "src/budget/rules.py": BUDGET_RULES,
        "src/budget/ledger.py": BUDGET_LEDGER,
        "src/budget/csv_writer.py": BUDGET_CSV,
        "src/budget/report.py": BUDGET_REPORT,
        "tests/test_rules.py": BUDGET_TEST_RULES,
        "tests/test_ledger.py": BUDGET_TEST_LEDGER,
        "tests/test_report.py": BUDGET_TEST_REPORT,
    },
    "textkit": {
        "pyproject.toml": "[project]\nname='textkit'\nversion='0.1.0'\n",
        "src/textkit/__init__.py": '"""Text toolkit demo package."""\n',
        "src/textkit/slices.py": TEXTKIT_SLICES,
        "src/textkit/normalise.py": TEXTKIT_NORMALISE,
        "src/textkit/stats.py": TEXTKIT_STATS,
        "src/textkit/paging.py": TEXTKIT_PAGING,
        "src/textkit/table.py": TEXTKIT_TABLE,
        "tests/test_slices.py": TEXTKIT_TEST_SLICES,
        "tests/test_normalise.py": TEXTKIT_TEST_NORMALISE,
        "tests/test_stats.py": TEXTKIT_TEST_STATS,
    },
    "httpclient": {
        "pyproject.toml": "[project]\nname='httpclient'\nversion='0.1.0'\n",
        "src/httpclient/__init__.py": '"""HTTP client demo package."""\n',
        "src/httpclient/errors.py": HTTP_ERRORS,
        "src/httpclient/config.py": HTTP_CONFIG,
        "src/httpclient/transport.py": HTTP_TRANSPORT,
        "src/httpclient/client.py": HTTP_CLIENT,
        "tests/test_errors.py": HTTP_TEST_ERRORS,
        "tests/test_config.py": HTTP_TEST_CONFIG,
        "tests/test_client.py": HTTP_TEST_CLIENT,
    },
    "taskqueue": {
        "pyproject.toml": "[project]\nname='taskqueue'\nversion='0.1.0'\n",
        "src/taskqueue/__init__.py": '"""Task queue demo package."""\n',
        "src/taskqueue/queue.py": TASKQUEUE_QUEUE,
        "src/taskqueue/priority.py": TASKQUEUE_PRIORITY,
        "src/taskqueue/store.py": TASKQUEUE_STORE,
        "src/taskqueue/retry.py": TASKQUEUE_RETRY,
        "tests/test_queue.py": TASKQUEUE_TEST_QUEUE,
        "tests/test_priority.py": TASKQUEUE_TEST_PRIORITY,
        "tests/test_store.py": TASKQUEUE_TEST_STORE,
        "tests/test_retry.py": TASKQUEUE_TEST_RETRY,
    },
    "inventory": {
        "pyproject.toml": "[project]\nname='inventory'\nversion='0.1.0'\n",
        "src/inventory/__init__.py": '"""Inventory demo package."""\n',
        "src/inventory/stock.py": INVENTORY_STOCK,
        "src/inventory/pricing.py": INVENTORY_PRICING,
        "src/inventory/shipping.py": INVENTORY_SHIPPING,
        "src/inventory/catalog.py": INVENTORY_CATALOG,
        "tests/test_stock.py": INVENTORY_TEST_STOCK,
        "tests/test_pricing.py": INVENTORY_TEST_PRICING,
        "tests/test_shipping.py": INVENTORY_TEST_SHIPPING,
        "tests/test_catalog.py": INVENTORY_TEST_CATALOG,
    },
}

# Task-local contracts: do not change the historical hosts used by other fixtures.
PRECISION_BASE = '''    def by_category(self) -> dict[str, float]:
        """按类别汇总；十进制金额最终取两位，半分取偶，类别独立。"""
        totals: dict[str, float] = {}
        for entry in self.entries:
            totals[entry.category] = totals.get(entry.category, 0.0) + entry.amount
        return dict(sorted(totals.items()))
'''

PRECISION_GOLD = '''    def by_category(self) -> dict[str, float]:
        """按类别汇总；十进制金额最终取两位，半分取偶，类别独立。"""
        from fractions import Fraction

        totals: dict[str, Fraction] = {}
        for entry in self.entries:
            amount = Fraction(str(entry.amount))
            totals[entry.category] = totals.get(entry.category, Fraction(0)) + amount
        return {category: float(round(total, 2)) for category, total in sorted(totals.items())}
'''

_category_start = BUDGET_LEDGER.index("    def by_category(")
_category_end = BUDGET_LEDGER.index("    def snapshot(")
HOSTS["budget_precision"] = {
    **HOSTS["budget"],
    "src/budget/ledger.py": (
        BUDGET_LEDGER[:_category_start] + PRECISION_GOLD + "\n" + BUDGET_LEDGER[_category_end:]
    ),
}

# 2026-10-18 user-confirmed synthetic API; CNY is a compatibility default,
# not an inferred universal business currency. The field exists in base and gold.
HOSTS["budget_currency"] = {
    **HOSTS["budget"],
    "src/budget/ledger.py": BUDGET_LEDGER.replace(
        "    entries: list[Entry] = field(default_factory=list)",
        '    entries: list[Entry] = field(default_factory=list)\n    currency: str = "CNY"',
    ),
    "src/budget/report.py": BUDGET_REPORT.replace('EXPORT_CURRENCY = "CNY"\n\n\n', "").replace(
        '"currency": EXPORT_CURRENCY', '"currency": ledger.currency'
    ),
}

#: 所有 fixture 共用同一类运行环境：只有标准库加 pytest。
COMMON_ENV: dict[str, object] = {
    "python_version": "3.12",
    "install": ["python -m pip install pytest"],
    "env": {"PYTHONPATH": "/workspace/src"},
    "timeout_seconds": 120,
}
