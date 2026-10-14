# Phase 7：Failure Diagnosis（D-10～D-26）

日期：2026-10-14。开发分支：`codex/phase7-failure-diagnosis`。
基线：`84dce3f263de28ef8cbbe502c23ba972f27650eb`（Phase 6）。

## 交付状态与证据

| ID | 实现文件（src/codepilot 下，除另注） | 主要测试证据 | 状态 |
|---|---|---|---|
| D-10 | failure/schemas.py；复用 schemas/common.py | test_parser_classifier：round-trip、最深帧必须属于真实 frames、合法行号与相对身份 | IMPLEMENTED |
| D-11 | failure/parser.py；sandbox/junit.py | Golden、损坏 XML、无有效记录、多个 suite / 参数化 / skipped / execution 异常 | IMPLEMENTED |
| D-12 | failure/traceback.py | 混合平台路径、异常链、第三方帧、源码缺失、路径逃逸 | IMPLEMENTED |
| D-13 | schemas/enums.py（既有权威枚举） | 十二值集合精确断言，无新增枚举副本 | IMPLEMENTED |
| D-14 | failure/classifier.py | JUnit type 冲突优先、pytest 阶段、缺失依赖与仓库模块、环境状态优先 | IMPLEMENTED |
| D-15 | failure/classifier.py | 规则命中零模型调用；UNKNOWN/FakeLLM/非法类别/非法 JSON/无 Provider | IMPLEMENTED |
| D-16 | failure/signature.py | 地址/临时路径稳定、异常和根因位置负控制、幂等、排序集合 | IMPLEMENTED |
| D-17 | failure/compressor.py | 3→1 完整样本+2 ID；保留全部 ID / 数量 / Artifact | IMPLEMENTED |
| D-18 | failure/compressor.py | 仓库帧+最后第三方帧、2000 字符、脱敏、原对象不变与遗漏计数 | IMPLEMENTED |
| D-19 | failure/compressor.py；context/builder.py（复用） | 四级优先级、80 条 Unicode 长失败、极小预算、最终装配与重试计数 | IMPLEMENTED |
| D-20 | schemas/diagnosis.py；agents/schemas.py 再导出 | 非空 Evidence、正行号、有限 [0,1] confidence、策略枚举、round-trip | IMPLEMENTED |
| D-21 | agents/diagnoser.py | FakeLLM 全链路、真实索引符号、当前规范 diff、历史仅作假设、错误传播 | IMPLEMENTED |
| D-22 | agents/diagnoser.py | 行文本匹配、文件/行越界、Symbol 混淆、symlink/禁止文件、源码变更、精确置信度 | IMPLEMENTED |
| D-23 | tests/fixtures/failures/ | 17 个真实本地 pytest 场景及源码/命令/退出码；16 XML+1 timeout 执行记录 | IMPLEMENTED |
| D-24 | tests/unit/failure/test_classifier_golden.py | 全量消息/类型/最深帧/类别断言；16/17 规则命中、UNKNOWN=1 | IMPLEMENTED |
| D-25 | tests/unit/failure/test_compress_signature.py | 稳定性、负控制、故意数字碰撞、确定性、无失败、极小预算 | IMPLEMENTED |
| D-26 | scripts/collect_failures.py | 脱敏、重复采集、错误计数、项目边界、保留人工 expected | IMPLEMENTED |

## Phase 8 消费接口

```text
ExecutionResult / JUnit XML
  → FailureParser.parse_execution / parse_xml → FailureBatch
      ├─ execution_failure：执行级 TIMEOUT / OOM / ENV / 无记录 COLLECTION
      └─ failures：真实 ParsedFailure + 共享 TraceFrame
  → FailureClassifier.classify → Classification(category, source, rule, basis)
  → failure.model_copy(update={"category": classification.category})
  → failure_signature / execution_signatures
  → compress_failures → CompressedFailures(groups, all_test_ids, context, parts)
  → DiagnosisAgent.diagnose → 权威 Diagnosis
      └─ last_record / 可选 ArtifactStore：证据拒绝、源码哈希、每次上下文、签名与 Artifact 引用
```

调用方提供可信文件清单、仓库根映射和顶层模块清单；Traceback 路径仅作展示身份，
不会赋予读取权限。`repo_modules` 是缺失模块是否属于仓库的判断依据；未提供清单时
将模块来源保留为 UNKNOWN；显式空清单则表示已知仓库没有顶层模块。类型提取沿用 Phase 6 JUnit 逻辑，并补充 Python
末行与 collection SyntaxError 的 `E ...` 形态。`Failed: DID NOT RAISE ValueError`
保持 exception_type=Failed 与 expected_exception_missing 类别。

同一个 JUnit testcase 同时有 call failure 和 teardown error 时分别保留两条真实
失败详情（相同 test_id、不同 phase），不会只取第一个而丢失清理异常。

首先检查 `batch.execution_failure`；ENV_ERROR/SANDBOX_ERROR 不进入代码修复诊断。
即使环境异常携带部分失败记录，规则仍优先归为 environment，DiagnosisAgent 拒绝
将这些记录当代码诊断输入。OOM 沿用冻结协议 `SANDBOX_ERROR/OOM_KILLED`，没有
新增 ExecutionStatus，也没有修改 ExecutionResult。无 JUnit 的执行异常不伪造测试。
空失败集合会明确报错，不能返回虚假的成功 Diagnosis。

Phase 6 的 `attempt-*-outcome.json` 是审计摘要，不是完整 ExecutionResult；Phase 8
应传递 Sandbox 原始对象。交接测试直接捕获 ReproductionAgent 收到的对象，未给
摘要补造 duration 或 traceback。临时复现测试仍由既有 canonical diff 层排除。

分类返回对象应与诊断记录一起留档，以保留 rule/llm 来源。无 Provider 的 UNKNOWN
返回 source=unavailable；结构化调用错误使用现有 Provider 重试/异常机制。
诊断模型不能创建 ParsedFailure 或 TraceFrame，也不能调用工具、代码或 Shell。
本轮没有实现任何 Phase 8 策略执行或 LangGraph 控制流。

## 分类样本与指标

17 个场景的真实来源见 fixtures 的 `provenance.json` 和 README。16 份 JUnit 包含
17 条失败记录（multiple 场景有两个参数化失败）；另有一个真实 pytest-timeout
执行记录。Windows thread timeout 没有写出 JUnit，这一条从执行状态分类。

JUnit 类别分布：assertion=3、value_key_index=4、fixture=2；syntax、import、
name_attribute、type、collection、environment、expected_exception_missing、unknown
各 1。执行级 timeout=1。JUnit 规则命中率 **16/17=94.12%**，UNKNOWN=1；
若连执行级 timeout 一并统计，则 **17/18=94.44%**，分母口径不同，不能混用。
Golden 的预期从未进入分类输入。这里衡量受控样本规则覆盖，不是真实模型准确率。

## 签名与压缩边界

严格使用 §14.4 的 SHA1(exception_type|file:function|normalize(message))；不含
test_id 或行号，无仓库帧使用 no_repo_frame。地址、数字、临时路径、长引号字符串、
斜杠和空白归一化是确定且幂等的。两份真实 volatile 样本得到相同签名；异常类型、
根因函数或语义短字符串变化的负控制得到不同签名。

数字差异及长字符串差异可能产生**有意的签名碰撞**，缺失函数名的短栈也会降低
区分度。聚合只代表指纹相同，不证明根因相同；原始 ID 与 Artifact 仍完整保留。
这些边界不通过修改冻结 SHA1 算法规避，后续真实多轮误聚合评估登记 DA-010。

压缩保留一个完整结构化样本，其他成员仅保留测试 ID 与组级 Artifact 引用，记录
count、omitted_frames、omitted_traceback_characters 和 truncated_characters。
原始 ParsedFailure 不被改写。样本正文采用原子 JSON 段，预算不足时整段省略，
不会切出缺少身份的半段 JSON。目标测试→复现→新增回归→其他，组内代表也按此
顺序选取；同级按 test_id/内容确定排序，不使用输入到达次序。

默认诊断预算 24,000，可通过 ContextBuilder 配置或调用 budget 覆盖；最终装配、
结构化纠错和显式 overflow 重建都复用既有预算检查。included/omitted 可审计，
未入模的全部测试 ID 仍保留在 CompressedFailures。极小预算下不调用模型。
TokenCounter 的估算仍标记 exact_count=False/method/count_note；严格上限指既有
charged 口径，**没有将 /3.5 估算包装成真实模型 token 数**。DA-001 保持待验收。

## Evidence 与 confidence

每条证据先经 WorkspacePathPolicy，再核对解析后身份的禁止目录/答案隔离规则。
文件需真实存在，行号合法，引用的整段文本须与当前源码逐行匹配（允许缩进与脱敏
表示）。TRACEBACK 还需属于当前观测帧；symbol_id 需对应索引里的同文件同范围。
模型只能引用实际装入最后一次上下文的源码行；源码在模型调用期间改变则拒绝。
不存在、路径逃逸、symlink 越界、答案别名、文本不匹配、符号混淆都有拒绝原因。

调整公式：`validated_confidence = proposed_confidence * valid_count / proposed_count`；
零条证据或全部无效时为 0。测试精确覆盖 0.8→0.8、0.8→0.4、0.8→0。
根因始终是模型假设，证据是可核验引用；源码核验不等同于证明因果关系。
is_patch_caused=True 还要求当前规范 diff 中实际新增行的已验证证据，且 diff 已入模。
未知符号剔除；通过索引的符号输出真实 ID。confidence 仅记录，不用于路由。

## 验证与门禁

最后一笔生产代码改动之后，四条门禁命令均 **exit 0**：`ruff check .`（0.26s）、
`ruff format --check .`（0.24s）、`mypy src`（3.32s）、
`pytest -m "not docker and not llm" --cov --cov-report=term-missing` =
**2139 passed / 0 failed / 1 skipped / 29 deselected**，覆盖率 **94.04%**，耗时
829.43s（13:49）。阈值 80% 未下调，既有断言未弱化；Phase 6 基线为 2107 passed /
93.81%。Phase 7 净增 **33 条用例**（32 条离线 + 1 条 docker 标记；收集总数
2136 → 2169）。唯一 skip 是未配置预安装 Embedding 模型；29 项 deselected 是
`docker`/`llm` 标记用例，**不得计作通过**。日志与逐命令退出码在
`artifacts/phase7/gate-final/{ruff,format,mypy,pytest}.log` 和 `summary.json`。

Phase 7 关键模块覆盖率：`failure/classifier.py`、`failure/compressor.py`、
`failure/signature.py`、`failure/schemas.py` 均 **100%**；`failure/parser.py`
**98%**、`failure/traceback.py` **98%**、`agents/diagnoser.py` **96%**
（未覆盖项是 2 MiB 读取上限等防御分支与个别分支尾项）。

真实 Docker：单独执行 `pytest tests/graph/test_phase7_diagnosis_e2e.py -m docker`
= **1 passed / 3 deselected**，exit 0，12.14s（镜像命中 Phase 6 的构建缓存）。
真实容器执行 pytest 产生 AssertionError 与 junit.xml，经 Phase 6 交接再进入
Phase 7；诊断侧使用 FakeLLM，并断言 `execution_backend=docker` 与
`container_verified=true`，另行人工解析容器落盘的 junit.xml 复核异常类型。
**不把 FakeLLM 的诊断输出当作真实模型效果证据**。日志：
`artifacts/phase7/docker-integration/pytest-docker.log`。本轮不重跑 Phase 4
全量安全验收或历史 Benchmark。

定向集实测：`pytest tests/unit/failure tests/graph/test_phase7_diagnosis_e2e.py`
（去掉 docker 项）**32 passed**（49.02s）；`pytest tests/unit/failure`
**29 passed**（70.83s）。

此前 `artifacts/phase7/gates/` 与 `artifacts/phase7/gates-final/` 两次全量 pytest
在收口发现模块来源不确定性和双详情 JUnit 边界后主动中止，后者未写出 pytest
退出码，**均不计作 PASS，也不计作完成的一次全量验收**；修正后重新运行上述最终
门禁。最后补上的两条断言（非 failed/error 用例不得当失败证据、仓库内帧必须是
相对身份）先以测试自身缺必填字段失败一次，修正测试后通过，期间未改动生产代码。
定向新增反例先失败再修正，未弱化原有断言。

采集 CLI 对真实原始 JUnit Artifact 实测：首次 collected=16 / duplicates=0 /
errors=0；第二次 collected=0 / duplicates=16 / errors=0。入库时统一 LF，未改动
异常消息或失败语义。

## 保留的验收缺口

DA-001 真实 tokenizer；DA-002 symlink Linux CI；DA-003 Phase 3 Linux CI；
DA-004 L-18 API smoke；DA-005 C-14 真实定位；DA-006 真实 Issue benchmark；
DA-008 真实模型复现；DA-009 新增测试交付边界；P-24 真实模型单次修复均未关闭。
DA-007 保持已关闭。新增 DA-010（P2）：真实多轮中的诊断质量与签名误聚合评估。
Phase 7 只将测试源码视为引用材料，不把新增测试或复现测试当作可信成功证据。

本轮仅本地分支与新提交；无 Push、PR、Merge、reset、rebase 或 amend。
