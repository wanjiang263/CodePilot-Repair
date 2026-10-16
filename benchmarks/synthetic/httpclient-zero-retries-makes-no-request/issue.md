Configuring `retries=0` makes the client send no request at all.

`retries` counts *extra* attempts after the first one, so zero retries should still mean one honest attempt at the request. Today a zero-retry client raises before ever touching the network.

Actual behaviour on the current revision:

```
            except Exception as exc:  # 传输层抛什么异常不由本库规定
                last = exc
>       assert last is not None
               ^^^^^^^^^^^^^^^^
E       AssertionError
src/httpclient/client.py:34: AssertionError
```
