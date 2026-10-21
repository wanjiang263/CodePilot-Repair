# Phase 14 演示脚本（约 3 分钟）

录制状态：**NOT RUN**。当前执行环境没有可用的屏幕录制通道；本文是可直接照做的完整演示路径，
不得把“未录制”写成已完成。

准备：

```bash
cp .env.example .env
# 设置 CODEPILOT_API_KEY=<至少 16 字符>
uv sync --extra dev
uv run codepilot api          # 终端 1
uv run codepilot worker       # 终端 2
cd web && npm run dev         # 终端 3 -> http://127.0.0.1:5173
```

默认离线模式，不会产生模型费用。若要展示 Provider 目录，可在 Settings 中只添加模板，
不要点击 **Test inference**，除非你明确接受一次极小请求的供应商费用。

---

## 0:00–0:20 打开工作台

1. 打开 `http://127.0.0.1:5173/`。
2. 输入 `CODEPILOT_API_KEY`，点击 **连接 / Connect**。
3. 说明：Key 只保存在当前标签页的 sessionStorage，浏览器不接触模型 Key。

预期：左侧导航、最近修复、回放归档；顶部显示 **API 已连接 / Connected to local API**。

## 0:20–0:40 切换中英文

1. 进入 **设置 / Settings → 通用 / General**。
2. 在 Language 中把 **简体中文** 切到 **English**，再切回中文。
3. 观察左侧导航、按钮、状态徽章、提示即时变化，无需刷新。

预期：`document.documentElement.lang` 随之变化；刷新后语言仍保留。

## 0:40–1:00 切换明暗主题

1. 在 Theme 中选择 **Dark / 黑暗**。
2. 回到新建任务页，确认背景、卡片、输入框、Diff/日志配色都切换为深色。
3. 选择 **System / 跟随系统**，说明它会监听操作系统主题变化。
4. 切回 **Light / 明亮**，刷新页面确认无白屏闪烁。

## 1:00–1:40 设置中心与模型供应商

1. 进入 **设置 / Settings → 模型与供应商 / Models & Providers**。
2. 点击 **添加供应商 / Add provider**，展示内置目录：
   OpenAI、Anthropic、DeepSeek、Z.AI、BigModel、Moonshot/Kimi、Qwen、AWS Bedrock、Custom。
3. 选择 **DeepSeek**（或任一已配置环境变量的模板），确认协议、Base URL、模型与来源版本已预填。
4. 点击 **创建供应商 / Create provider**。
5. 展示凭据区：Key 已存在时只显示来源（环境变量或加密凭据库）与可用状态，页面不显示明文。
6. 展示模型行：上下文、最大输出、工具调用、价格、阻塞项；点击 **默认 / Default** 设为全局默认。
7. （可选）打开 **校验配置 / Check configuration**，说明它完全本地执行，不联网。
8. 进入 **智能体 / Agents**，把某个角色（如 Coder）覆盖到另一个模型，说明优先级：
   任务级 > 角色覆盖 > 全局默认；保存只影响新任务。

预期：所有保存都有版本号与成功反馈；同一设置页并发保存会用 409 拒绝过期版本。

## 1:40–2:30 创建一个真实运行的离线任务

1. 回到 **新建修复 / New repair**。
2. 仓库选择 `demo/mini_calc`，执行模式保持 **离线演示 / Offline demo**。
3. 点击 **使用示例 Issue / Use example issue**，或粘贴：

   > `add(2, 3) returns 4 instead of 5. The contract says add() must return the exact sum, and the existing test tests/test_calc.py::test_add fails.`

4. 点击 **开始修复 / Start repair**。
5. 在时间线中依次指出：Issue 分析、检索、定位、影响分析、环境与基线、计划、编码、
   校验、目标测试、回归、独立审查、最终报告。
6. 打开右侧 **变更 / Changes**，展示统一/并排 Diff 与补丁哈希。
7. 打开 **测试 / Tests**，指出官方判分与内部验证是两套口径；本任务没有官方 Harness。
8. 打开 **运行 / Run**，展示预算读数、冻结的 LLM mode / model identity / config hash。

预期：任务终态为 **已完成 / Completed**，Patch 可下载，报告可打开；所有运行标记为模拟。

## 2:30–2:55 历史真实实验回放

1. 进入 **历史 / History** 或 **基准评测 / Benchmarks**。
2. 打开 `GLM14 · marshmallow-1343`。
3. 指出：
   - 官方 verdict：**RESOLVED**；
   - 内部终态：**budget_exhausted / budget_test_runs**；
   - 两者独立，不允许合并成一个“成功”徽章。
4. 打开 **回放 / Replay**，说明不会重新执行、不会改写 artifact；页面为只读。
5. 对比 `GLM13 · UNRESOLVED / stopped / repeated_patch`，说明官方与内部可以相反。

## 2:55–3:00 收尾

展示 **设置 → 关于 / About** 的版本、Git commit、文档与许可证说明。

---

## 录制注意事项

- 不要展示 `.env`、API Key、Provider 明文 Key 或宿主绝对路径。
- Test inference 会产生费用；演示默认不点，或先明确提示并确认。
- 不要用 Fake/Mock 结果推导 SWE-bench 解决率。
- 若使用 Compose 演示，先执行 `docker compose up --build`，视频中说明 Docker socket 风险。
- 本环境未录制视频：状态 **NOT RUN**。
