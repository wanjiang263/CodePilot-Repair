Describing an unlisted SKU raises KeyError.

The stock report includes products that have not yet been added to the catalog. For example, an unlisted Q should be displayed as "Q - unknown" rather than aborting the report. This also applies when a caller supplies its own catalog; listed products should retain their names.

Actual behaviour on the current revision:

```
Traceback (most recent call last):
E       KeyError: 'Q'
src/inventory/catalog.py:14: KeyError
```
