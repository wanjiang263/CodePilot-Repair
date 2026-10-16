A shared response script runs out too early.

Tests reuse one list of canned responses across several recorders. Each recorder is supposed to work from its own copy, because it pops from the list as it serves responses - sharing the caller's list means one recorder drains the script the next one was going to use.
