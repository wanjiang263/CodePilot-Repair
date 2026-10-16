`title('')` raises `IndexError`.

Blank cells are normal in imported spreadsheets, and the label generator walks a column of them. The function is meant to leave an empty value alone.

Actual behaviour on the current revision:

```
Traceback (most recent call last):
E       IndexError: string index out of range
src/textkit/normalise.py:20: IndexError
```
