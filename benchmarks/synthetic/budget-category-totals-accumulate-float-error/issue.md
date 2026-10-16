Category totals drift after many small entries.

`Ledger.total` rounds its result, and the account view compares the two against each other. After a handful of 0.1 entries `by_category` reports something like `0.30000000000000004` while `total` reports `0.3`, so the two views of the same ledger disagree.
