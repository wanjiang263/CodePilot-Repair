# CodePilot-Repair

Repository-level autonomous software engineering agent.

给定一个 Python 仓库、一个固定 commit 和一份 Issue，CodePilot-Repair 在隔离环境中自动完成
**定位 → 计划 → 修改 → 执行 → 失败诊断 → 迭代修复 → 回归验证 → Review**，
输出一个经过真实执行验证的 patch，以及可以完整回放的运行记录。

## 状态

项目正在按 `docs/04-分阶段开发任务清单.md` 的 Phase 顺序开发。当前进度见
[`docs/05-精细化待办清单.md`](docs/05-精细化待办清单.md)。

> 本 README 中的实验结果表格在 Phase 10–12 完成后填入，所有数字只能来自 `artifacts/` 中
> 真实记录过的运行（03 §19）。在真实实验完成之前，此处不放任何数字。

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/01-需求文档-PRD.md`](docs/01-需求文档-PRD.md) | 需求、范围、指标、验收标准、信息隔离协议 |
| [`docs/02-实现方案.md`](docs/02-实现方案.md) | 设计权威：架构、数据模型、算法、编排 |
| [`docs/03-AGENTS.md`](docs/03-AGENTS.md) | 开发规范：工程原则、测试策略、汇报格式 |
| [`docs/04-分阶段开发任务清单.md`](docs/04-分阶段开发任务清单.md) | Phase 0–14 的任务与验收（阶段编号权威） |
| [`docs/05-精细化待办清单.md`](docs/05-精细化待办清单.md) | 由 04 深拆得到的阶梯式任务清单与依赖 DAG |

文档冲突时的优先级：设计细节以 02 为准；流程规则以 03 为准；阶段编号以 04 为准。

## 快速开始

```bash
# 安装依赖（需要 uv；Python 3.12）
uv sync --extra dev

# 确认 CLI 可用
uv run codepilot --help

# 跑不依赖 Docker 与真实 LLM 的测试
uv run pytest -m "not docker and not llm"
uv run ruff check .
uv run mypy src
```

需要真实 LLM 的验收脚本放在 `scripts/`，运行产物写入 `artifacts/`；
需要 Docker 的测试标记为 `@pytest.mark.docker`，Docker 不可用时自动跳过。

密钥只通过环境变量提供：复制 `.env.example` 为 `.env` 后填写，`.env` 永不提交。

## 架构

```text
CLI (codepilot)            FastAPI
      │                       │
      └───────────┬───────────┘
                  ▼
          Task Runner / Worker
                  │
                  ▼
       LangGraph RepairGraph ──── Checkpointer (SQLite)
                  │
   ┌──────────────┼───────────────┬─────────────────┐
   ▼              ▼               ▼                 ▼
Repository     Agents          Sandbox          LLM Layer
Intelligence   (Analyzer,      (Docker,         (Provider,
(Index,        Planner,        EnvBuilder,      Structured Output,
Retrieval,     Coder,          TestRunner,      Record/Replay,
Graph)         Diagnoser,      Baseline)        Cost Tracking)
               Reviewer)
   │              │               │                 │
   └──────────────┴───────┬───────┴─────────────────┘
                          ▼
             Storage (SQLite / PostgreSQL)
             Artifacts (每次运行一个目录)
             Events (JSONL + events 表)
```

完整设计见 [`docs/02-实现方案.md`](docs/02-实现方案.md)。

## 安全边界

仓库代码只在 Docker 容器内执行，宿主机不执行任何仓库代码；测试执行阶段断网；
Agent 的文件工具路径被限制在隔离 workspace 内；日志与事件写盘前统一脱敏。

沙箱是**评测沙箱**，用于防止死循环、写满磁盘、误访问网络等意外副作用，
**不是**对抗恶意代码的生产级安全边界（03 §13）。
