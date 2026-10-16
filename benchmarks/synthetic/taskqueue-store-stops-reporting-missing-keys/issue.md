Reading a key that was never stored returns a stale value or reports nothing useful.

`MissingKey` is part of this library's interface and callers depend on it to distinguish "empty" from "absent". The check that raised it used to be there; today the read path goes straight to the value.
