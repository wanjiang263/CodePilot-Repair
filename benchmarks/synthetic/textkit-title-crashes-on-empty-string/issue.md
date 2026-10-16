`title('')` raises `IndexError`.

Blank cells are normal in imported spreadsheets, and the label generator walks a column of them. The function is meant to leave an empty value alone.

Actual behaviour on the current revision:

```
    def title(value: str) -> str:
        """首字母大写，其余原样。"""
>       return value[0].upper() + value[1:]
               ^^^^^^^^
E       IndexError: string index out of range
src/textkit/normalise.py:20: IndexError
```
