# 开源发布审查（2026-11-06）

本次目标是可供审阅的 engineering preview 候选，不是生产上线认证。审查基线为 `9e99eda2036a17d4c0f38c2a8e505c57716bdd39`，加本次工作区改动。本报告生成时，候选尚未提交、推送或公开；后续交付状态以候选分支的提交和 CI 记录为准。

## 仓库和历史

GitHub 仓库为 `wanjiang263/CodePilot-Repair`，目前私有，默认分支 `main`。远端 7 个分支均已核对为当前工作分支的祖先，远端没有标签。当前较新的 `codex/phase12-real-issue-pilot` 尚未在远端出现。

本地 `.git/shallow` 的 pvlib 边界 `04a523f` 属于本地异常 `main`，不是当前 HEAD 的祖先。对当前工作分支执行独立单分支克隆后，副本为 **non-shallow**，根为本项目初始化提交 `98aa6de`，没有带入本地异常 `main`。无需因该边界重建项目历史，也不要执行 `push --all` 或 `push --mirror`。

## 许可证与第三方材料

- 采用官方 Apache-2.0 原文；署名 `Copyright 2026 jiaoenqi`。所有者确认自有代码属于个人项目，无公司或团队权属限制。
- 增加 LICENSE、NOTICE、安全政策、贡献说明、社区规范。私密报告及社区投诉邮箱由所有者确认为 `jeq0731@gmail.com`。
- 补齐前端中英文许可证说明、Docker 构建中的 LICENSE/NOTICE，以及 Python 包元数据和许可文件。
- 保留 py-tree-sitter MIT 源码快照声明；从官方 GitHub 接口取回 14 个第三方项目、65 个基准提交的许可证，保存 25 份去重文本。另保留 SWE-bench v4.1.0 的 MIT 许可证。具体来源及摘要见 [第三方清单](third-party/README.md)。
- 前端生产依赖的完整许可文本包含在 `web/public/third-party-notices.txt`，随构建复制。Python/前端 SBOM 和 Python 许可元数据已保存。
- 可选 psycopg 的 LGPL 和可选 CUDA 库的专有条款没有被 Apache-2.0 替代；本次源码及 wheel 不捆绑其二进制或模型权重。

## 密钥与隐私

使用校验过官方发布包 SHA-256 的 Gitleaks 8.30.1，检查当前候选文件树、当前项目分支及远端 main 的可达历史，以及 176 个根目录/`logs/` 本地排障文件。历史扫描实际处理 312 个非合并变更提交；当前分支可达提交总数为 318。

历史和候选树各有 33 个 generic-api-key 命中，逐条定位为测试凭据或 SHA-256 清单误报；日志扫描 0 命中。`.gitleaksignore` 仅记录已审阅历史条目的精确 fingerprint，不排除目录、不添加通用密钥模式白名单。应用该清单后历史复扫 0 命中。没有发现需要据此轮换的真实凭据，但这不是对所有未知格式密钥或磁盘上所有运行目录的保证。

另检查了当前 tracked 文件中的邮箱、Windows 路径和私网地址。邮箱命中为测试示例/误匹配；历史路径与本地 Docker 网络地址保留为实验元数据，不是公开部署配置。下载地址指向公开 PyPI，不含私有索引凭据。300 个已跟踪的 `artifacts/` 证据文件也纳入候选树扫描；`.gitignore` 不会使这些文件自动退出版本管理。未跟踪日志、`.env`、运行数据库、模型缓存和嵌套工作区不进入候选源码归档。

## 依赖安全

对 universal `uv.lock` 的全部 181 个第三方 registry 包，逐版本查询 PyPI 官方 JSON 中的漏洞信息及许可元数据，查询失败为 0；最终未撤销公告命中为 **0**。候选 CI 的核心 Python `pip-audit` 也报告 0。本地 npm 镜像源最初报告 0，但随后 GitHub CI 使用官方源检出了 13 项前端开发依赖告警，因此不能采用镜像源结果。已升级 Vitest 到 5.1.0、更新兼容范围内工具链，并为 typescript-estree 明确覆盖到修复后的 minimatch >=11.0.1；官方 npm 源复扫报告 **0** 已知漏洞，50 项前端测试通过。CI 使用 Node 22；本地开发应使用新版 Vitest 支持的 Node 22.12+ 或 24。以上均为审查时公告快照，不覆盖尚未公开的漏洞。

修复的锁定版本包括 certifi、filelock、GitPython、setuptools、virtualenv，以及 datasets、fsspec、huggingface-hub、tokenizers、transformers。可选依赖下限相应更新；真实模型和官方 Harness 未运行，所以不能声称这些升级已通过真实 embedding 或完整官方评测兼容性验证。另一个 pip-audit 在线扫描长时间无结果，已终止；不把它计作通过。

推送候选后，额外对照 GitHub 默认分支的 29 条 Dependabot 告警，发现 PyPI 返回数据未覆盖的 `GHSA-6hmw-9432-67qq`：原候选的 langgraph-checkpoint-sqlite 3.1.1 仍受影响。已将其下限设为 3.1.2 并锁定到 3.1.3，同时更新 langgraph-checkpoint 到 4.3.1。最终候选对上述 29 条 GitHub 告警的受影响数为 0，逐条版本范围对照见 [GitHub 告警核对](third-party/github-advisory-review.json)。默认分支未合并前，GitHub 仍会显示旧分支告警；没有人工忽略或关闭它们。

## 安装、构建与回归

| 检查 | 结果 |
|---|---|
| 当前项目分支独立克隆 | PASS；non-shallow，自有根提交 |
| 前端干净依赖安装 | PASS；不运行依赖安装脚本 |
| 前端 typecheck / lint / Vitest / build | PASS；14 个测试文件，50 项测试 |
| Python 相关回归 | PASS；147 passed，1 项真实模型测试 skipped |
| Ruff（改动的 Python 源文件） | PASS；格式检查通过 |
| mypy | PASS；217 个源文件 |
| 锁文件一致性 | PASS |
| wheel 与 sdist 构建 | PASS；精确文件清单，不遍历本地运行目录 |
| wheel 全新安装与依赖一致性 | PASS；先验证普通安装，再按核心锁定版本安装 |
| 已安装 wheel 的健康检查、鉴权与离线修复任务 | PASS；任务 succeeded，补丁可用，真实 provider 请求/费用均为 0 |

安装测试发现原 wheel 缺少默认配置及演示资源，已修复为携带审阅过的明确资源文件，并让安装后的配置和离线环境规格定位到包资源。源码运行仍使用仓库中的资源。

新增前端和包安装 CI job、每周依赖审计 workflow、Dependabot 更新配置，并把工作流权限限制为 `contents: read`。GitHub 远端漏洞告警和自动安全修复已启用并回读确认。候选已推送到 `codex/open-source-preview`，前端、包安装和 lint/types 已在 GitHub CI 通过；全量 pytest 与 Linux Docker 暴露的问题正在修复复验，最终结果以[候选 CI](https://github.com/wanjiang263/CodePilot-Repair/actions?query=branch%3Acodex%2Fopen-source-preview)为准。

CI 修复包括 pytest 根目录导入路径、旧格式差异、Docker 测试构建资源清单、跨平台路径与内存采集、隔离离线快照、固定测试时钟，以及与现行重新定位/回归去重行为一致的 Docker 夹具。没有降低覆盖率要求或跳过失败用例。

## 发布动作与保留限制

1. 候选已提交并仅推送 `codex/open-source-preview`；未推送本地异常 `main` 或全部 refs。
2. 通过 PR 或正常快进方式更新远端 `main`，不需要强推；等待新增 CI 实际完成。
3. 发布 preview 前核对远端默认分支的 LICENSE/NOTICE 和最终文件清单，再确认将整个仓库公开。公开操作不属于本次已执行动作。
4. 仓库仍私有，GitHub 私密漏洞报告接口返回 404；已配置邮箱作为可立即使用的私密报告渠道。公开后可再启用 GitHub 私密报告。
5. 已启用远端 `main` 基础保护：禁止强推和删除，对管理员同样生效，保留个人维护者的正常推送能力。新增 CI 实际运行后，再选择实际 check 名加入合并门禁；没有配置单人项目无法自行满足的额外审批人数。

24h soak、真实 provider、真实 embedding、浏览器 E2E、复杂真实大仓及全平台 Docker 回归没有在本次执行。Linux Docker 验收已在 CI 实际执行，仍需核对最终通过结果。历史测试数字不升级为本次验证结果。这些限制必须保留在 preview 声明中。

## Phase 37 增补（2026-11-07）

本节只增补后续证据，不改写 2026-11-06 审计当时的事实。以 `d86360a`
为基线的工作树新增 Chromium 浏览器验收、OpenAI 兼容 SSE 解析/中断/usage
结算、真实 BigModel GLM 流式探针，以及禁用 SDK 目录上下文构建的防护；
详见 [Phase 37 验证报告](notes/phase37-validation.md)。三条接受的真实
GLM 请求分别覆盖完成、取消和超时；连同此前保留的六条探针请求，九次请求
的已知结算费用为 0.000442 CNY，六次中断的实际费用未知且保留 4.8086016 CNY
预留，不能把这些数字相加称为实际账单。

新增候选文件已做精确密钥模式复核：最终 79 个变更/未跟踪候选文件的两处
脱敏命中分别为证据清单里的文件名与 SHA-256 摘要、以及 Python 路径属性
引用，复核后无待审命中或真实凭据；`.env` 已忽略且未纳入版本管理。证据
目录中的 `gitleaks-precommit-final.json` 和 `secret-review-final.json`
保留最终扫描与复核结果，早期 50 文件扫描另行保留。Apache-2.0、
个人署名、私密邮箱与第三方声明沿用前文，不因新增代码而变化。

Phase 37 修改了 HTTP/SSE、provider 和 sandbox 源码，因此 Phase36 的
50/50 Docker 与 4184 项离线回归只作为旧源码证据。当前源码全量离线的
4202 个用例为 4199 passed / 1 failed / 2 skipped，唯一失败是 Windows
进程树超时用例在整机负载下的 3.75 秒对 3.5 秒阈值偏差；同一模块隔离
重跑 21 passed。当前源码 Docker 为 50 passed / 0 failed。详见
[最终证据增补](notes/phase37-validation.md#final-evidence-addendum-at-2026-11-07-2125-0800)。
候选 `cddeeb2` 的 GitHub CI 六个 job 与依赖审计均通过，详见
[候选 CI 结果](notes/phase37-validation.md#candidate-ci-result-at-2026-11-07-2156-0800)。
后续仅改发布说明的提交须另行核对 CI；目前只能发布 research / engineering
preview，不能称为稳定版或生产可用。
