Describing an unlisted SKU raises `KeyError`.

`UNKNOWN_NAME` exists so that a catalog lookup can always produce a label, and callers are supposed to get that label instead of an exception - the report generator walks every SKU seen on the floor, most of which are not in the catalog at all.

Actual behaviour on the current revision:

```
        """返回 ``"SKU - 名称"``。未知 SKU 用占位名，调用方不必自己挡 KeyError。"""
        table = catalog or CATALOG
>       name = table[sku]
               ^^^^^^^^^^
E       KeyError: 'Q'
src/inventory/catalog.py:14: KeyError
```
