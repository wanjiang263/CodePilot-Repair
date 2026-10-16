The urgent task is still served last when its name sorts late.

`order` is meant to sort by the weight that `rank` computes. It currently hands `sorted` something that makes the comparison depend on the task name instead, so priority only works by accident when names happen to line up with urgency.

Actual behaviour on the current revision:

```
>       assert [task.name for task in order(tasks)] == ["m", "c", "k"]
E       AssertionError: assert ['c', 'k', 'm'] == ['m', 'c', 'k']
E         
E         At index 0 diff: 'c' != 'm'
E         Use -v to get more diff
tests/test_priority_key.py:21: AssertionError
```
