"""Repository 层（02 §6–§7）：workspace 隔离、扫描、解析、索引、图查询。

依赖方向：本包依赖 L1 基础设施（``storage``、``config``）与 L0 契约（``schemas``），
**不**依赖 L3 agents 或 L4 graph。检索（``retrieval``）反过来依赖本包产出的索引。
"""

from __future__ import annotations

__all__: list[str] = []
