An order that needs more stock than exists fails with `ValueError`.

The checkout flow catches `OutOfStock` to show a restock prompt, and lets anything else propagate as a bug. A short shipment therefore surfaces as an unhandled error instead of the expected stock problem.

Actual behaviour on the current revision:

```
Traceback (most recent call last):
E           ValueError: B-2: need 1, have 0
src/inventory/stock.py:40: ValueError
```
