Zero and negative timeouts are accepted again.

Constructing a configuration with timeout=0 or timeout=-1 succeeds. A timeout must be strictly positive, and invalid values should raise the library configuration error at construction time so callers can report a bad setting before making a request. Positive timeouts should remain usable.
