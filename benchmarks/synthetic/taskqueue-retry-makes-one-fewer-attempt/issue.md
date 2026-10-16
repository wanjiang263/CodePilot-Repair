A `RetryPolicy(attempts=n)` only calls the function `n - 1` times.

Callers size their timeouts around the number of attempts they configured. The last attempt is the one that usually succeeds for a flaky dependency, so dropping it turns 'eventually succeeds' into 'always gives up'.
