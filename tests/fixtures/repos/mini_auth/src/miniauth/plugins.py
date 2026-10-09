"""装饰器：覆盖无参、带参、嵌套三种写法。"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any


def retry(times: int = 3) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """**带参数**的装饰器。"""

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last: Exception | None = None
            for _ in range(times):
                try:
                    return function(*args, **kwargs)
                except Exception as exc:
                    last = exc
            raise last

        return wrapper

    return decorate


def audited(function: Callable[..., Any]) -> Callable[..., Any]:
    """**无参数**的装饰器。"""

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    return wrapper
