Shipping approves products that have no per-shipment limit configured.

`fits` should reject any line whose quantity exceeds the limit for its SKU. When the limits table has no entry for a SKU there is no limit we can check, so the line must not be waved through - the current behaviour lets it through, and the warehouse has been shipping single-unit orders for SKUs that are not configured at all.
