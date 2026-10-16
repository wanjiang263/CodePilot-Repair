A zero or negative timeout is accepted again.

`validate_timeout` exists and the constructor used to call it, but the check no longer runs, so `Config(timeout=0)` builds successfully and then hangs or fails in whatever transport receives it. The helper itself still works when called directly - it is the wiring that went missing.
