The table reader returns padded cells even though the single-cell parser does not.

`textkit.table.parse_row` is documented to delegate to `split_fields`, which is where the cleaning rules live. It splits on its own instead, so the two entry points disagree about the same input and fixing only one of them leaves the other wrong.
