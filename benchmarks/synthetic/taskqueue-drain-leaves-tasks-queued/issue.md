`drain` hands out the tasks but leaves them in the queue.

The worker takes a batch with `drain` and then processes it; because the queue still holds those tasks, the next `drain` hands out the same work twice. The method is documented as emptying the queue.
