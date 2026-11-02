"""E-01 的图级证据：完整 M2 图发出去的每一个 prompt 都不含评测侧内容。

fixture 级的泄漏检查（E-07）看的是数据；这里看的是**真正发给模型的文本**。
中间隔着推理投影、检索上下文、仓库索引、工具回传、诊断片段好几环，任何一环把
``eval/`` 的东西拼进 prompt，后面的所有分数都变成开卷考试——而症状只是"成功率
莫名偏高"，事后无从归因。

用 FakeLLM 是因为它的 ``calls`` 记录了每个请求：断言对象是实际出站文本，不是
"我们大概不会拼进去"。FakeSandbox 同理——这条测试证明的是信息隔离，不是修复能力。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import harness as harness_module
import pytest
from harness import build_harness, passed_result

from codepilot.eval.synthetic import SyntheticFixture, load_fixture
from codepilot.llm.types import LLMRequest
from codepilot.schemas.enums import RunStatus
from support.synth import SENTINEL, build_and_load, gold_sentinel_spec, write_fixture

TARGET_TEST = "tests/test_public.py::test_documented_base_behaviour"

#: 只要出现在推理侧就算泄漏。每一项都真实存在于 fixture 目录，但只存在于 ``eval/``。
FORBIDDEN_FRAGMENTS = (
    SENTINEL,
    "gold.patch",
    "test_hidden",
    "eval/",
    "fail_to_pass",
    "pass_to_pass",
)


def _prompt_texts(llm: Any) -> list[str]:
    """把每个请求展开成"模型实际会读到的字符"。"""
    texts: list[str] = []
    for request in llm.calls:
        assert isinstance(request, LLMRequest)
        parts: list[str] = []
        for message in request.messages:
            parts.append(message.content or "")
            for call in message.tool_calls:
                parts.append(call.name)
                parts.append(json.dumps(call.arguments, ensure_ascii=False, default=str))
        parts.extend(str(tool) for tool in (request.tools or ()))
        texts.append("\n".join(parts))
    return texts


def _run_graph(tmp_path: Path, fixture: SyntheticFixture, monkeypatch: pytest.MonkeyPatch) -> Any:
    """在 fixture 的**推理视图**上跑一次完整的 M2 图。"""
    repository = fixture.materialize(tmp_path / "repo")
    monkeypatch.setattr(
        harness_module,
        "_base_repo",
        lambda root, **_: (repository, fixture.metadata.base_commit),
    )
    monkeypatch.setattr(harness_module, "ISSUE", fixture.issue)
    runner = build_harness(
        tmp_path,
        # 基线两次运行必须记录**同一个**目标测试节点：图只把基线里出现过的测试当作
        # 授权目标，否则补丁根本不会被执行，运行会以 tests_failed 收场。
        baseline_results=[passed_result(TARGET_TEST), passed_result(TARGET_TEST)],
        results=[passed_result(TARGET_TEST)],
        tests=(TARGET_TEST,),
        coder_rounds=[
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 1",
                        "replace": "    return value",
                    },
                },
                {"tool": "finish", "arguments": {"summary": "fixed normalize"}},
            ]
        ],
    )
    runner.state = runner.run()
    return runner


def test_graph_prompts_never_carry_evaluation_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """39 个正式任务共用的哨兵字符串不得出现在任何一次请求里。"""
    fixture = build_and_load(tmp_path, gold_sentinel_spec())
    assert SENTINEL in fixture.evaluation.gold_patch, "哨兵必须真的存在于 gold，否则这条测试是空的"

    runner = _run_graph(tmp_path, fixture, monkeypatch)
    try:
        assert runner.state.status is RunStatus.PROVISIONALLY_VERIFIED, (
            runner.state.model_dump_json()
        )
        assert len(runner.state.attempts) == 1
        texts = _prompt_texts(runner.llm)
        assert len(texts) >= 4, f"图没有真的跑起来：只有 {len(texts)} 次调用"

        joined = "\n".join(texts)
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in joined, f"评测侧内容 {fragment!r} 出现在了发给模型的 prompt 里"
        assert not (runner.workspace / "eval").exists(), "推理 workspace 里出现了 eval/"
        assert not (runner.original / "eval").exists()
        assert "return value - 1" in joined, "base 源码应当可见——否则这条测试什么也没测"
    finally:
        runner.close()


def test_a_leaked_issue_reaches_the_prompt_so_the_scan_is_not_vacuous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正控制：把 gold 的那一行写进 issue.md，同一套扫描必须抓到。

    一个从未报出过问题的检查器和一个没有检查器等价。这条测试不检查产品代码，它检查
    的是上面那条断言有没有杀伤力。
    """
    directory = write_fixture(
        tmp_path / "leak", gold_sentinel_spec(task_id="demo-leaked-issue"), name="leak"
    )
    leaked = f"The fix is to write `{SENTINEL}` into the return statement.\n"
    (directory / "issue.md").write_text(leaked, encoding="utf-8", newline="\n")
    fixture = load_fixture(directory)

    runner = _run_graph(tmp_path / "clean", fixture, monkeypatch)
    try:
        joined = "\n".join(_prompt_texts(runner.llm))
        assert SENTINEL in joined, "泄漏的 Issue 没有进到 prompt 里：负控制无从可信"
    finally:
        runner.close()
