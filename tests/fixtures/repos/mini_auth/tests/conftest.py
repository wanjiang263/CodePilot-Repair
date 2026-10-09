"""pytest 夹具。"""

from __future__ import annotations

import pytest

from miniauth.backends import MemoryBackend
from miniauth.models import User


@pytest.fixture
def user() -> User:
    return User(name="demo", roles=["admin"])


@pytest.fixture
def backend() -> MemoryBackend:
    backend = MemoryBackend()
    backend.add(User(name="demo", roles=["admin"]), "hunter2")
    return backend
