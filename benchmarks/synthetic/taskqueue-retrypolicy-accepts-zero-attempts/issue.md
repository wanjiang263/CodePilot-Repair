`RetryPolicy(attempts=0)` is accepted.

A policy that never calls the function is not a retry policy - it is a way to silently skip work. Constructing it should fail immediately rather than produce an object whose `run` always reports exhaustion.
