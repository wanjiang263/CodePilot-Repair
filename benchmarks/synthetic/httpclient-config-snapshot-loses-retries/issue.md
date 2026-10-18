Client settings are lost and the diagnostic snapshot is incomplete.

Creating a client through the convenience API with retries=5 leaves its retry budget at the default 2. Diagnostic snapshots omit the retry count, including for a configuration created directly. Callers expect the supplied timeout and retry settings to take effect and snapshots to report base URL, timeout and retries accurately.
