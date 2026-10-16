Paged reads return the wrong slice for any page after the first.

`window_start` is the single place that turns a page number into an offset, and `textkit.paging.page` is supposed to use it. The page helper instead re-derives the offset where it needs it, so the two copies of the rule have drifted and no page boundary lines up any more.
