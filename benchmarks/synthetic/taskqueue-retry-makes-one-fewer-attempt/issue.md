Work is abandoned before the configured attempts are used.

With an allowance of three attempts, a callback that fails twice and would then succeed never reaches that success. A one-attempt policy also fails without calling a healthy callback. The allowance includes the initial call; success should stop further attempts, and exhaustion should be reported only after the allowance is used.
