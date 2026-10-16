`Ledger.add` accepts zero and negative amounts.

Refunds already have their own entry point (`Ledger.refund`), so a negative amount pushed through `add` produces a ledger whose totals look plausible while the individual entries are meaningless. The library defines `InvalidAmount` for exactly this case and the docstring already promises the rule, but nothing enforces it.
