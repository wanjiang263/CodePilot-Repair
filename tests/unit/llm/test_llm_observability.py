"""逐请求 / 逐角色 LLM 可观测性 + Planner 工具审计（Harness Hardening 修复三）。

首次真实 GLM 运行里，这三个问题一个都答不出来：``events.jsonl`` 的 ``usage`` 全是
``null``（注册表没拿到 EventSink）、``by_model`` 只有一个聚合项（没有角色维度）、
``Planner.tool_records`` 只活在内存里（报告里的空列表会被读成"模型没调用工具"）。
这里的每一条断言都对应那三个问题之一。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codepilot.agents.planner import Planner
from codepilot.artifacts import ArtifactStore
from codepilot.llm.calls import CallRecorder, LLMRunContext
from codepilot.llm.fake import FakeLLM
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.llm.types import LLMRequest, LLMResponse, Message
from codepilot.observability.events import EventSink
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.schemas.enums import RiskLevel
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.localization import (
    FileDecision,
    LocalizationResult,
    SymbolDecision,
)
from codepilot.schemas.retrieval import RankedFile, RankedSymbol
from codepilot.schemas.usage import Usage
from codepilot.storage.repository import Stores
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.read_tools import find_symbol_tool, read_file_tool
from codepilot.tools.registry import ToolRegistry

SOURCE = '''"""Ledger under test."""


class Ledger:
    total = 0

    def add(self, amount):
        self.total += amount
        return self
'''

TESTS = """from src.budget.ledger import Ledger


def test_add_positive():
    assert Ledger().add(2).total == 2
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "obs-repo"
    (root / "src" / "budget").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "budget" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "budget" / "ledger.py").write_text(SOURCE, encoding="utf-8")
    (root / "tests" / "test_ledger.py").write_text(TESTS, encoding="utf-8")
    return root


def _tracking(
    usage: Usage,
    recorder: CallRecorder,
    *,
    role: str,
    replies: list[Any],
    provider: str = "glm",
) -> tuple[UsageTrackingProvider, FakeLLM]:
    fake = FakeLLM(
        replies,
        model_alias=f"{role}-model",
        input_tokens=100,
        output_tokens=20,
        cost_per_call=0.01,
    )
    return (
        UsageTrackingProvider(fake, usage=usage, role=role, provider=provider, recorder=recorder),
        fake,
    )


# --------------------------------------------------------------------- 逐请求


def test_every_request_is_logged_with_role_node_and_attempt(tmp_path: Path) -> None:
    usage = Usage(currency="CNY")
    context = LLMRunContext(run_id="run-9", task_id="task-9", node="plan", iteration=0)
    recorder = CallRecorder(path=tmp_path / "llm_requests.jsonl", context=context)
    provider, _ = _tracking(
        usage,
        recorder,
        role="planner",
        replies=[{"plan": 1}, {"plan": 2}],
    )
    provider.complete(_request("planner", attempt=1))
    provider.complete(_request("planner", attempt=2, correction=True))

    lines = [
        json.loads(line)
        for line in (tmp_path / "llm_requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [item["seq"] for item in lines] == [1, 2]
    assert [item["attempt"] for item in lines] == [1, 2]
    assert [item["correction"] for item in lines] == [False, True]
    assert {item["role"] for item in lines} == {"planner"}
    assert {item["node"] for item in lines} == {"plan"}
    assert {item["run_id"] for item in lines} == {"run-9"}
    assert all(item["status"] == "ok" for item in lines)
    assert all(item["call_id"].startswith("llm-") for item in lines)
    assert all(item["provider"] == "glm" for item in lines)


def test_role_aggregates_reconcile_with_the_total_ledger(tmp_path: Path) -> None:
    """分角色累计必须等于总账——否则"贵在哪个环节"与"一共花了多少"是两个故事。"""
    usage = Usage(currency="CNY")
    context = LLMRunContext(run_id="r", task_id="t")
    recorder = CallRecorder(path=tmp_path / "llm_requests.jsonl", context=context)
    planner, _ = _tracking(usage, recorder, role="planner", replies=[{"a": 1}, {"b": 2}])
    coder, _ = _tracking(usage, recorder, role="coder", replies=[{"c": 3}])
    reviewer, _ = _tracking(usage, recorder, role="reviewer", replies=[{"d": 4}])

    context.node = "plan"
    planner.complete(_request("planner"))
    planner.complete(_request("planner"))
    context.node = "generate_patch"
    context.iteration = 1
    coder.complete(_request("code"))
    context.node = "review"
    reviewer.complete(_request("review"))

    assert usage.llm_calls == 4
    assert usage.role_totals_reconcile()
    summary = recorder.summary()
    assert summary["total_requests"] == 4
    assert summary["by_role"]["planner"]["requests"] == 2
    assert summary["by_role"]["coder"]["input_tokens"] == 100
    assert sum(item["input_tokens"] for item in summary["by_role"].values()) == usage.input_tokens
    assert sum(item["output_tokens"] for item in summary["by_role"].values()) == (
        usage.output_tokens
    )
    # 节点归属来自图，不是猜的：每次请求都能定位到它发生在哪个节点。
    nodes = {item.role: item.node for item in recorder.history()}
    assert nodes == {"planner": "plan", "coder": "generate_patch", "reviewer": "review"}


def test_cached_tokens_and_finish_reason_are_recorded(tmp_path: Path) -> None:
    usage = Usage()
    context = LLMRunContext(run_id="r", task_id="t")
    recorder = CallRecorder(path=tmp_path / "llm_requests.jsonl", context=context)
    fake = FakeLLM(
        [
            LLMResponse(
                content="ok",
                input_tokens=1000,
                output_tokens=5,
                cached_input_tokens=768,
                cost=0.5,
                cost_known=True,
                model_alias="glm-main",
                finish_reason="length",
            )
        ]
    )
    provider = UsageTrackingProvider(fake, usage=usage, role="coder", recorder=recorder)
    provider.complete(_request("code"))

    record = recorder.history()[0]
    assert record.cached_tokens == 768
    assert record.finish_reason == "length"
    assert record.total_tokens == 1005
    assert usage.cached_input_tokens == 768


def test_unknown_cost_is_not_recorded_as_free(tmp_path: Path) -> None:
    """``cost_known=False`` 时总账只是**下界**，并且这个事实要能被查到。"""
    usage = Usage()
    context = LLMRunContext(run_id="r", task_id="t")
    recorder = CallRecorder(path=tmp_path / "llm_requests.jsonl", context=context)
    fake = FakeLLM(
        [
            LLMResponse(
                content="ok",
                input_tokens=10,
                output_tokens=1,
                cost=0.0,
                cost_known=False,
                model_alias="glm-main",
            )
        ]
    )
    UsageTrackingProvider(fake, usage=usage, role="planner", recorder=recorder).complete(
        _request("planner")
    )

    assert usage.cost == 0.0
    assert usage.cost_unknown_calls == 1
    assert usage.cost_is_lower_bound
    assert recorder.history()[0].cost_known is False
    assert recorder.summary()["by_role"]["planner"]["cost_unknown_requests"] == 1
    # 按角色的账仍然与总账一致：未知费用不改变 token 计数口径。
    assert usage.role_totals_reconcile()


def test_provider_failure_and_uncertain_request_are_distinguished(tmp_path: Path) -> None:
    from codepilot.llm.errors import LLMError, ProviderError, RateLimitError

    usage = Usage()
    context = LLMRunContext(run_id="r", task_id="t")
    recorder = CallRecorder(path=tmp_path / "llm_requests.jsonl", context=context)
    provider, _ = _tracking(
        usage,
        recorder,
        role="coder",
        replies=[RateLimitError("429"), ProviderError("请求超时: x", uncertain=True)],
    )
    for _ in range(2):
        with pytest.raises(LLMError):
            provider.complete(_request("code"))

    statuses = [item.status for item in recorder.history()]
    assert statuses == ["error", "uncertain"]
    assert recorder.summary()["by_role"]["coder"]["uncertain"] == 1
    # 失败的调用也计数：token 已经消耗掉了。
    assert usage.llm_calls == 2
    assert all(item.cost_known is False for item in recorder.history())


def test_currencies_are_never_summed_into_one_number() -> None:
    usage = Usage(currency="CNY")
    usage.add_call(alias="m", input_tokens=1, output_tokens=1, cost=1.0, role="planner")
    with pytest.raises(ValueError, match="不能把"):
        usage.add_call(
            alias="m", input_tokens=1, output_tokens=1, cost=1.0, role="planner", currency="USD"
        )


def test_records_never_contain_prompt_or_secrets(tmp_path: Path) -> None:
    """默认只记元数据。Prompt 正文与密钥属于显式开启的录制，不属于逐请求日志。"""
    usage = Usage()
    context = LLMRunContext(run_id="r", task_id="t")
    recorder = CallRecorder(path=tmp_path / "llm_requests.jsonl", context=context)
    provider, _ = _tracking(usage, recorder, role="planner", replies=[{"a": 1}])
    provider.complete(
        _request("planner", content="请读 src/budget/ledger.py，api_key=sk-abcdefgh123456789")
    )
    text = (tmp_path / "llm_requests.jsonl").read_text(encoding="utf-8")
    assert "sk-abcdefgh123456789" not in text
    assert "budget/ledger.py" not in text
    assert json.loads(text.splitlines()[0])["role"] == "planner"


def _request(purpose: str, *, attempt: int = 1, correction: bool = False, content: str = "hi"):
    return LLMRequest(
        messages=[Message.user(content)],
        purpose=purpose,
        attempt=attempt,
        correction=correction,
    )


# --------------------------------------------------------- 事件流与 record 开关


def test_events_carry_usage_once_the_sink_is_bound(tmp_path: Path) -> None:
    """真实 GLM 那次运行里每条事件的 usage 都是 null；这一步就是接上它。"""
    usage = Usage()
    context = LLMRunContext(run_id="run-1", task_id="task-1")
    recorder = CallRecorder(path=tmp_path / "llm_requests.jsonl", context=context)
    sink = EventSink(run_id="run-1", task_id="task-1", jsonl_path=tmp_path / "events.jsonl")
    provider, _ = _tracking(usage, recorder, role="planner", replies=[{"a": 1}])
    provider.bind_sink(sink)
    provider.complete(_request("planner"))
    sink.close()

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    llm_event = next(item for item in events if item["type"] == "llm_call")
    assert llm_event["usage"] is not None
    assert llm_event["usage"]["input_tokens"] == 100
    assert llm_event["usage"]["currency"] == "CNY"
    assert llm_event["extra"]["role"] == "planner"
    assert llm_event["extra"]["call_id"].startswith("llm-")


def test_live_bundle_uses_record_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``settings.record_llm`` 以前只被 CLI 打印出来，从没接到真实执行路径上。"""
    from codepilot.eval.assembly import ProviderBundle
    from codepilot.llm.config import KNOWN_ROLES, LLMConfig
    from codepilot.llm.registry import LLMMode

    monkeypatch.setenv("OBS_KEY", "sk-test")
    config = LLMConfig.model_validate(
        {
            "providers": {
                "z": {"name": "z", "base_url": "https://fake.test/v1", "api_key_env": "OBS_KEY"}
            },
            "models": {"m": {"alias": "m", "provider": "z", "model": "glm-4.7"}},
            "roles": dict.fromkeys(KNOWN_ROLES, "m"),
            "exchange_rates": {"CNY": 1.0},
        }
    )
    bundle = ProviderBundle.live(config, budget_currency="CNY", record_llm=True)
    assert bundle.registry is not None
    assert bundle.registry.mode is LLMMode.RECORD
    assert not bundle.simulated
    assert bundle.model_identity.startswith("localizer=")
    bundle.close()


# ------------------------------------------------------------- Planner 工具审计


def _planner_setup(
    repo: Path, stores: Stores, replies: list[Any]
) -> tuple[Planner, RepositoryIndex, EventSink, ArtifactStore]:
    workspace = Workspace(
        path=repo,
        source=normalize_source(str(repo)),
        base_commit="0" * 40,
        repository_id="obs_repo",
    )
    Indexer().index(stores, workspace)
    index = RepositoryIndex(stores, workspace.repository_id)
    policy = WorkspacePathPolicy(repo)
    registry = ToolRegistry([read_file_tool(policy), find_symbol_tool(index)])
    artifacts = ArtifactStore(repo.parent / "artifacts", "run-obs")
    events = EventSink(
        run_id="run-obs", task_id="task-obs", jsonl_path=artifacts.run_dir / "events.jsonl"
    )
    context = LLMRunContext(run_id="run-obs", task_id="task-obs", node="plan")
    recorder = CallRecorder(path=artifacts.run_dir / "llm_requests.jsonl", context=context)
    usage = Usage()
    fake = FakeLLM(replies)
    tracked = UsageTrackingProvider(
        fake, usage=usage, sink=events, role="planner", node="plan", recorder=recorder
    )
    planner = Planner(
        tracked,
        registry,
        policy,
        index,
        events=events,
        artifacts=artifacts,
        llm_context=context,
    )
    return planner, index, events, artifacts


def _localization(index: RepositoryIndex, repo: Path) -> LocalizationResult:
    symbol = next(
        item for item in index.symbol_objects() if item.qualified_name.endswith("Ledger.add")
    )
    return LocalizationResult(
        files=[RankedFile(path=symbol.path, score=1.0, top_symbols=[symbol.qualified_name])],
        symbols=[
            RankedSymbol(
                symbol_id=symbol.id,
                qualified_name=symbol.qualified_name,
                file=symbol.path,
                score=1.0,
                kind=symbol.kind,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                reason="named in issue",
            )
        ],
        file_decisions=[FileDecision(candidate_id="f1", path=symbol.path, reason="named in issue")],
        symbol_decisions=[
            SymbolDecision(
                symbol_id=symbol.id,
                file=symbol.path,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                file_line_count=len((repo / symbol.path).read_text(encoding="utf-8").splitlines()),
                reason="exact",
            )
        ],
        retrieval_snapshot_id="retrieval-sha256-" + "0" * 64,
    )


_PLAN_REPLY: dict[str, Any] = {
    "root_cause_hypothesis": "amount is not validated",
    "changes": [
        {
            "file": "src/budget/ledger.py",
            "action": "modify",
            "description": "reject non-positive amounts",
        }
    ],
    "tests_to_run": ["tests/test_ledger.py::test_add_positive"],
    "risk": "low",
    "alternatives": [],
}


def test_planner_multi_round_tool_calls_are_audited(
    repo: Path, stores: Stores, tmp_path: Path
) -> None:
    planner, index, events, artifacts = _planner_setup(
        repo,
        stores,
        [
            {"tool": "read_file", "arguments": {"path": "src/budget/ledger.py"}},
            {"tool": "find_symbol", "arguments": {"name": "Ledger"}},
            _PLAN_REPLY,
        ],
    )
    plan = planner.plan(
        issue_text="non-positive amounts are accepted",
        issue_analysis=IssueAnalysis(summary="non-positive amounts are accepted"),
        localization=_localization(index, repo),
        impact=ImpactAnalysis(risk=RiskLevel.LOW, related_tests=[]),
    )
    audit = planner.write_audit()
    events.close()

    assert plan.changes
    assert audit.requested == 2
    assert audit.succeeded == 2
    assert audit.requested_but_denied == 0
    assert audit.records_present is True

    written = json.loads((artifacts.run_dir / "plan_tools.json").read_text("utf-8"))
    assert len(written["records"]) == 2
    assert written["requested_but_denied"] == 0
    assert {item["name"] for item in written["records"]} == {"read_file", "find_symbol"}
    # 审计里是脱敏摘要，不带原始参数对象，也不带文件正文。
    assert all(
        "arguments_summary" in item and "arguments" not in item for item in written["records"]
    )

    event_lines = [
        json.loads(line)
        for line in (artifacts.run_dir / "events.jsonl").read_text("utf-8").splitlines()
    ]
    tool_events = [item for item in event_lines if item["type"] == "tool_call"]
    assert len(tool_events) == 2
    assert all(item["node"] == "plan" for item in tool_events)
    llm_events = [item for item in event_lines if item["type"] == "llm_call"]
    assert len(llm_events) == 3, "两轮工具 + 一次成稿，每次都该有 usage"
    assert all(item["usage"] is not None for item in llm_events)


def test_denied_tool_request_is_visible_as_denied_not_absent(
    repo: Path, stores: Stores, tmp_path: Path
) -> None:
    """模型请求了未注册给 planner 的工具：要记成"被拒"，不是"没发生"。"""
    planner, index, events, artifacts = _planner_setup(
        repo,
        stores,
        [
            {"tool": "edit_file", "arguments": {"path": "src/budget/ledger.py"}},
            _PLAN_REPLY,
        ],
    )
    planner.plan(
        issue_text="non-positive amounts",
        issue_analysis=IssueAnalysis(summary="non-positive amounts"),
        localization=_localization(index, repo),
        impact=ImpactAnalysis(risk=RiskLevel.LOW, related_tests=[]),
    )
    audit = planner.write_audit()
    events.close()

    assert audit.requested == 1
    assert audit.requested_but_denied == 1
    assert audit.succeeded == 0
    assert audit.records[0].error_code == "unregistered_tool"
    assert (artifacts.run_dir / "plan_tools.json").is_file()
    # 只读契约没有被绕过。
    assert (repo / "src" / "budget" / "ledger.py").read_text("utf-8") == SOURCE


def test_empty_audit_is_written_and_marked_present(
    repo: Path, stores: Stores, tmp_path: Path
) -> None:
    """一条工具调用都没有时，仍然要落一份"审计存在、计数为 0"的证据。"""
    planner, index, events, artifacts = _planner_setup(repo, stores, [_PLAN_REPLY])
    planner.plan(
        issue_text="non-positive amounts",
        issue_analysis=IssueAnalysis(summary="non-positive amounts"),
        localization=_localization(index, repo),
        impact=ImpactAnalysis(risk=RiskLevel.LOW, related_tests=[]),
    )
    audit = planner.write_audit()
    events.close()

    assert audit.records == []
    summary = audit.summary()
    assert summary["records_present"] is True
    assert summary["requested"] == 0
    assert (artifacts.run_dir / "plan_tools.json").is_file()
