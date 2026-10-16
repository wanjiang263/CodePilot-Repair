Category totals drift after many small entries.

`Ledger.total` rounds its result, and the account view compares the two against each other. After a handful of 0.1 entries `by_category` reports something like `0.30000000000000004` while `total` reports `0.3`, so the two views of the same ledger disagree.

Actual behaviour on the current revision:

```
E        +      where <built-in method values of dict object at 0x000001F8AB592340> = {'food': 0.30000000000000004}.values
E        +        where {'food': 0.30000000000000004} = by_category()
E        +          where by_category = Ledger(entries=[Entry(amount=0.1, note='Coffee', category='food'), Entry(amount=0.1, note='Coffee', category='food'), Entry(amount=0.1, note='Coffee', category='food')]).by_category
E        +  and   0.3 = total()
E        +    where total = Ledger(entries=[Entry(amount=0.1, note='Coffee', category='food'), Entry(amount=0.1, note='Coffee', category='food'), Entry(amount=0.1, note='Coffee', category='food')]).total
tests/test_category_rounding.py:21: AssertionError
```
