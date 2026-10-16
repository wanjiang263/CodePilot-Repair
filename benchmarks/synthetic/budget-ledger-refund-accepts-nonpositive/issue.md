`Ledger.refund` accepts a zero or negative refund amount.

A negative refund silently records an extra *charge*, which is the opposite of what the caller asked for. The ledger already raises `InvalidAmount` for bad amounts on the `add` side; the refund path needs the same guard.
