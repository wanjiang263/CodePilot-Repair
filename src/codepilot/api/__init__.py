"""HTTP 接口层（Phase 13 完整落地，Phase 0 只有 ``/health``）。

这一层**不许**被 L0–L4 的任何模块 import。依赖方向是单向的：
api → graph → agents → …。反过来的话，一次接口调整就会波及核心逻辑，
而接口的变动频率远高于核心逻辑。
"""

from __future__ import annotations

from codepilot.api.app import api_key_dependency, create_app, verify_api_key

__all__ = ["api_key_dependency", "create_app", "verify_api_key"]
