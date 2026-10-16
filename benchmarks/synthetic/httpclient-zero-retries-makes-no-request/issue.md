Configuring `retries=0` makes the client send no request at all.

`retries` counts *extra* attempts after the first one, so zero retries should still mean one honest attempt at the request. Today a zero-retry client raises before ever touching the network.
