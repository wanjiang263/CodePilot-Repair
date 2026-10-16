`mean([])` blows up with a `ZeroDivisionError`.

The package defines `EmptySample` for exactly this situation, and callers catch that type to decide whether to show `n/a`. Letting the builtin division error escape means every caller either crashes or has to catch `Exception`.

Actual behaviour on the current revision:

```
Traceback (most recent call last):
E       ZeroDivisionError: division by zero
src/textkit/stats.py:12: ZeroDivisionError
```
