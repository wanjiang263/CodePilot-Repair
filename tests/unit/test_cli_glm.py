"""CLI 层的 GLM 接入：``llm smoke`` 命令与 bench/solve 的生效配置装配。

这里刻意不测"函数返回值对不对"（那是 test_glm_config / test_glm_provider 的事），
只测**操作者真的能按文档把这些命令跑起来**：参数名、退出码、拒绝措辞，以及
live 跑批的配置指纹是否与真正跑起来的是同一份。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from codepilot.cli import app
from codepilot.llm.glm import GLM_API_KEY_ENV

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()
KEY = "cli-smoke-key-abc123"


@pytest.fixture(autouse=True)
def no_repo_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把 ``.env`` 指向空目录：本机有没有真密钥都不该影响这些断言。"""
    import codepilot.config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", tmp_path)
    app_config._dotenv_values.cache_clear()
    monkeypatch.delenv(GLM_API_KEY_ENV, raising=False)
    yield
    app_config._dotenv_values.cache_clear()


def _reply(content: str = "收到") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "glm-4.7",
            "choices": [
                {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5},
        },
    )


def _mock_transport_client(monkeypatch: pytest.MonkeyPatch, *responses: httpx.Response) -> list:
    """让 CLI 建出来的 provider 走 MockTransport，并且记下每一次请求。"""
    seen: list[httpx.Request] = []
    queue = list(responses) or [_reply()]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    import codepilot.llm.registry as registry_module

    def factory(provider_spec: Any, api_key: str) -> httpx.Client:
        del api_key
        return httpx.Client(transport=httpx.MockTransport(handler), base_url=provider_spec.base_url)

    monkeypatch.setattr(registry_module, "_default_client", factory)
    return seen


# --------------------------------------------------------------- llm smoke


def test_smoke_defaults_to_dry_run_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不给 ``--live`` 就一个请求都不发 —— 这条命令的默认必须是安全的。"""

    def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("dry-run 不该构造 HTTP 客户端")

    monkeypatch.setattr("codepilot.llm.registry._default_client", explode)
    result = runner.invoke(app, ["llm", "smoke", "--alias", "glm-main"])

    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "NOT_RUN" in result.output
    assert "合计：0 次 HTTP" in result.output


def test_smoke_live_without_cost_ceiling_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    _mock_transport_client(monkeypatch)
    result = runner.invoke(app, ["llm", "smoke", "--alias", "glm-main", "--live"])

    assert result.exit_code == 1
    assert "--max-cost" in result.output + (result.stderr or "")


def test_smoke_live_reports_all_three_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """三阶段各自给出结论；工具阶段要看到真实执行过 read_file。"""
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    seen = _mock_transport_client(
        monkeypatch,
        _reply("收到"),
        _reply(
            json.dumps(
                {"verdict": "pass", "summary": "可用", "evidence": [], "confidence": 0.5},
                ensure_ascii=False,
            )
        ),
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "notes.txt"}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5},
            },
        ),
        _reply("MARKER=GLM-SMOKE-7QX4"),
    )
    result = runner.invoke(
        app, ["llm", "smoke", "--alias", "glm-main", "--live", "--max-cost", "0.05"]
    )

    assert result.exit_code == 0, result.output
    assert "✓ text" in result.output
    assert "✓ structured" in result.output
    assert "✓ tools" in result.output
    assert len(seen) == 4
    assert "按配置估算" not in result.output  # 费用由单价算出，不是含糊的估算口径


def test_smoke_json_output_carries_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    _mock_transport_client(monkeypatch, httpx.Response(401, json={"message": f"bad {KEY}"}))
    result = runner.invoke(
        app,
        [
            "llm",
            "smoke",
            "--alias",
            "glm-main",
            "--live",
            "--max-cost",
            "1",
            "--stage",
            "text",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert KEY not in json.dumps(payload, ensure_ascii=False)
    assert payload["stages"][0]["status"] == "FAIL"
    assert payload["cost_known"] is False, "失败调用的费用必须报成未知"


def test_smoke_distinguishes_missing_key_from_model_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺密钥是配置错误，不是"模型不行"：措辞与退出原因都要分开。"""
    monkeypatch.delenv(GLM_API_KEY_ENV, raising=False)
    result = runner.invoke(
        app, ["llm", "smoke", "--alias", "glm-main", "--live", "--max-cost", "1"]
    )
    assert result.exit_code == 1
    assert GLM_API_KEY_ENV in result.stdout
    assert "NOT_RUN" in result.stdout


def test_smoke_blocks_usd_alias_without_exchange_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    result = runner.invoke(
        app, ["llm", "smoke", "--alias", "glm-5-zai", "--live", "--max-cost", "1"]
    )
    assert result.exit_code == 1
    assert "汇率" in result.stdout

    # 换成 USD 预算就放行：币种一致时不需要汇率，而不是继续按 1.0 混算。
    seen = _mock_transport_client(monkeypatch)
    ok = runner.invoke(
        app,
        [
            "llm",
            "smoke",
            "--alias",
            "glm-5-zai",
            "--live",
            "--max-cost",
            "1",
            "--stage",
            "text",
            "--budget-currency",
            "USD",
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert len(seen) == 1


def test_smoke_platform_flag_switches_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    seen = _mock_transport_client(monkeypatch)
    result = runner.invoke(
        app,
        [
            "llm",
            "smoke",
            "--alias",
            "glm-5-zai",
            "--glm-platform",
            "zai",
            "--live",
            "--max-cost",
            "1",
            "--stage",
            "text",
            "--budget-currency",
            "USD",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen[0].url.host == "api.z.ai"
    assert seen[0].url.path == "/api/paas/v4/chat/completions"


def test_smoke_refuses_to_guess_among_several_aliases() -> None:
    result = runner.invoke(app, ["llm", "smoke", "--alias", "x", "--provider", "zai"], color=False)
    assert result.exit_code == 2
    assert "--alias" in result.output


# --------------------------------------------------------------- 生效配置


def test_effective_config_applies_glm_env_and_role_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codepilot.cli import _effective_llm_config

    monkeypatch.setenv("GLM_PLATFORM", "zai")
    monkeypatch.setenv("GLM_MODEL", "glm-5.1")
    config = _effective_llm_config(alias="glm-5-zai")

    assert config.providers["zai"].base_url == "https://api.z.ai/api/paas/v4"
    assert config.models["glm-5-zai"].model == "glm-5.1"
    assert set(config.roles.values()) == {"glm-5-zai"}


def test_effective_config_rejects_unknown_alias() -> None:
    from codepilot.cli import _effective_llm_config

    with pytest.raises(KeyError, match="不在配置中"):
        _effective_llm_config(alias="no-such-alias")


# --------------------------------------------------------------- bench live


_CAPTURED: list[Any] = []


class _StopAfterAssemblyError(RuntimeError):
    """装配完成后立刻停下：真实的 run() 会派发付费调用。"""


class _StubRunner:
    """记录配置、什么都不跑的 BenchmarkRunner 替身。"""

    def __init__(self, *, config: Any, **_kwargs: Any) -> None:
        _CAPTURED.append(config)
        self.run_dir = Path("artifacts/stub-run")

    def run(self) -> Any:
        raise _StopAfterAssemblyError


def _bench_arguments(extra: list[str]) -> list[str]:
    return ["bench", "--seeds", "1", "--fake-sandbox", "--allow-live-llm", *extra]


@pytest.fixture
def validated_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """给 E-02 校验记录门放一个通行的返回值。

    ``--require-validated`` 在 CLI 上**只有正向开关**（``--no-require-validated`` 并不
    存在），而校验记录目录在 ``artifacts/`` 里、CI 上不存在。这批测试只关心 provider
    装配，所以把这道门按"当前套件全部已校验"来伪造；那道门自己的测试在 Phase 10。
    """
    import codepilot.cli as cli
    from codepilot.eval.synthetic import validate_suite

    def fake(directory: Path) -> dict[str, str]:
        del directory
        loaded = validate_suite(Path("benchmarks/synthetic"))
        return {item.task_id: "sha-fixture" for item in loaded.fixtures}

    monkeypatch.setattr(cli, "_validated_fixture_versions", fake)


def test_bench_live_reads_yaml_not_json(
    monkeypatch: pytest.MonkeyPatch, validated_fixtures: None
) -> None:
    """``--allow-live-llm`` 曾经对 YAML 做 ``json.loads``，直接报错。

    这条测试存在的全部理由是：live 跑批在 Phase 10 从未真的跑过（E-10 是 NOT RUN），
    所以这条路径上没有任何东西踩过。它坏的表征是"配置读取失败"，看起来像 YAML
    写坏了，其实是代码用错了加载器。
    """
    import codepilot.cli as cli

    _CAPTURED.clear()
    monkeypatch.setattr(cli, "BenchmarkRunner", _StubRunner)

    result = runner.invoke(app, _bench_arguments([]))
    # write_benchmark_report 在函数内部 import，因此只能靠"run() 抛出后不再往下走"
    # 来停下：_StopAfterAssemblyError 出现就说明装配已经完成、派发尚未开始。
    assert isinstance(result.exception, _StopAfterAssemblyError), (
        f"装配没有走到派发任务这一步：{result.output!r} / {result.exception!r}"
    )
    config = _CAPTURED[0]
    assert "configs/llm.yaml" not in config.model_identity
    assert "deepseek-flash" in config.model_identity
    assert config.provider_config_hash != "unconfigured"


def test_bench_config_hash_tracks_the_effective_glm_config(
    monkeypatch: pytest.MonkeyPatch, validated_fixtures: None
) -> None:
    """GLM 覆盖生效时，指纹与身份必须跟着变。

    否则一次"换了模型 ID 的跑批"会在 manifest 里留下与历史批次完全相同的指纹，
    而两批分数其实来自不同的模型——这正是 Phase 10 定为不可复现的那一类事故。
    """
    import codepilot.cli as cli

    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    _CAPTURED.clear()
    monkeypatch.setattr(cli, "BenchmarkRunner", _StubRunner)

    assert isinstance(runner.invoke(app, _bench_arguments([])).exception, _StopAfterAssemblyError)
    monkeypatch.setenv("GLM_MODEL", "glm-5.1")
    assert isinstance(
        runner.invoke(app, _bench_arguments(["--llm-model", "glm-5-main"])).exception,
        _StopAfterAssemblyError,
    )

    first, second = _CAPTURED
    assert first.model_identity != second.model_identity
    assert "glm-5" in second.model_identity
    assert first.provider_config_hash != second.provider_config_hash


def test_bench_refuses_to_mix_currencies(
    monkeypatch: pytest.MonkeyPatch, validated_fixtures: None
) -> None:
    """角色指向 USD 计价模型而预算仍是 CNY 且无汇率：拒绝开跑，不产生任何调用。"""
    import codepilot.cli as cli

    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    calls: list[Any] = []
    monkeypatch.setattr(cli, "BenchmarkRunner", lambda **kw: calls.append(kw))

    result = runner.invoke(app, _bench_arguments(["--llm-model", "glm-5-zai"]))

    assert result.exit_code == 2
    assert "汇率" in result.output
    assert calls == []


def test_repo_dotenv_stays_untracked() -> None:
    """密钥落盘的位置必须被 Git 忽略：这是整条凭证策略唯一的外部依赖。"""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    ).stdout.splitlines()
    assert ".env" not in tracked
    ignored = subprocess.run(
        ["git", "check-ignore", ".env"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert ignored.returncode == 0
