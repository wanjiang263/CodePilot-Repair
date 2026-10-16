The urgent task is still served last when its name sorts late.

`order` is meant to sort by the weight that `rank` computes. It currently hands `sorted` something that makes the comparison depend on the task name instead, so priority only works by accident when names happen to line up with urgency.
