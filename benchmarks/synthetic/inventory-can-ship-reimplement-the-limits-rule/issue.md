Whole-order shipping decisions disagree with the per-line check.

`fits` is the one definition of "this order is within the per-SKU limits", and `can_ship` is documented to build on it. `can_ship` instead compares quantities against its own copy of the rule, so an order that `fits` rejects can still be approved by `can_ship`.
