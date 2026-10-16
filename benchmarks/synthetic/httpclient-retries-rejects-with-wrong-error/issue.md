Rejecting a negative retry count raises a plain `ValueError`.

Everything this library rejects about *configuration* is a `ConfigError`, and callers rely on that to tell a bad config apart from a transport failure. The timeout check already raises the right type; the retry check does not.

Actual behaviour on the current revision:

```
Traceback (most recent call last):
E           ValueError: retries must be >= 0, got -2
src/httpclient/config.py:20: ValueError
```
