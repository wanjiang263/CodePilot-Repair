Rejecting a negative retry count raises a plain `ValueError`.

Everything this library rejects about *configuration* is a `ConfigError`, and callers rely on that to tell a bad config apart from a transport failure. The timeout check already raises the right type; the retry check does not.

Actual behaviour on the current revision:

```
    def validate_retries(value: int) -> int:
        """重试次数必须是非负整数。"""
        if value < 0:
>           raise ValueError(f"retries must be >= 0, got {value}")
E           ValueError: retries must be >= 0, got -2
src/httpclient/config.py:20: ValueError
```
