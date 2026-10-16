Exported CSV rows sometimes have their columns swapped.

`COLUMNS` is the single definition of the export order, but the row renderer walks the record's own values instead of looking each column up. A record built in a different order therefore writes a different column order than the header line promises.

Actual behaviour on the current revision:

```
>       assert render_row(record) == ",".join(str(record[name]) for name in COLUMNS)
E       AssertionError: assert '12,food,CNY,Coffee' == 'food,Coffee,CNY,12'
E         
E         - food,Coffee,CNY,12
E         + 12,food,CNY,Coffee
tests/test_column_order.py:10: AssertionError
```
