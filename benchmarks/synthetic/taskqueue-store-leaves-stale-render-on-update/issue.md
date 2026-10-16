Overwriting a key does not change what reading it returns.

`get` caches the rendered value. The write path updates the data but leaves the rendered copy behind, so a key that has been written twice keeps reporting its first value forever. `delete` already clears both maps; the write path needs the same treatment.
