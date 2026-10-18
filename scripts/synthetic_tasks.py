"""34 个合成任务的定义（E-05 / E-06）。

每个任务 = 一个宿主包 + 一处（或多处）"把干净代码换成带缺陷代码"的种子。
``fixed`` 必须是干净源码里**唯一出现**的子串，构建时断言；于是 base 与 gold 只差
那一处，"gold 是修复"这件事由构造保证，而不是靠作者声明。

类别配额（02 §17.3 十类，每类 >=2）、多文件修复数量、无 traceback 的 Issue 数量
都由 ``build_synthetic_fixtures.suite_requirements`` 在构建前核对，不靠这里注释。

任务之间刻意在这些维度上不同（04 明确不许 30 个任务都只是"把比较运算符改回去"）：
单函数 / 类方法 / 跨模块 / 状态更新 / 异常类型 / 边界条件 / 调用接口变化 /
多步业务逻辑；缺陷形态也分散在"条件写反、少校验、异常类型错、差一、返回值错、
用错 API、边界输入、跨文件不同步、回归丢了守卫、该复制的地方直接改"。
"""

from __future__ import annotations

from dataclasses import dataclass

from synthetic_hosts import PRECISION_BASE, PRECISION_GOLD

from codepilot.eval.synthetic import BenchmarkCategory


@dataclass(frozen=True, slots=True)
class Bug:
    """一处种子缺陷：把 ``fixed`` 换成 ``buggy`` 就得到 base。"""

    file: str
    fixed: str
    buggy: str


@dataclass(frozen=True, slots=True)
class TaskDef:
    """一个 benchmark 任务的定义。"""

    task_id: str
    host: str
    category: BenchmarkCategory
    difficulty: str
    issue: str
    bugs: tuple[Bug, ...]
    hidden: dict[str, str]


_HEADER = '"""Hidden evaluation tests (test.patch)."""\n\nfrom __future__ import annotations\n\n'


def hidden(module: str, body: str) -> dict[str, str]:
    """把一段测试正文包成 ``tests/test_<module>.py``。"""
    return {f"tests/test_{module}.py": _HEADER + body.lstrip()}


TASKS: tuple[TaskDef, ...] = (
    # =========================================================== wrong condition (3)
    TaskDef(
        task_id="budget-food-branch-drops-lunch",
        host="budget",
        category=BenchmarkCategory.WRONG_CONDITION,
        difficulty="easy",
        issue=(
            "Lunch expenses are not grouped as food.\n\n"
            "Buying lunch ends up under `general` in the category totals, while "
            "coffee is grouped correctly. Both are meals and the category rules "
            "treat them as the same group.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/rules.py",
                fixed='if "coffee" in lowered or "lunch" in lowered:',
                buggy='if "coffee" in lowered:',
            ),
        ),
        hidden=hidden(
            "lunch_branch",
            """
from budget.rules import categorise


def test_lunch_is_food() -> None:
    assert categorise(12.0, "Lunch") == "food"


def test_uppercase_lunch_is_food() -> None:
    assert categorise(30.0, "LUNCH deal") == "food"
""",
        ),
    ),
    TaskDef(
        task_id="budget-recurring-demands-every-keyword",
        host="budget",
        category=BenchmarkCategory.WRONG_CONDITION,
        difficulty="easy",
        issue=(
            "`is_recurring` answers False for almost every real note.\n\n"
            "A note such as `Rent April` names one recurring keyword and should be "
            "recognised as recurring. Listing all of the keywords in one note is not "
            "something real data does.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/rules.py",
                fixed="return any(keyword in lowered for keyword in RECURRING_KEYWORDS)",
                buggy="return all(keyword in lowered for keyword in RECURRING_KEYWORDS)",
            ),
        ),
        hidden=hidden(
            "recurring_quantifier",
            """
from budget.rules import is_recurring


def test_one_keyword_is_enough() -> None:
    assert is_recurring("Rent April") is True


def test_another_single_keyword_is_enough() -> None:
    assert is_recurring("Insurance renewal") is True
""",
        ),
    ),
    TaskDef(
        task_id="inventory-fits-accepts-unlisted-sku",
        host="inventory",
        category=BenchmarkCategory.WRONG_CONDITION,
        difficulty="medium",
        issue=(
            "Shipping approves products that have no per-shipment limit configured.\n\n"
            "`fits` should reject any line whose quantity exceeds the limit for its "
            "SKU. When the limits table has no entry for a SKU there is no limit we "
            "can check, so the line must not be waved through - the current behaviour "
            "lets it through, and the warehouse has been shipping single-unit orders "
            "for SKUs that are not configured at all.\n"
        ),
        bugs=(
            Bug(
                file="src/inventory/shipping.py",
                fixed="if line.quantity > limits.get(line.sku, 0):",
                buggy="if line.sku in limits and line.quantity > limits[line.sku]:",
            ),
        ),
        hidden=hidden(
            "fits_unlisted",
            """
from inventory.shipping import fits
from inventory.stock import Line


def test_unlisted_sku_does_not_fit() -> None:
    assert fits([Line("Z-9", 1)], {"A-1": 5}) is False


def test_listed_sku_still_checked() -> None:
    assert fits([Line("A-1", 6)], {"A-1": 5}) is False
""",
        ),
    ),
    # ====================================================== missing validation (3)
    TaskDef(
        task_id="budget-ledger-add-accepts-nonpositive",
        host="budget",
        category=BenchmarkCategory.MISSING_VALIDATION,
        difficulty="medium",
        issue=(
            "`Ledger.add` accepts zero and negative amounts.\n\n"
            "Refunds already have their own entry point (`Ledger.refund`), so a "
            "negative amount pushed through `add` produces a ledger whose totals "
            "look plausible while the individual entries are meaningless. The "
            "library defines `InvalidAmount` for exactly this case and the "
            "docstring already promises the rule, but nothing enforces it.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/ledger.py",
                fixed=(
                    "        if amount <= 0:\n"
                    '            raise InvalidAmount(f"amount must be positive, got {amount}")\n'
                    "        entry = Entry(amount=amount, note=note, category=categorise(amount, note))"
                ),
                buggy="        entry = Entry(amount=amount, note=note, category=categorise(amount, note))",
            ),
        ),
        hidden=hidden(
            "add_validation",
            """
import pytest

from budget.ledger import InvalidAmount, Ledger


def test_add_rejects_zero() -> None:
    with pytest.raises(InvalidAmount):
        Ledger().add(0.0, "Coffee")


def test_add_rejects_negative() -> None:
    with pytest.raises(InvalidAmount):
        Ledger().add(-5.0, "Coffee")
""",
        ),
    ),
    TaskDef(
        task_id="budget-ledger-refund-accepts-nonpositive",
        host="budget",
        category=BenchmarkCategory.MISSING_VALIDATION,
        difficulty="medium",
        issue=(
            "`Ledger.refund` accepts a zero or negative refund amount.\n\n"
            "A negative refund silently records an extra *charge*, which is the "
            "opposite of what the caller asked for. The ledger already raises "
            "`InvalidAmount` for bad amounts on the `add` side; the refund path "
            "needs the same guard.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/ledger.py",
                fixed=(
                    "        if amount <= 0:\n"
                    '            raise InvalidAmount(f"refund amount must be positive, got {amount}")\n'
                    "        original = self.entries[index]"
                ),
                buggy="        original = self.entries[index]",
            ),
        ),
        hidden=hidden(
            "refund_validation",
            """
import pytest

from budget.ledger import InvalidAmount, Ledger


def _ledger() -> Ledger:
    ledger = Ledger()
    ledger.add(50.0, "Lunch")
    return ledger


def test_refund_rejects_zero() -> None:
    with pytest.raises(InvalidAmount):
        _ledger().refund(0, 0.0)


def test_refund_rejects_negative() -> None:
    with pytest.raises(InvalidAmount):
        _ledger().refund(0, -10.0)
""",
        ),
    ),
    TaskDef(
        task_id="taskqueue-retrypolicy-accepts-zero-attempts",
        host="taskqueue",
        category=BenchmarkCategory.MISSING_VALIDATION,
        difficulty="easy",
        issue=(
            "`RetryPolicy(attempts=0)` is accepted.\n\n"
            "A policy that never calls the function is not a retry policy - it is a "
            "way to silently skip work. Constructing it should fail immediately "
            "rather than produce an object whose `run` always reports exhaustion.\n"
        ),
        bugs=(
            Bug(
                file="src/taskqueue/retry.py",
                fixed=(
                    "    def __post_init__(self) -> None:\n"
                    "        if self.attempts < 1:\n"
                    '            raise ValueError("attempts 至少为 1")'
                ),
                buggy="    def __post_init__(self) -> None:\n        return None",
            ),
        ),
        hidden=hidden(
            "policy_validation",
            """
import pytest

from taskqueue.retry import RetryPolicy


def test_zero_attempts_rejected() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)


def test_negative_attempts_rejected() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(attempts=-3)
""",
        ),
    ),
    # ======================================================== wrong exception (3)
    TaskDef(
        task_id="textkit-mean-empty-sample-raises-wrong-error",
        host="textkit",
        category=BenchmarkCategory.WRONG_EXCEPTION,
        difficulty="medium",
        issue=(
            "`mean([])` blows up with a `ZeroDivisionError`.\n\n"
            "The package defines `EmptySample` for exactly this situation, and "
            "callers catch that type to decide whether to show `n/a`. Letting the "
            "builtin division error escape means every caller either crashes or "
            "has to catch `Exception`.\n"
        ),
        bugs=(
            Bug(
                file="src/textkit/stats.py",
                fixed=(
                    "    if not values:\n"
                    '        raise EmptySample("mean of an empty sample")\n'
                    "    return sum(values) / len(values)"
                ),
                buggy="    return sum(values) / len(values)",
            ),
        ),
        hidden=hidden(
            "mean_empty",
            """
import pytest

from textkit.stats import EmptySample, mean


def test_empty_sample_raises_typed_error() -> None:
    with pytest.raises(EmptySample):
        mean([])


def test_no_division_error_leaks() -> None:
    with pytest.raises(EmptySample):
        mean([])  # 不是 ZeroDivisionError
""",
        ),
    ),
    TaskDef(
        task_id="httpclient-retries-rejects-with-wrong-error",
        host="httpclient",
        category=BenchmarkCategory.WRONG_EXCEPTION,
        difficulty="medium",
        issue=(
            "Rejecting a negative retry count raises a plain `ValueError`.\n\n"
            "Everything this library rejects about *configuration* is a "
            "`ConfigError`, and callers rely on that to tell a bad config apart "
            "from a transport failure. The timeout check already raises the right "
            "type; the retry check does not.\n"
        ),
        bugs=(
            Bug(
                file="src/httpclient/config.py",
                fixed='        raise ConfigError(f"retries must be >= 0, got {value}")',
                buggy='        raise ValueError(f"retries must be >= 0, got {value}")',
            ),
        ),
        hidden=hidden(
            "retries_error_type",
            """
import pytest

from httpclient.config import validate_retries
from httpclient.errors import ConfigError, HttpError


def test_negative_retries_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        validate_retries(-1)


def test_error_is_an_http_error() -> None:
    with pytest.raises(HttpError):
        validate_retries(-2)
""",
        ),
    ),
    TaskDef(
        task_id="inventory-reserve-raises-with-wrong-error",
        host="inventory",
        category=BenchmarkCategory.WRONG_EXCEPTION,
        difficulty="medium",
        issue=(
            "An order that needs more stock than exists fails with `ValueError`.\n\n"
            "The checkout flow catches `OutOfStock` to show a restock prompt, and "
            "lets anything else propagate as a bug. A short shipment therefore "
            "surfaces as an unhandled error instead of the expected stock problem.\n"
        ),
        bugs=(
            Bug(
                file="src/inventory/stock.py",
                fixed='            raise OutOfStock(f"{line.sku}: need {line.quantity}, have {available}")',
                buggy='            raise ValueError(f"{line.sku}: need {line.quantity}, have {available}")',
            ),
        ),
        hidden=hidden(
            "reserve_error_type",
            """
import pytest

from inventory.stock import Line, OutOfStock, Stock


def test_short_stock_raises_out_of_stock() -> None:
    stock = Stock()
    stock.restock("A-1", 1)
    with pytest.raises(OutOfStock):
        stock.reserve(Line("A-1", 4))


def test_message_names_the_sku() -> None:
    stock = Stock()
    with pytest.raises(OutOfStock, match="B-2"):
        stock.reserve(Line("B-2", 1))
""",
        ),
    ),
    # ============================================================== off by one (3)
    TaskDef(
        task_id="textkit-window-start-one-past-page",
        host="textkit",
        category=BenchmarkCategory.OFF_BY_ONE,
        difficulty="easy",
        issue=(
            "Paged exports skip the first row of every page after the first.\n\n"
            "Page numbers start at 0 and a page of size `n` should begin at offset "
            "`n * page`. Today the second page begins one row late, so the row at "
            "the boundary lands on two pages and another is never shown.\n"
        ),
        bugs=(
            Bug(
                file="src/textkit/slices.py",
                fixed="    return page * size",
                buggy="    return page * size + 1",
            ),
        ),
        hidden=hidden(
            "window_alignment",
            """
from textkit.slices import window_start


def test_first_page_starts_at_zero() -> None:
    assert window_start(0, 10) == 0


def test_third_page_of_ten() -> None:
    assert window_start(3, 10) == 30


def test_pages_cover_the_sequence_without_gaps() -> None:
    assert window_start(1, 4) == 4
""",
        ),
    ),
    TaskDef(
        task_id="textkit-tail-eats-one-extra-char",
        host="textkit",
        category=BenchmarkCategory.OFF_BY_ONE,
        difficulty="easy",
        issue=(
            "`tail(text, n)` returns `n + 1` characters.\n\n"
            "The log viewer asks for the last few characters of a line and gets one "
            "more than it asked for, which shows up as a stray character at the "
            "start of every truncated line.\n"
        ),
        bugs=(
            Bug(
                file="src/textkit/slices.py",
                fixed='    return text[len(text) - count :] if count else ""',
                buggy='    return text[len(text) - count - 1 :] if count else ""',
            ),
        ),
        hidden=hidden(
            "tail_length",
            """
from textkit.slices import tail


def test_tail_returns_exactly_requested() -> None:
    assert tail("abcdef", 2) == "ef"


def test_tail_of_whole_string() -> None:
    assert tail("abcdef", 6) == "abcdef"


def test_tail_one() -> None:
    assert tail("abcdef", 1) == "f"
""",
        ),
    ),
    TaskDef(
        task_id="taskqueue-retry-makes-one-fewer-attempt",
        host="taskqueue",
        category=BenchmarkCategory.OFF_BY_ONE,
        difficulty="medium",
        issue=(
            "Work is abandoned before the configured attempts are used.\n"
            "\n"
            "With an allowance of three attempts, a callback that fails twice and would then "
            "succeed never reaches that success. A one-attempt policy also fails without calling "
            "a healthy callback. The allowance includes the initial call; success should stop "
            "further attempts, and exhaustion should be reported only after the allowance is "
            "used.\n"
        ),
        bugs=(
            Bug(
                file="src/taskqueue/retry.py",
                fixed="    for _ in range(policy.attempts):",
                buggy="    for _ in range(policy.attempts - 1):",
            ),
        ),
        hidden=hidden(
            "attempts_count",
            """
from taskqueue.retry import RetryPolicy, run


def test_uses_every_attempt() -> None:
    calls = {"n": 0}

    def succeeds_last() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("flaky")
        return "ok"

    assert run(succeeds_last, RetryPolicy(attempts=3)) == "ok"
    assert calls["n"] == 3


def test_single_attempt_is_enough_for_a_good_function() -> None:
    assert run(lambda: "ok", RetryPolicy(attempts=1)) == "ok"
""",
        ),
    ),
    # ========================================================= incorrect return (3)
    TaskDef(
        task_id="budget-refund-records-a-charge",
        host="budget",
        category=BenchmarkCategory.INCORRECT_RETURN,
        difficulty="medium",
        issue=(
            "Recording a refund increases spending instead of reducing it.\n"
            "\n"
            "After a lunch expense of 50 and a refund of 10 against that expense, the ledger "
            "reports 60 spent; it should report 40. The original expense must remain in the "
            "history, and the refund must remain associated with the same category.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/ledger.py",
                fixed='        entry = Entry(amount=-amount, note=f"refund of {original.note}", category=original.category)',
                buggy='        entry = Entry(amount=amount, note=f"refund of {original.note}", category=original.category)',
            ),
        ),
        hidden=hidden(
            "refund_sign",
            """
from budget.ledger import Ledger


def test_refund_reduces_total() -> None:
    ledger = Ledger()
    ledger.add(50.0, "Lunch")
    ledger.refund(0, 10.0)
    assert ledger.total() == 40.0


def test_refund_entry_is_negative() -> None:
    ledger = Ledger()
    ledger.add(50.0, "Lunch")
    entry = ledger.refund(0, 10.0)
    assert entry.amount == -10.0
""",
        ),
    ),
    TaskDef(
        task_id="taskqueue-pop-takes-from-the-wrong-end",
        host="taskqueue",
        category=BenchmarkCategory.INCORRECT_RETURN,
        difficulty="medium",
        issue=(
            "The task queue serves tasks in the wrong order.\n\n"
            "`TaskQueue` is documented and named as FIFO, and `push` appends. Under "
            "load a task that has been waiting longest can still be served last, "
            "which is what the on-call escalation rules were written to avoid.\n"
        ),
        bugs=(
            Bug(
                file="src/taskqueue/queue.py",
                fixed="        task = self._items.popleft()",
                buggy="        task = self._items.pop()",
            ),
        ),
        hidden=hidden(
            "fifo_order",
            """
from taskqueue.queue import Task, TaskQueue


def test_first_pushed_is_first_popped() -> None:
    queue = TaskQueue()
    queue.push(Task("old"))
    queue.push(Task("new"))
    popped = queue.pop()
    assert popped is not None
    assert popped.name == "old"


def test_full_order() -> None:
    queue = TaskQueue([Task("a"), Task("b"), Task("c")])
    names = []
    while (task := queue.pop()) is not None:
        names.append(task.name)
    assert names == ["a", "b", "c"]
""",
        ),
    ),
    TaskDef(
        task_id="textkit-spread-inverted",
        host="textkit",
        category=BenchmarkCategory.INCORRECT_RETURN,
        difficulty="easy",
        issue=(
            "`spread` reports negative ranges.\n\n"
            "The chart scale uses `spread` to size its axis, and a negative axis "
            "range makes the whole plot collapse. The docstring defines spread as "
            "the largest value minus the smallest.\n"
        ),
        bugs=(
            Bug(
                file="src/textkit/stats.py",
                fixed="    return max(values) - min(values)",
                buggy="    return min(values) - max(values)",
            ),
        ),
        hidden=hidden(
            "spread_direction",
            """
from textkit.stats import spread


def test_spread_is_non_negative() -> None:
    assert spread([4.0, 1.0, 9.0]) == 8.0


def test_spread_of_equal_values_is_zero() -> None:
    assert spread([2.0, 2.0]) == 0.0
""",
        ),
    ),
    # ============================================================= api misuse (3)
    TaskDef(
        task_id="budget-render-row-ignores-column-order",
        host="budget",
        category=BenchmarkCategory.API_MISUSE,
        difficulty="medium",
        issue=(
            "Exported CSV values appear under the wrong headings.\n"
            "\n"
            "A record containing amount 12, category food, currency CNY and note Coffee produces "
            "a different row when its fields are supplied in a different order. The exported "
            "values must stay aligned with their headings regardless of the order in which the "
            "caller assembled the record.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/csv_writer.py",
                fixed='    return ",".join(str(values[column]) for column in COLUMNS)',
                buggy='    return ",".join(str(value) for value in values.values())',
            ),
        ),
        hidden=hidden(
            "column_order",
            """
from budget.csv_writer import COLUMNS, render_row


def test_row_follows_column_order_not_dict_order() -> None:
    record = {"amount": 12, "category": "food", "currency": "CNY", "note": "Coffee"}
    assert render_row(record) == ",".join(str(record[name]) for name in COLUMNS)


def test_row_has_one_field_per_column() -> None:
    record = {"amount": 1, "category": "c", "currency": "X", "note": "n"}
    assert len(render_row(record).split(",")) == len(COLUMNS)
""",
        ),
    ),
    TaskDef(
        task_id="inventory-describe-crashes-on-unknown-sku",
        host="inventory",
        category=BenchmarkCategory.API_MISUSE,
        difficulty="easy",
        issue=(
            "Describing an unlisted SKU raises KeyError.\n"
            "\n"
            "The stock report includes products that have not yet been added to the catalog. For "
            'example, an unlisted Q should be displayed as "Q - unknown" rather than aborting the '
            "report. This also applies when a caller supplies its own catalog; listed products "
            "should retain their names.\n"
        ),
        bugs=(
            Bug(
                file="src/inventory/catalog.py",
                fixed="    name = table.get(sku, UNKNOWN_NAME)",
                buggy="    name = table[sku]",
            ),
        ),
        hidden=hidden(
            "describe_unknown",
            """
from inventory.catalog import UNKNOWN_NAME, describe


def test_unknown_sku_gets_placeholder() -> None:
    assert describe("NOPE") == f"NOPE - {UNKNOWN_NAME}"


def test_custom_catalog_missing_key() -> None:
    assert describe("Q", catalog={"A": "x"}) == "Q - unknown"
""",
        ),
    ),
    TaskDef(
        task_id="inventory-total-weight-crashes-on-unpriced-sku",
        host="inventory",
        category=BenchmarkCategory.API_MISUSE,
        difficulty="easy",
        issue=(
            "`total_weight` raises `KeyError` for a product with no listed unit "
            "weight.\n\n"
            "The function already treats a missing price table entry as zero weight "
            "in intent - an item we have no weight for simply contributes nothing - "
            "but the lookup itself blows up before that can happen.\n"
        ),
        bugs=(
            Bug(
                file="src/inventory/shipping.py",
                fixed="    return round(sum(line.quantity * per_unit.get(line.sku, 0.0) for line in lines), 3)",
                buggy="    return round(sum(line.quantity * per_unit[line.sku] for line in lines), 3)",
            ),
        ),
        hidden=hidden(
            "weight_missing_entry",
            """
from inventory.shipping import total_weight
from inventory.stock import Line


def test_unlisted_sku_counts_as_zero() -> None:
    assert total_weight([Line("Z-9", 3)], {"A-1": 1.0}) == 0.0


def test_mixed_known_and_unknown() -> None:
    lines = [Line("A-1", 2), Line("Z-9", 5)]
    assert total_weight(lines, {"A-1": 1.5}) == 3.0
""",
        ),
    ),
    # =============================================================== edge case (4)
    TaskDef(
        task_id="budget-category-totals-accumulate-float-error",
        host="budget_precision",
        category=BenchmarkCategory.EDGE_CASE,
        difficulty="medium",
        issue=(
            "Category totals contain numerical noise and must follow the reporting precision "
            "contract.\n"
            "\n"
            "Three food expenses of 0.1 currently produce 0.30000000000000004 instead of 0.3. For "
            "finite amounts, the decimal representation of each input is the accounting value. "
            "Sub-cent amounts and refunds are allowed. Each category reports its exact aggregate "
            "rounded once to two decimal places, with half-cent ties rounded to the nearest even "
            "cent. Results must be independent of entry order and returned as floats, with "
            "category keys sorted. Categories are rounded independently: their rounded totals "
            "need not sum to the separately rounded whole-ledger total. An empty ledger returns "
            "an empty mapping.\n"
        ),
        bugs=(Bug(file="src/budget/ledger.py", fixed=PRECISION_GOLD, buggy=PRECISION_BASE),),
        hidden=hidden(
            "category_rounding",
            """
from itertools import permutations

import pytest

from budget.ledger import Entry, Ledger


@pytest.mark.parametrize("amounts, expected", [
    ([0.1, 0.1, 0.1], 0.3),
    ([0.004, 0.004, 0.004], 0.01),
    ([0.005, 0.005], 0.01),
    ([0.005], 0.0),
    ([0.015], 0.02),
    ([0.025], 0.02),
    ([-0.005], 0.0),
    ([-0.015], -0.02),
    ([-0.025], -0.02),
    ([0.0049], 0.0),
    ([0.0051], 0.01),
    ([2.675], 2.68),
])
def test_decimal_reporting(amounts, expected) -> None:
    ledger = Ledger([Entry(amount, "item", "food") for amount in amounts])
    result = ledger.by_category()
    assert result == {"food": expected}
    assert isinstance(result["food"], float)


@pytest.mark.parametrize("amounts, expected", [
    ([0.015, 0.015, -0.01], 0.02),
    ([1e16, 0.01, -1e16], 0.01),
])
def test_order_does_not_change_totals(amounts, expected) -> None:
    for ordered in permutations(amounts):
        ledger = Ledger([Entry(value, "item", "food") for value in ordered])
        assert ledger.by_category() == {"food": expected}


def test_categories_are_independent_and_sorted() -> None:
    ledger = Ledger([
        Entry(0.005, "item", "z"), Entry(0.005, "item", "a"),
        Entry(0.015, "item", "m"),
    ])
    before = list(ledger.entries)
    assert ledger.by_category() == {"a": 0.0, "m": 0.02, "z": 0.0}
    assert list(ledger.by_category()) == ["a", "m", "z"]
    assert ledger.entries == before


def test_refund_participates_before_rounding() -> None:
    ledger = Ledger()
    ledger.add(0.025, "Coffee")
    ledger.refund(0, 0.01)
    assert ledger.by_category() == {"food": 0.02}


def test_empty_and_cancelling_categories() -> None:
    assert Ledger().by_category() == {}
    ledger = Ledger([Entry(0.015, "item", "food"), Entry(-0.015, "refund", "food")])
    assert ledger.by_category() == {"food": 0.0}
""",
        ),
    ),
    TaskDef(
        task_id="textkit-title-crashes-on-empty-string",
        host="textkit",
        category=BenchmarkCategory.EDGE_CASE,
        difficulty="easy",
        issue=(
            "`title('')` raises `IndexError`.\n\n"
            "Blank cells are normal in imported spreadsheets, and the label "
            "generator walks a column of them. The function is meant to leave an "
            "empty value alone.\n"
        ),
        bugs=(
            Bug(
                file="src/textkit/normalise.py",
                fixed="    return value[:1].upper() + value[1:] if value else value",
                buggy="    return value[0].upper() + value[1:]",
            ),
        ),
        hidden=hidden(
            "title_empty",
            """
from textkit.normalise import title


def test_empty_string_is_left_alone() -> None:
    assert title("") == ""


def test_single_character() -> None:
    assert title("a") == "A"
""",
        ),
    ),
    TaskDef(
        task_id="inventory-box-count-loses-the-last-partial-box",
        host="inventory",
        category=BenchmarkCategory.EDGE_CASE,
        difficulty="medium",
        issue=(
            "Some shipments are short by one box.\n\n"
            "`box_count` divides the total weight by the box capacity and reports "
            "that as the number of boxes. It works when the weight fills the boxes "
            "exactly and quietly loses the remainder otherwise - the leftover "
            "fraction of a shipment is simply not shipped.\n"
        ),
        bugs=(
            Bug(
                file="src/inventory/shipping.py",
                fixed="    return math.ceil(weight_kg / capacity_kg)",
                buggy="    return int(weight_kg / capacity_kg)",
            ),
        ),
        hidden=hidden(
            "box_ceil",
            """
import pytest

from inventory.shipping import box_count


def test_partial_box_is_counted() -> None:
    assert box_count(10.0, 4.0) == 3


def test_smallest_nonzero_load_needs_one_box() -> None:
    assert box_count(0.5, 5.0) == 1


def test_exact_fill_unchanged() -> None:
    assert box_count(10.0, 5.0) == 2


def test_zero_load_needs_no_box() -> None:
    assert box_count(0.0, 5.0) == 0


def test_capacity_still_validated() -> None:
    with pytest.raises(ValueError):
        box_count(1.0, -1.0)
""",
        ),
    ),
    TaskDef(
        task_id="httpclient-zero-retries-makes-no-request",
        host="httpclient",
        category=BenchmarkCategory.EDGE_CASE,
        difficulty="medium",
        issue=(
            "Configuring `retries=0` makes the client send no request at all.\n\n"
            "`retries` counts *extra* attempts after the first one, so zero retries "
            "should still mean one honest attempt at the request. Today a "
            "zero-retry client raises before ever touching the network.\n"
        ),
        bugs=(
            Bug(
                file="src/httpclient/client.py",
                fixed="        attempts = self.config.retries + 1",
                buggy="        attempts = self.config.retries",
            ),
        ),
        hidden=hidden(
            "zero_retry_attempt",
            """
from httpclient.client import Client
from httpclient.config import Config
from httpclient.transport import build_sender


def test_zero_retries_still_asks_once() -> None:
    sender = build_sender(responses=["ok"])
    client = Client(Config(base_url="https://example.invalid/", retries=0), sender=sender)
    assert client.request_with_retries("users") == "ok"
    assert len(sender.calls) == 1


def test_two_retries_means_three_attempts() -> None:
    sender = build_sender(responses=["a", "b", "c"])
    client = Client(Config(base_url="https://example.invalid/", retries=2), sender=sender)
    client.request_with_retries("x")
    client.request_with_retries("y")
    client.request_with_retries("z")
    assert len(sender.calls) == 3
""",
        ),
    ),
    # ================================================= cross-module dependency (5)
    TaskDef(
        task_id="budget-export-adds-currency-column",
        host="budget_currency",
        category=BenchmarkCategory.CROSS_MODULE,
        difficulty="hard",
        issue=(
            "CSV exports are rejected because the currency column is missing.\n"
            "\n"
            "The import format requires category, note, currency, amount in that order, including "
            "the header for an empty ledger. Each ledger has a currency attribute: omitted "
            "currency defaults to CNY for compatibility, while a caller can supply another "
            "currency such as USD or EUR. Every exported row must use that ledger currency "
            "without converting its amount. Exports from different ledgers must retain their own "
            "currencies, including when produced consecutively.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/csv_writer.py",
                fixed='COLUMNS = ("category", "note", "currency", "amount")',
                buggy='COLUMNS = ("category", "note", "amount")',
            ),
            Bug(
                file="src/budget/report.py",
                fixed='                "currency": ledger.currency,\n',
                buggy="",
            ),
        ),
        hidden=hidden(
            "currency_column",
            """
import csv
import io

import pytest

from budget.ledger import Ledger
from budget.report import build_rows, render


@pytest.mark.parametrize("currency", ["CNY", "USD", "EUR"])
def test_export_uses_ledger_currency(currency) -> None:
    ledger = Ledger(currency=currency)
    ledger.add(20.0, "Coffee")
    ledger.add(7.5, "Lunch")
    rows = build_rows(ledger)
    assert [row["currency"] for row in rows] == [currency, currency]
    assert [row["amount"] for row in rows] == [20.0, 7.5]
    reader = csv.DictReader(io.StringIO(render(ledger)))
    assert reader.fieldnames == ["category", "note", "currency", "amount"]
    assert list(reader) == [
        {"category": "food", "note": "Coffee", "currency": currency, "amount": "20.0"},
        {"category": "food", "note": "Lunch", "currency": currency, "amount": "7.5"},
    ]


def test_default_currency_is_cny() -> None:
    ledger = Ledger()
    ledger.add(2.0, "Coffee")
    assert build_rows(ledger)[0]["currency"] == "CNY"


def test_sequential_exports_do_not_share_currency() -> None:
    usd, eur = Ledger(currency="USD"), Ledger(currency="EUR")
    usd.add(1.0, "Coffee")
    eur.add(1.0, "Coffee")
    for ledger, currency in [(usd, "USD"), (eur, "EUR"), (usd, "USD")]:
        assert list(csv.DictReader(io.StringIO(render(ledger))))[0]["currency"] == currency
        assert ledger.currency == currency
        assert ledger.entries[0].amount == 1.0


def test_empty_export_keeps_schema() -> None:
    assert render(Ledger(currency="EUR")) == "category,note,currency,amount\\n"
""",
        ),
    ),
    TaskDef(
        task_id="httpclient-retries-never-reach-the-transport",
        host="httpclient",
        category=BenchmarkCategory.CROSS_MODULE,
        difficulty="hard",
        issue=(
            "The transport layer never sees the configured retry budget.\n\n"
            "Some transports need to know how many attempts remain so they can pick "
            "a connection strategy that degrades gracefully. The sender protocol "
            "takes the retry count as its third argument, but nothing along the "
            "path actually passes it, so calls fail with a missing-argument error "
            "as soon as a real sender is plugged in.\n"
        ),
        bugs=(
            Bug(
                file="src/httpclient/transport.py",
                fixed=(
                    "        self.calls: list[tuple[str, float, int]] = []\n"
                    "\n"
                    "    def __call__(self, url: str, timeout: float, retries: int) -> str:\n"
                    "        self.calls.append((url, timeout, retries))"
                ),
                buggy=(
                    "        self.calls: list[tuple[str, float]] = []\n"
                    "\n"
                    "    def __call__(self, url: str, timeout: float) -> str:\n"
                    "        self.calls.append((url, timeout))"
                ),
            ),
            Bug(
                file="src/httpclient/transport.py",
                fixed="def build_sender(*, responses: list[str]) -> Callable[[str, float, int], str]:",
                buggy="def build_sender(*, responses: list[str]) -> Callable[[str, float], str]:",
            ),
            Bug(
                file="src/httpclient/client.py",
                fixed="        return self.sender(url, self.config.timeout, self.config.retries)",
                buggy="        return self.sender(url, self.config.timeout)",
            ),
        ),
        hidden=hidden(
            "retries_reach_transport",
            """
from httpclient.client import Client, new_client
from httpclient.config import Config
from httpclient.transport import build_sender


def test_sender_receives_three_arguments() -> None:
    sender = build_sender(responses=["ok"])
    client = Client(Config(base_url="https://example.invalid/", retries=4), sender=sender)
    client.request("users")
    assert len(sender.calls[0]) == 3


def test_configured_retries_are_passed_through() -> None:
    sender = build_sender(responses=["ok"])
    client = Client(Config(base_url="https://example.invalid/", retries=4), sender=sender)
    client.request("users")
    assert sender.calls[0][2] == 4


def test_helper_constructor_also_works() -> None:
    client = new_client(base_url="https://example.invalid/", responses=["ok"], retries=2)
    assert client.request("users") == "ok"
""",
        ),
    ),
    TaskDef(
        task_id="textkit-page-duplicates-the-offset-rule",
        host="textkit",
        category=BenchmarkCategory.CROSS_MODULE,
        difficulty="hard",
        issue=(
            "Paged reads return the wrong slice for any page after the first.\n\n"
            "`window_start` is the single place that turns a page number into an "
            "offset, and `textkit.paging.page` is supposed to use it. The page "
            "helper instead re-derives the offset where it needs it, so the two "
            "copies of the rule have drifted and no page boundary lines up any more.\n"
        ),
        bugs=(
            Bug(
                file="src/textkit/slices.py",
                fixed="    return page * size",
                buggy="    return page * size + 1",
            ),
            Bug(
                file="src/textkit/paging.py",
                fixed="    start = window_start(number, size)\n    return values[start : start + size]",
                buggy="    start = number * size + 1\n    return values[start : start + size]",
            ),
        ),
        hidden=hidden(
            "page_consistency",
            """
from textkit.paging import page
from textkit.slices import window_start

VALUES = list(range(10))


def test_second_page_follows_window_start() -> None:
    assert page(VALUES, 1, 3) == VALUES[window_start(1, 3) : window_start(1, 3) + 3]


def test_each_page_boundary() -> None:
    assert page(VALUES, 0, 3) == [0, 1, 2]
    assert page(VALUES, 1, 3) == [3, 4, 5]


def test_last_partial_page() -> None:
    assert page(VALUES, 3, 3) == [9]


def test_page_and_window_never_disagree() -> None:
    for number in range(4):
        assert page(VALUES, number, 3)[:1] == VALUES[window_start(number, 3) :][:1]
""",
        ),
    ),
    TaskDef(
        task_id="inventory-can-ship-reimplement-the-limits-rule",
        host="inventory",
        category=BenchmarkCategory.CROSS_MODULE,
        difficulty="hard",
        issue=(
            "Whole-order shipping decisions disagree with the per-line check.\n\n"
            '`fits` is the one definition of "this order is within the per-SKU '
            'limits", and `can_ship` is documented to build on it. `can_ship` '
            "instead compares quantities against its own copy of the rule, so an "
            "order that `fits` rejects can still be approved by `can_ship`.\n"
        ),
        bugs=(
            Bug(
                file="src/inventory/shipping.py",
                fixed="if line.quantity > limits.get(line.sku, 0):",
                buggy="if line.sku in limits and line.quantity > limits[line.sku]:",
            ),
            Bug(
                file="src/inventory/shipping.py",
                fixed=(
                    "    if not fits(order, limits):\n"
                    "        return False\n"
                    "    return box_count(weight_kg, capacity_kg) <= max_boxes"
                ),
                buggy=(
                    "    if any(line.quantity > limits.get(line.sku, 10**9) for line in order):\n"
                    "        return False\n"
                    "    return box_count(weight_kg, capacity_kg) <= max_boxes"
                ),
            ),
        ),
        hidden=hidden(
            "can_ship_follows_fits",
            """
from inventory.shipping import can_ship, fits
from inventory.stock import Line


def _order() -> list[Line]:
    return [Line("Z-9", 1)]


def test_unlisted_sku_rejected_by_both() -> None:
    assert fits(_order(), {"A-1": 5}) is False
    assert can_ship(_order(), limits={"A-1": 5}, weight_kg=1.0, capacity_kg=5.0, max_boxes=1) is False


def test_can_ship_agrees_with_fits_for_listed_orders() -> None:
    order = [Line("A-1", 2)]
    limits = {"A-1": 5}
    assert fits(order, limits) is True
    assert can_ship(order, limits=limits, weight_kg=1.0, capacity_kg=5.0, max_boxes=1) is True


def test_box_limit_still_applies() -> None:
    order = [Line("A-1", 1)]
    assert can_ship(order, limits={"A-1": 5}, weight_kg=9.0, capacity_kg=4.0, max_boxes=1) is False
""",
        ),
    ),
    TaskDef(
        task_id="httpclient-config-snapshot-loses-retries",
        host="httpclient",
        category=BenchmarkCategory.CROSS_MODULE,
        difficulty="hard",
        issue=(
            "Client settings are lost and the diagnostic snapshot is incomplete.\n"
            "\n"
            "Creating a client through the convenience API with retries=5 leaves its retry budget "
            "at the default 2. Diagnostic snapshots omit the retry count, including for a "
            "configuration created directly. Callers expect the supplied timeout and retry "
            "settings to take effect and snapshots to report base URL, timeout and retries "
            "accurately.\n"
        ),
        bugs=(
            Bug(
                file="src/httpclient/config.py",
                fixed='        return {"base_url": self.base_url, "timeout": self.timeout, "retries": self.retries}',
                buggy='        return {"base_url": self.base_url, "timeout": self.timeout}',
            ),
            Bug(
                file="src/httpclient/client.py",
                fixed="    config = Config(base_url=base_url, **kwargs)  # type: ignore[arg-type]",
                buggy="    config = Config(base_url=base_url)",
            ),
        ),
        hidden=hidden(
            "config_snapshot",
            """
from httpclient.client import new_client
from httpclient.config import Config


def test_as_dict_reports_every_field() -> None:
    snapshot = Config(base_url="https://example.invalid/", timeout=1.0, retries=5).as_dict()
    assert snapshot == {
        "base_url": "https://example.invalid/",
        "timeout": 1.0,
        "retries": 5,
    }


def test_helper_forwards_keyword_arguments() -> None:
    client = new_client(base_url="https://example.invalid/", responses=["ok"], retries=5)
    assert client.config.retries == 5


def test_helper_and_snapshot_agree() -> None:
    client = new_client(base_url="https://example.invalid/", responses=["ok"], retries=3)
    assert client.config.as_dict()["retries"] == 3
""",
        ),
    ),
    # =========================================================== regression bug (3)
    TaskDef(
        task_id="httpclient-config-stops-checking-timeout",
        host="httpclient",
        category=BenchmarkCategory.REGRESSION,
        difficulty="medium",
        issue=(
            "Zero and negative timeouts are accepted again.\n"
            "\n"
            "Constructing a configuration with timeout=0 or timeout=-1 succeeds. A timeout must "
            "be strictly positive, and invalid values should raise the library configuration "
            "error at construction time so callers can report a bad setting before making a "
            "request. Positive timeouts should remain usable.\n"
        ),
        bugs=(
            Bug(
                file="src/httpclient/config.py",
                fixed="        self.timeout = validate_timeout(timeout)",
                buggy="        self.timeout = timeout",
            ),
        ),
        hidden=hidden(
            "timeout_wiring",
            """
import pytest

from httpclient.config import Config
from httpclient.errors import ConfigError


def test_zero_timeout_rejected_by_constructor() -> None:
    with pytest.raises(ConfigError):
        Config(base_url="https://example.invalid/", timeout=0.0)


def test_negative_timeout_rejected() -> None:
    with pytest.raises(ConfigError):
        Config(base_url="https://example.invalid/", timeout=-1.0)


def test_valid_timeout_still_accepted() -> None:
    assert Config(base_url="https://example.invalid/", timeout=0.5).timeout == 0.5
""",
        ),
    ),
    TaskDef(
        task_id="budget-categorise-loses-the-rent-branch",
        host="budget",
        category=BenchmarkCategory.REGRESSION,
        difficulty="medium",
        issue=(
            "Rent payments are no longer classified as housing.\n"
            "\n"
            'An expense of 900 with note "Rent April" appears as large, although rent belongs to '
            "housing regardless of its amount. Insurance expenses still appear as housing. Notes "
            "are matched without regard to letter case, and unrelated expenses should retain "
            "their existing categories.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/rules.py",
                fixed='if "rent" in lowered or "insurance" in lowered:',
                buggy='if "insurance" in lowered:',
            ),
        ),
        hidden=hidden(
            "rent_branch",
            """
from budget.rules import categorise


def test_rent_is_housing() -> None:
    assert categorise(900.0, "Rent April") == "housing"


def test_both_keywords_still_housing() -> None:
    assert categorise(10.0, "rent and insurance") == "housing"


def test_large_rent_does_not_fall_through() -> None:
    assert categorise(1500.0, "Rent") == "housing"
""",
        ),
    ),
    TaskDef(
        task_id="taskqueue-store-stops-reporting-missing-keys",
        host="taskqueue",
        category=BenchmarkCategory.REGRESSION,
        difficulty="medium",
        issue=(
            "Reading a key that was never stored returns a stale value or reports "
            "nothing useful.\n\n"
            "`MissingKey` is part of this library's interface and callers depend on "
            'it to distinguish "empty" from "absent". The check that raised it '
            "used to be there; today the read path goes straight to the value.\n"
        ),
        bugs=(
            Bug(
                file="src/taskqueue/store.py",
                fixed=(
                    "        if key not in self._data:\n"
                    "            raise MissingKey(key)\n"
                    "        rendered = self._data[key].upper()"
                ),
                buggy="        rendered = self._data[key].upper()",
            ),
        ),
        hidden=hidden(
            "store_missing_key",
            """
import pytest

from taskqueue.store import MissingKey, Store


def test_get_unknown_key_raises() -> None:
    with pytest.raises(MissingKey):
        Store().get("nope")


def test_deleted_key_raises_again() -> None:
    store = Store()
    store.set("a", "x")
    store.delete("a")
    with pytest.raises(MissingKey):
        store.get("a")


def test_existing_key_still_readable() -> None:
    store = Store()
    store.set("a", "x")
    assert store.get("a") == "X"
""",
        ),
    ),
    # ======================================================= state / mutation (4)
    TaskDef(
        task_id="budget-snapshot-shares-the-live-list",
        host="budget",
        category=BenchmarkCategory.STATE_MUTATION,
        difficulty="medium",
        issue=(
            "A ledger snapshot changes when the ledger keeps growing.\n\n"
            "`snapshot` exists so a background job can hold onto the state it read. "
            "Callers that sort or trim their copy are seeing those edits in the "
            "ledger itself, and the exported totals then disagree with the live "
            "ones.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/ledger.py",
                fixed="        return list(self.entries)",
                buggy="        return self.entries",
            ),
        ),
        hidden=hidden(
            "snapshot_isolation",
            """
from budget.ledger import Ledger


def _ledger() -> Ledger:
    ledger = Ledger()
    ledger.add(1.0, "Coffee")
    return ledger


def test_snapshot_is_not_the_internal_list() -> None:
    ledger = _ledger()
    snapshot = ledger.snapshot()
    snapshot.clear()
    assert len(ledger.entries) == 1


def test_later_entries_are_not_visible_in_an_older_snapshot() -> None:
    ledger = _ledger()
    snapshot = ledger.snapshot()
    ledger.add(2.0, "Lunch")
    assert len(snapshot) == 1
    assert len(ledger.entries) == 2
""",
        ),
    ),
    TaskDef(
        task_id="httpclient-recorder-aliases-the-caller-list",
        host="httpclient",
        category=BenchmarkCategory.STATE_MUTATION,
        difficulty="medium",
        issue=(
            "A shared response script runs out too early.\n\n"
            "Tests reuse one list of canned responses across several recorders. Each "
            "recorder is supposed to work from its own copy, because it pops from "
            "the list as it serves responses - sharing the caller's list means one "
            "recorder drains the script the next one was going to use.\n"
        ),
        bugs=(
            Bug(
                file="src/httpclient/transport.py",
                fixed="        self.responses = list(responses)",
                buggy="        self.responses = responses",
            ),
        ),
        hidden=hidden(
            "recorder_copy",
            """
from httpclient.transport import Recorder, build_sender


def test_recorder_does_not_drain_the_callers_list() -> None:
    responses = ["a", "b"]
    recorder = Recorder(responses)
    recorder("u1", 1.0, 0)
    assert responses == ["a", "b"]


def test_two_recorders_from_one_script() -> None:
    responses = ["a", "b"]
    first = build_sender(responses=responses)
    second = build_sender(responses=responses)
    assert first("u", 1.0, 0) == "a"
    assert second("u", 1.0, 0) == "a"
""",
        ),
    ),
    TaskDef(
        task_id="taskqueue-store-leaves-stale-render-on-update",
        host="taskqueue",
        category=BenchmarkCategory.STATE_MUTATION,
        difficulty="hard",
        issue=(
            "Overwriting a key does not change what reading it returns.\n\n"
            "`get` caches the rendered value. The write path updates the data but "
            "leaves the rendered copy behind, so a key that has been written twice "
            "keeps reporting its first value forever. `delete` already clears both "
            "maps; the write path needs the same treatment.\n"
        ),
        bugs=(
            Bug(
                file="src/taskqueue/store.py",
                fixed=(
                    "    def set(self, key: str, value: str) -> None:\n"
                    '        """写入。缓存里这一条必须一起清掉。"""\n'
                    "        self._data[key] = value\n"
                    "        self._rendered.pop(key, None)"
                ),
                buggy=(
                    "    def set(self, key: str, value: str) -> None:\n"
                    '        """写入。缓存里这一条必须一起清掉。"""\n'
                    "        self._data[key] = value"
                ),
            ),
        ),
        hidden=hidden(
            "cache_invalidation",
            """
from taskqueue.store import Store


def test_overwrite_invalidates_the_render() -> None:
    store = Store()
    store.set("a", "one")
    assert store.get("a") == "ONE"
    store.set("a", "two")
    assert store.get("a") == "TWO"


def test_render_cache_is_not_shared_between_keys() -> None:
    store = Store()
    store.set("a", "x")
    store.get("a")
    store.set("b", "y")
    assert store.get("b") == "Y"
    assert store.get("a") == "X"
""",
        ),
    ),
    TaskDef(
        task_id="taskqueue-drain-leaves-tasks-queued",
        host="taskqueue",
        category=BenchmarkCategory.STATE_MUTATION,
        difficulty="medium",
        issue=(
            "`drain` hands out the tasks but leaves them in the queue.\n\n"
            "The worker takes a batch with `drain` and then processes it; because "
            "the queue still holds those tasks, the next `drain` hands out the same "
            "work twice. The method is documented as emptying the queue.\n"
        ),
        bugs=(
            Bug(
                file="src/taskqueue/queue.py",
                fixed="        items = list(self._items)\n        self._items.clear()\n        return items",
                buggy="        return list(self._items)",
            ),
        ),
        hidden=hidden(
            "drain_empties",
            """
from taskqueue.queue import Task, TaskQueue


def test_drain_empties_the_queue() -> None:
    queue = TaskQueue([Task("a"), Task("b")])
    assert len(queue.drain()) == 2
    assert len(queue) == 0


def test_second_drain_is_empty() -> None:
    queue = TaskQueue([Task("a")])
    queue.drain()
    assert queue.drain() == []


def test_drained_batch_is_detached() -> None:
    queue = TaskQueue([Task("a")])
    batch = queue.drain()
    queue.push(Task("b"))
    assert [task.name for task in batch] == ["a"]
""",
        ),
    ),
    # ============================================ 额外类别补强（提高多样性） (5)
    TaskDef(
        task_id="budget-member-discount-inverted-condition",
        host="budget",
        category=BenchmarkCategory.WRONG_CONDITION,
        difficulty="easy",
        issue=(
            "Membership prices are incorrect.\n"
            "\n"
            "For a purchase total of 200, a member is charged 200 while a guest is charged 180. "
            "The membership offer is a ten percent reduction; guests pay the original total. "
            "Discounted prices are rounded to two decimal places.\n"
        ),
        bugs=(
            Bug(
                file="src/budget/rules.py",
                fixed="    if member:",
                buggy="    if not member:",
            ),
        ),
        hidden=hidden(
            "discount_branch",
            """
from budget.rules import apply_discount


def test_member_pays_less() -> None:
    assert apply_discount(200.0, member=True) == 180.0


def test_guest_pays_full() -> None:
    assert apply_discount(200.0, member=False) == 200.0
""",
        ),
    ),
    TaskDef(
        task_id="inventory-restock-accepts-zero-shipment",
        host="inventory",
        category=BenchmarkCategory.MISSING_VALIDATION,
        difficulty="easy",
        issue=(
            "`Stock.restock` accepts a non-positive amount.\n\n"
            "A receiving clerk who scans nothing currently creates a bookkeeping "
            "entry that moves stock down or does nothing at all, and the audit trail "
            "cannot tell a mis-scan from an intentional adjustment. Zero and "
            "negative quantities need to be refused.\n"
        ),
        bugs=(
            Bug(
                file="src/inventory/stock.py",
                fixed=(
                    "        if amount <= 0:\n"
                    '            raise InvalidQuantity(f"restock amount must be positive, got {amount}")\n'
                    "        self.quantities[sku] = self.quantities.get(sku, 0) + amount"
                ),
                buggy="        self.quantities[sku] = self.quantities.get(sku, 0) + amount",
            ),
        ),
        hidden=hidden(
            "restock_validation",
            """
import pytest

from inventory.stock import InvalidQuantity, Stock


def test_zero_restock_rejected() -> None:
    with pytest.raises(InvalidQuantity):
        Stock().restock("A-1", 0)


def test_negative_restock_rejected() -> None:
    with pytest.raises(InvalidQuantity):
        Stock().restock("A-1", -2)


def test_positive_restock_still_works() -> None:
    stock = Stock()
    stock.restock("A-1", 4)
    assert stock.available("A-1") == 4
""",
        ),
    ),
    TaskDef(
        task_id="textkit-split-fields-forgets-to-strip",
        host="textkit",
        category=BenchmarkCategory.INCORRECT_RETURN,
        difficulty="easy",
        issue=(
            "Parsed CSV cells keep their surrounding spaces.\n\n"
            "The importer compares cells against fixed option names, and every cell "
            "written as `a , b` fails to match because of the padding. The "
            "docstring says the cells come back stripped.\n"
        ),
        bugs=(
            Bug(
                file="src/textkit/normalise.py",
                fixed="    return [part.strip() for part in line.split(separator, max_split)]",
                buggy="    return line.split(separator, max_split)",
            ),
        ),
        hidden=hidden(
            "split_strip",
            """
from textkit.normalise import split_fields


def test_cells_are_stripped() -> None:
    assert split_fields(" a , b , c ") == ["a", "b", "c"]


def test_tab_padding_is_stripped() -> None:
    assert split_fields("a\\t,b", ",") == ["a", "b"]
""",
        ),
    ),
    TaskDef(
        task_id="taskqueue-priority-orders-by-name",
        host="taskqueue",
        category=BenchmarkCategory.API_MISUSE,
        difficulty="medium",
        issue=(
            "The urgent task is still served last when its name sorts late.\n\n"
            "`order` is meant to sort by the weight that `rank` computes. It "
            "currently hands `sorted` something that makes the comparison depend on "
            "the task name instead, so priority only works by accident when names "
            "happen to line up with urgency.\n"
        ),
        bugs=(
            Bug(
                file="src/taskqueue/priority.py",
                fixed="    return sorted(tasks, key=rank)",
                buggy="    return sorted(tasks, key=lambda task: task.name)",
            ),
        ),
        hidden=hidden(
            "priority_key",
            """
from taskqueue.priority import order
from taskqueue.queue import Task


def test_urgent_wins_over_alphabetical_order() -> None:
    tasks = [Task("a"), Task("z", payload={"level": "urgent"})]
    assert [task.name for task in order(tasks)] == ["z", "a"]


def test_low_goes_last() -> None:
    tasks = [Task("b", payload={"level": "low"}), Task("a", payload={"level": "normal"})]
    assert [task.name for task in order(tasks)] == ["a", "b"]


def test_equal_weights_keep_input_order() -> None:
    tasks = [Task("m"), Task("c"), Task("k")]
    assert [task.name for task in order(tasks)] == ["m", "c", "k"]
""",
        ),
    ),
    TaskDef(
        task_id="textkit-table-keeps-its-own-split-rule",
        host="textkit",
        category=BenchmarkCategory.CROSS_MODULE,
        difficulty="hard",
        issue=(
            "The table reader returns padded cells even though the single-cell "
            "parser does not.\n\n"
            "`textkit.table.parse_row` is documented to delegate to "
            "`split_fields`, which is where the cleaning rules live. It splits on "
            "its own instead, so the two entry points disagree about the same input "
            "and fixing only one of them leaves the other wrong.\n"
        ),
        bugs=(
            Bug(
                file="src/textkit/normalise.py",
                fixed="    return [part.strip() for part in line.split(separator, max_split)]",
                buggy="    return line.split(separator, max_split)",
            ),
            Bug(
                file="src/textkit/table.py",
                fixed="    return split_fields(line)",
                buggy='    return line.split(",")',
            ),
        ),
        hidden=hidden(
            "table_delegates",
            """
from textkit.normalise import split_fields
from textkit.table import parse_row, parse_table

TEXT = " a , b \\n\\n  c,d  \\n"


def test_row_cells_are_stripped() -> None:
    assert parse_row(" a , b ") == ["a", "b"]


def test_row_agrees_with_split_fields() -> None:
    for line in (" a,b ", "x ,y,z", "  p , q"):
        assert parse_row(line) == split_fields(line)


def test_table_skips_blank_lines_and_strips() -> None:
    assert parse_table(TEXT) == [["a", "b"], ["c", "d"]]
""",
        ),
    ),
)

__all__ = ["TASKS", "Bug", "TaskDef", "hidden"]
