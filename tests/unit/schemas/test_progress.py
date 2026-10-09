"""``made_progress`` 的证明（02 §15.4）。

判错这一条的代价很具体：判太松，系统在两个错误之间来回振荡直到烧完预算；
判太严，明明在推进的修复被 no_progress 规则提前掐掉。

注意最后一组用例：**签名变化一律算进展**，包括"变成一个全新的错误"。
这是刻意的——真正的振荡识别需要跨多轮的历史，只有 ``graph.progress``
（Phase 8）才拿得到那个历史，所以这里不做超出单轮信息的判断。
"""

from __future__ import annotations

from codepilot.schemas.progress import ProgressSnapshot, made_progress


def _snap(
    *, failed: int = 3, regressions: int = 0, signatures: set[str] | None = None
) -> ProgressSnapshot:
    return ProgressSnapshot(
        target_failed=failed,
        regression_new_failures=regressions,
        signatures=frozenset(signatures if signatures is not None else {"AssertionError@a.py:1"}),
    )


def test_fewer_target_failures_is_progress() -> None:
    assert made_progress(_snap(failed=3), _snap(failed=2)) is True


def test_fewer_regressions_is_progress() -> None:
    assert made_progress(_snap(regressions=2), _snap(regressions=1)) is True


def test_changed_signature_at_same_count_is_progress() -> None:
    """ImportError 修成 AssertionError 说明修复在推进。"""
    prev = _snap(failed=1, signatures={"ImportError@b.py:5"})
    cur = _snap(failed=1, signatures={"AssertionError@b.py:12"})
    assert made_progress(prev, cur) is True


def test_identical_snapshot_is_no_progress() -> None:
    assert made_progress(_snap(), _snap()) is False


def test_more_failures_is_no_progress() -> None:
    assert made_progress(_snap(failed=1), _snap(failed=3)) is False


def test_more_regressions_is_no_progress() -> None:
    assert made_progress(_snap(regressions=0), _snap(regressions=2)) is False


def test_worse_on_both_axes_is_no_progress() -> None:
    assert made_progress(_snap(failed=1, regressions=0), _snap(failed=4, regressions=3)) is False


def test_fewer_target_failures_wins_even_with_more_regressions() -> None:
    """目标测试推进了就算进展，哪怕引入了一个新回归。

    这个取舍是刻意的：新增回归由 ``regression`` 的独立停止规则处理，
    如果在进度判定里也扣一次分，两个机制会互相掩盖，让人分不清
    "没在推进"和"推进了但引入了回归"。
    """
    assert made_progress(_snap(failed=3, regressions=0), _snap(failed=2, regressions=1)) is True


def test_fewer_regressions_wins_even_with_more_target_failures() -> None:
    assert made_progress(_snap(failed=2, regressions=3), _snap(failed=3, regressions=0)) is True


def test_empty_signature_sets_are_equal() -> None:
    assert made_progress(_snap(signatures=set()), _snap(signatures=set())) is False


def test_signature_order_does_not_matter() -> None:
    """签名集合是无序的，比较不能受插入顺序影响。"""
    prev = _snap(signatures={"A@a.py:1", "B@b.py:2"})
    cur = _snap(signatures={"B@b.py:2", "A@a.py:1"})
    assert made_progress(prev, cur) is False


def test_zero_to_zero_is_stable() -> None:
    """全部修好之后再跑一轮（failed=0, regressions=0）不该被当成进展。

    否则最后一轮"确认已修好"的复跑会被误记为进展，把 no_progress 计数清零。
    """
    assert made_progress(_snap(failed=0), _snap(failed=0)) is False


def test_snapshot_is_immutable_in_practice() -> None:
    """签名集合存的是 frozenset，避免被调用方就地改掉导致历史快照失真。"""
    snap = _snap(signatures={"A@a.py:1"})
    assert isinstance(snap.signatures, frozenset)
