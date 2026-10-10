# V-06～V-10 执行证据

详细范围、接口、限制与实测记录见 [第二批验收记录](../../notes/phase2-semantic-retrieval.md)。`acceptance.json` 保存 JUnit 统计、退出码与覆盖率明细，`manifest.json` 保存本目录文件 SHA-256。

本地 Windows 使用现有 `.venv`：

```text
.venv/Scripts/python.exe -m pytest tests/unit/retrieval/test_embeddings.py -q                 # v06
.venv/Scripts/python.exe -m pytest tests/unit/retrieval/test_embedding_api.py tests/unit/retrieval/test_embeddings.py -q  # v07
.venv/Scripts/python.exe -m pytest tests/unit/retrieval/test_embedding_cache.py tests/unit/retrieval/test_embedding_api.py tests/unit/retrieval/test_embeddings.py -q  # v08
.venv/Scripts/python.exe -m pytest tests/unit/retrieval tests/integration/test_cli_retrieve.py -q  # v09/v10
uv pip install --python .venv/Scripts/python.exe faiss-cpu==1.15.1
.venv/Scripts/python.exe -m pytest tests/unit/retrieval/test_semantic_backends.py tests/unit/retrieval/test_embedding_api.py -q  # native-faiss
.venv/Scripts/pytest.exe tests/unit/retrieval tests/integration/test_cli_retrieve.py -q        # collection-after
.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing --cov-report=json:artifacts/phase2-semantic/final-coverage.json --junitxml=docs/evidence/phase2-semantic-20261010/final-pytest.xml
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe docs/evidence/phase2-semantic-20261010/semantic-smoke.py
```

逐项 pytest 同时生成同名 `.xml`；stdout/stderr 重定向至同名 `.log`。`pytest.*` 是 API 分批修复前的初轮全量，`final-pytest.*` 是业务修复后的最终全量。共享测试桩的位置随后调整，当前提交通过 `collection-after` 与完整 Linux CI；完整覆盖率原始 JSON 保留在本机 artifacts，精选数字来自原始 JSON。

两份故意保留的失败证据均已有修复：`api-batching-before.log`（新增分批配置回归 2 failed）、`collection-before.log` 与 `ci-before.log`（pytest CLI 找不到 tests 包）。修复后的相邻日志及 `implementation-ci.*` 全部通过。

真实 FAISS 已安装并执行。`semantic-smoke.py` 使用真实静态索引、真实 NumPy/FAISS，Embedding 为 MockTransport 合成向量，不能用来宣称真实模型质量。真实本地模型推理、真实 Embedding API、L-18 结构化输出/工具调用与完整 Phase 2 Recall@K 验收均 NOT RUN。本机 Docker 不可用，真实 Phase 1 Docker 验收已在 Linux CI 成功执行。
