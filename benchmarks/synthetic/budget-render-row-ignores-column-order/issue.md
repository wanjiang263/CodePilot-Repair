Exported CSV rows sometimes have their columns swapped.

`COLUMNS` is the single definition of the export order, but the row renderer walks the record's own values instead of looking each column up. A record built in a different order therefore writes a different column order than the header line promises.
