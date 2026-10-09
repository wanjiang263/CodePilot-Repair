"""认证后端。"""

from miniauth.backends.base import AuthBackend
from miniauth.backends.memory import MemoryBackend

__all__ = ["AuthBackend", "MemoryBackend"]
