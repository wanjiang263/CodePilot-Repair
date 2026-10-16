Some shipments are short by one box.

`box_count` divides the total weight by the box capacity and reports that as the number of boxes. It works when the weight fills the boxes exactly and quietly loses the remainder otherwise - the leftover fraction of a shipment is simply not shipped.

Actual behaviour on the current revision:

```
def test_partial_box_is_counted() -> None:
>       assert box_count(10.0, 4.0) == 3
E       assert 2 == 3
E        +  where 2 = box_count(10.0, 4.0)
tests/test_box_ceil.py:11: AssertionError
```
