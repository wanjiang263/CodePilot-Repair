CSV exports are rejected because the currency column is missing.

The import format requires category, note, currency, amount in that order, including the header for an empty ledger. Each ledger has a currency attribute: omitted currency defaults to CNY for compatibility, while a caller can supply another currency such as USD or EUR. Every exported row must use that ledger currency without converting its amount. Exports from different ledgers must retain their own currencies, including when produced consecutively.
