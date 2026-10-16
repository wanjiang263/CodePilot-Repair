`mean([])` blows up with a `ZeroDivisionError`.

The package defines `EmptySample` for exactly this situation, and callers catch that type to decide whether to show `n/a`. Letting the builtin division error escape means every caller either crashes or has to catch `Exception`.

Actual behaviour on the current revision:

```
    def mean(values: list[float]) -> float:
        """算术平均。空输入没有定义，必须报错而不是除零。"""
>       return sum(values) / len(values)
               ^^^^^^^^^^^^^^^^^^^^^^^^^
E       ZeroDivisionError: division by zero
src/textkit/stats.py:12: ZeroDivisionError
```
