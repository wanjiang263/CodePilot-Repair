`total_weight` raises `KeyError` for a product with no listed unit weight.

The function already treats a missing price table entry as zero weight in intent - an item we have no weight for simply contributes nothing - but the lookup itself blows up before that can happen.

Actual behaviour on the current revision:

```
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
.0 = <list_iterator object at 0x00000211FA9D0EB0>
>   return round(sum(line.quantity * per_unit[line.sku] for line in lines), 3)
                                     ^^^^^^^^^^^^^^^^^^
E   KeyError: 'Z-9'
src/inventory/shipping.py:45: KeyError
```
