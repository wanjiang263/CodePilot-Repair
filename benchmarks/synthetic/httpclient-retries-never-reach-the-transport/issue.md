The transport layer never sees the configured retry budget.

Some transports need to know how many attempts remain so they can pick a connection strategy that degrades gracefully. The sender protocol takes the retry count as its third argument, but nothing along the path actually passes it, so calls fail with a missing-argument error as soon as a real sender is plugged in.
