"""带缓存的键值存储。

``_rendered`` 缓存 :meth:`get` 的结果。**写路径必须让对应的缓存失效**，否则读到的
还是旧值。这类缺陷只在第二次读之后出现，也因此最容易躲过小范围的冒烟测试。
"""

from __future__ import annotations


class MissingKey(Exception):
    """键不存在。"""


class Store:
    """内存键值存储，带一层渲染缓存。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._rendered: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        """写入。缓存里这一条必须一起清掉。"""
        self._data[key] = value
        self._rendered.pop(key, None)

    def get(self, key: str) -> str:
        """读取渲染结果并缓存它。"""
        cached = self._rendered.get(key)
        if cached is not None:
            return cached
        if key not in self._data:
            raise MissingKey(key)
        rendered = self._data[key].upper()
        self._rendered[key] = rendered
        return rendered

    def delete(self, key: str) -> None:
        """删除一条记录。"""
        self._data.pop(key, None)
        self._rendered.pop(key, None)

    def keys(self) -> list[str]:
        """排序后的键列表。"""
        return sorted(self._data)
