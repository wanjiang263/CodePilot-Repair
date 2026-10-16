A client built through the helper ignores the configuration it is given.

`new_client` accepts the same keyword arguments as `Config`, and `Config.as_dict` is what the diagnostics endpoint prints. Right now a caller can pass `retries=5`, see `retries: 2` in the diagnostic snapshot, and have no way to tell which of the two is lying - so both the helper and the snapshot need to be fixed.
