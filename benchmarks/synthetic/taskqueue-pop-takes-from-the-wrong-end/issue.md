The task queue serves tasks in the wrong order.

`TaskQueue` is documented and named as FIFO, and `push` appends. Under load a task that has been waiting longest can still be served last, which is what the on-call escalation rules were written to avoid.
