Recording a refund increases the ledger total.

`refund` is documented as writing a negative entry for the given amount, but the entry it appends carries the amount unchanged, so money appears from nowhere. `test_refund_adds_an_entry` only checks that an entry appears, not what it contains.
