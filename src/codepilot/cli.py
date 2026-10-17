"""命令行入口（F-17）。

Phase 0 只把 ``config`` 与 ``doctor`` 做实。其余子命令**注册但不假装可用**：
它们会打印"尚未实现 + 计划阶段"并以退出码 2 结束。

为什么不写成看起来正常的空实现：03 §2.2 明确禁止把未完成的实现呈现为完成。
一个静默返回 0 的 ``solve`` 比不存在的 ``solve`` 危险得多——CI 里它会一路
绿灯，直到有人真的去看产物。
"""

from __future__ import annotations

import json
import secrets
import shlex
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import typer

from codepilot.artifacts import ArtifactStore
from codepilot.config import PROJECT_ROOT, get_settings
from codepilot.eval.assembly import role_model_identity
from codepilot.eval.runner import BenchmarkRunner
from codepilot.observability.logging import configure_logging
from codepilot.observability.redaction import redact, redact_text
from codepilot.repo.workspace import WorkspaceManager

if TYPE_CHECKING:
    from codepilot.llm.config import LLMConfig
    from codepilot.llm.smoke import SmokeReport
    from codepilot.repo.index import IndexStats

__all__ = ["app"]

#: 尚未实现的子命令 → 计划落地的阶段。值会出现在报错信息里。
#: ``index`` 已在 R-18 落地，不再列在这里——``test_unimplemented_commands_fail_loudly``
#: 以 ``sorted(_PLANNED)`` 参数化，删键即自动不再要求 index 退出码 2。
_PLANNED: dict[str, str] = {
    "report": "Phase 13（Run Report 与 Dashboard）",
}

app = typer.Typer(
    name="codepilot",
    help="仓库级自主修复 Agent。给定仓库、base commit 与 Issue，自主完成定位、修复与验证。",
    no_args_is_help=True,
    add_completion=False,
)

config_app = typer.Typer(name="config", help="查看与校验配置。", no_args_is_help=True)
app.add_typer(config_app, name="config")

llm_app = typer.Typer(name="llm", help="LLM Provider 的体检与冒烟。", no_args_is_help=True)
app.add_typer(llm_app, name="llm")


def _effective_llm_config(*, alias: str | None = None, platform: str | None = None) -> LLMConfig:
    """``configs/llm.yaml`` → 应用 GLM 运行期覆盖 → 可选地把所有角色绑到同一别名。

    所有需要真实 provider 的入口都走这里，而不是各自 ``load_llm_config``：否则
    ``GLM_PLATFORM`` / ``GLM_MODEL`` 只对某一条命令生效，而报表里的
    ``provider_config_hash`` 算的是另一份配置——那正是"跑的和记的不是一份东西"。
    """
    from codepilot.llm.config import load_llm_config
    from codepilot.llm.glm import apply_glm_env

    config = apply_glm_env(load_llm_config(get_settings().resolved_llm_config), platform=platform)
    if alias is not None:
        config = config.bind_all_roles(alias)
    return config


def _version() -> str:
    try:
        return _pkg_version("codepilot-repair")
    except PackageNotFoundError:  # 直接从源码运行、未安装时
        return "0.1.0+source"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"codepilot {_version()}")
        raise typer.Exit


@app.callback()
def main(
    # 这个参数不由函数体使用：``--version`` 由 callback 在 eager 阶段处理并
    # 直接退出，因此函数体永远看不到 True 的情况。保留参数是为了让 Typer
    # 注册这个选项。
    version: bool = typer.Option(  # noqa: ARG001
        False,
        "--version",
        "-V",
        help="显示版本号后退出。",
        callback=_version_callback,
        is_eager=True,
    ),
    log_level: str = typer.Option("INFO", "--log-level", help="日志级别。"),
) -> None:
    """全局选项。"""
    configure_logging(log_level)


# ---------------------------------------------------------------- config


@config_app.command("check")
def config_check(
    strict: bool = typer.Option(
        False,
        "--strict",
        help="配置不完整时以退出码 1 结束（供 CI 使用）。",
    ),
) -> None:
    """检查 LLM 配置：缺密钥、未填模型 ID、未填单价、角色未映射。

    ⚠️ 未填单价不会阻止运行——费用会记为 0 并标记 cost_known=False，
    但 Run Report 里会如实标注，不会伪装成"零成本"。
    """
    from codepilot.llm.config import load_llm_config

    settings = get_settings()
    path = settings.resolved_llm_config

    try:
        config = load_llm_config(path)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(f"✗ 无法加载配置: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    report = config.check()
    typer.echo(f"配置文件: {path}")
    typer.echo(report.render())

    if report.ready:
        typer.secho("✓ 配置完整。", fg=typer.colors.GREEN)
        return

    if strict:
        typer.secho("✗ 配置不完整（--strict）。", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(
        "配置尚不完整。填写完成后可用 --strict 让 CI 把关。",
        fg=typer.colors.YELLOW,
    )


@config_app.command("show")
def config_show() -> None:
    """打印生效的进程级配置（不含任何密钥值）。"""
    settings = get_settings()
    typer.echo(f"项目根目录      : {PROJECT_ROOT}")
    typer.echo(f"配置目录        : {settings.config_dir}")
    typer.echo(f"LLM 配置        : {settings.resolved_llm_config}")
    typer.echo(f"产物目录        : {settings.artifacts_dir}")
    typer.echo(f"缓存目录        : {settings.cache_dir}")
    typer.echo(f"数据库          : {settings.database_url}")
    typer.echo(f"API 监听        : {settings.api_host}:{settings.api_port}")
    typer.echo(f"API key 已配置  : {'是' if settings.api_key else '否'}")
    typer.echo(f"录制 LLM 调用   : {settings.record_llm}")
    typer.echo(f"回放文件        : {settings.replay_llm_path or '（未设置）'}")


# ---------------------------------------------------------------- llm


@llm_app.command("smoke")
def llm_smoke(
    alias: str = typer.Option(
        ..., "--alias", help="configs/llm.yaml 里的模型别名，例如 glm-main 或 glm-5-zai。"
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="等价写法：给定 provider 名时用它下面唯一的别名。"
    ),
    platform: str | None = typer.Option(
        None, "--glm-platform", help="GLM 平台：zai 或 bigmodel（覆盖 GLM_PLATFORM）。"
    ),
    model: str | None = typer.Option(
        None, "--model", help="覆盖该 provider 下所有别名的官方模型 ID。"
    ),
    stage: list[str] = typer.Option(
        [], "--stage", help="只跑指定阶段：text / structured / tools。可重复；默认全跑。"
    ),
    live: bool = typer.Option(
        False, "--live", help="发起**真实**请求并产生费用。不传则只做 dry-run，一个字节都不发。"
    ),
    max_output_tokens: int = typer.Option(
        512, "--max-output-tokens", min=1, help="每次请求的输出上限（真实调用必须有限）。"
    ),
    max_cost: float | None = typer.Option(
        None, "--max-cost", help="费用上限（预算币种）。live 必须给出，否则拒绝执行。"
    ),
    timeout_seconds: float | None = typer.Option(
        None, "--timeout-seconds", help="覆盖 provider 超时（等价 GLM_TIMEOUT_SECONDS）。"
    ),
    max_retries: int | None = typer.Option(
        None, "--max-retries", help="覆盖 provider 重试次数（等价 GLM_MAX_RETRIES）。"
    ),
    budget_currency: str | None = typer.Option(
        None, "--budget-currency", help="本次冒烟的费用币种；默认取配置的 budget_currency。"
    ),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读的 JSON 结果。"),
) -> None:
    """对单个 provider/模型跑三阶段冒烟：普通对话、结构化输出、函数调用。

    默认 **dry-run**：不发任何请求，只报告将要执行什么。三条前置条件
    （显式 ``--live``、有限输出上限、显式费用上限）里任何一条不满足，真实调用就不会发生，
    报告里如实写成 NOT RUN —— 而不是"试试看会不会失败"。

    \b
    例子：
      uv run codepilot llm smoke --alias glm-main                       # dry-run
      uv run codepilot llm smoke --alias glm-main --live --max-cost 0.05
    """
    from codepilot.llm.config import load_llm_config
    from codepilot.llm.glm import apply_glm_env
    from codepilot.llm.smoke import DEFAULT_STAGES, SmokeStage, run_smoke

    try:
        stages = tuple(SmokeStage(item.strip().lower()) for item in stage) or DEFAULT_STAGES
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    config = apply_glm_env(
        load_llm_config(get_settings().resolved_llm_config),
        platform=platform,
        model=model,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )

    selected_alias = alias
    if provider is not None:
        # 只给 provider 时替调用方挑别名，但多于一个就拒绝猜：猜错的那个
        # 会在报表里留下一个与实际操作者意图不同的模型 ID。
        candidates = sorted(a for a, s in config.models.items() if s.provider == provider)
        if not candidates:
            raise typer.BadParameter(f"provider {provider!r} 下没有模型别名")
        if len(candidates) > 1:
            raise typer.BadParameter(
                f"provider {provider!r} 有多个别名：{', '.join(candidates)}；请用 --alias 指定一个"
            )
        selected_alias = candidates[0]

    report = run_smoke(
        config,
        alias=selected_alias,
        live=live,
        stages=stages,
        max_output_tokens=max_output_tokens,
        max_cost=max_cost,
        budget_currency=budget_currency,
    )

    if as_json:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        _render_smoke_report(report)

    # dry-run 不是失败：它的全部意义就是"在没有授权的情况下把要做什么说清楚"。
    # 只有真实冒烟没通过才用非零退出码，让 CI 与脚本能据此判断。
    if report.live and not report.passed:
        raise typer.Exit(code=1)


def _render_smoke_report(report: SmokeReport) -> None:
    from codepilot.llm.smoke import SmokeStatus

    marks = {SmokeStatus.PASS: "✓", SmokeStatus.FAIL: "✗", SmokeStatus.NOT_RUN: "-"}
    # "live" 只代表**已授权**，不代表真的花了钱：前置条件不满足时一个请求都不会发，
    # 把它写成"已产生费用"会让报表谎报支出。
    if not report.live:
        mode = "dry-run（未发出任何请求）"
    elif report.total_requests:
        mode = f"live（已产生 {report.total_requests} 次真实调用）"
    else:
        mode = "live（已授权，但前置条件未满足，未发出请求）"
    typer.echo(
        f"provider={report.provider} alias={report.model_alias} model={report.model_id}\n"
        f"base_url={report.base_url} 币种={report.currency} 模式={mode}"
    )
    for item in report.stages:
        typer.echo(f"  {marks[item.status]} {item.stage:<10} {item.status.value:<7} {item.detail}")
        for note in item.notes:
            typer.echo(f"      · {note}")
        if item.requests:
            cost = (
                f"费用 {item.cost:.6f} {item.currency}"
                if item.cost_known
                else "费用 UNKNOWN（未确认单价，或请求结果未知）"
            )
            typer.echo(
                f"      HTTP {item.requests} 次，"
                f"token {item.input_tokens} in / {item.output_tokens} out，{cost}"
            )
    if report.cost_known:
        typer.echo(
            f"合计：{report.total_requests} 次 HTTP，费用 {report.total_cost:.6f} {report.currency}"
        )
    else:
        typer.echo(f"合计：{report.total_requests} 次 HTTP，费用 UNKNOWN")
    if report.usage_uncertain:
        typer.echo(
            "  ⚠️ 有已发出但结果未知的请求：服务端可能已按生成量计费，因此上面的费用是下界而非账单。"
        )
    if report.refusal:
        typer.echo(f"  未执行原因：{report.refusal}", err=True)


# ---------------------------------------------------------------- doctor


@app.command("doctor")
def doctor() -> None:
    """检查本机环境能否跑通各阶段。

    存在的理由：这个项目的多个阶段依赖本机能力（Docker、git、ripgrep），
    缺一样时症状往往是运行到一半才以晦涩的方式暴露。
    """
    settings = get_settings()
    rows: list[tuple[str, bool, str]] = []

    rows.append(("Python >= 3.12", sys.version_info >= (3, 12), sys.version.split()[0]))

    git = shutil.which("git")
    rows.append(("git", git is not None, git or "未找到——Phase 1 起必需"))

    docker = shutil.which("docker")
    docker_note = docker or "未找到——Phase 4 起必需，沙箱相关验收将无法本地执行"
    rows.append(("docker", docker is not None, docker_note))

    rg = shutil.which("rg")
    rows.append(("ripgrep", rg is not None, rg or "未找到——ripgrep 检索通道不可用"))

    llm_path = settings.resolved_llm_config
    rows.append(("LLM 配置文件", llm_path.exists(), str(llm_path)))

    if docker:
        try:
            probe = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            ok = probe.returncode == 0
            # 先 strip 再挑：daemon 没起来时 stdout 是一个 "\n"——它是真值，
            # 不 strip 就用 `or` 会选中它，取 [0] 时越界（实测 Docker Desktop 未启动）。
            lines = (probe.stdout.strip() or probe.stderr.strip()).splitlines()
            detail = lines[0] if lines else ""
            rows.append(("Docker daemon 可用", ok, detail))
        except (subprocess.TimeoutExpired, OSError) as exc:
            rows.append(("Docker daemon 可用", False, f"探测失败: {exc}"))

    width = max(len(name) for name, _, _ in rows)
    for name, ok, detail in rows:
        mark = "✓" if ok else "✗"
        color = typer.colors.GREEN if ok else typer.colors.YELLOW
        typer.secho(f"{mark} {name.ljust(width)}  {detail}", fg=color)

    typer.echo()
    typer.echo("提示：docker 缺失时带 @pytest.mark.docker 的用例会自动 skip，")
    typer.echo("      相关验收项必须记为「未执行」，而不是「通过」。")


# ---------------------------------------------------------------- index (R-18)


@app.command("index")
def index(
    repo: str = typer.Argument(..., help="仓库路径或 URL。"),
    base_commit: str = typer.Option("HEAD", "--base-commit", help="基准提交（默认 HEAD）。"),
    database_url: str = typer.Option("", "--database-url", help="覆盖默认数据库 URL。"),
    mode: str = typer.Option(
        "fetch", "--mode", help="workspace 模式：fetch（默认）/ copy / worktree。"
    ),
    keep_workspace: bool = typer.Option(
        False, "--keep-workspace", help="保留临时 workspace 目录（默认索引完即删）。"
    ),
) -> None:
    """建立仓库索引：文件树、符号表、调用/依赖/继承/测试图，并打印四类计数。

    走的是与运行时完全相同的路径：先把仓库 clone 进共享缓存，再从缓存取出一份
    **临时 workspace**（默认 ``fetch`` 模式，看不到 base 之后的提交），索引它，
    结束后删掉。原目录绝不修改（02 §6）。
    """
    from codepilot.repo.index import Indexer
    from codepilot.repo.workspace import WorkspaceError, WorkspaceManager, WorkspaceMode
    from codepilot.storage.db import (
        create_engine_from_url,
        create_session_factory,
        init_db,
        session_scope,
    )
    from codepilot.storage.repository import Stores

    if mode == "fetch":
        ws_mode: WorkspaceMode = "fetch"
    elif mode == "copy":
        ws_mode = "copy"
    elif mode == "worktree":
        ws_mode = "worktree"
    else:
        typer.secho(
            f"✗ 未知的 workspace 模式: {mode!r}（可选 fetch / copy / worktree）",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    settings = get_settings()
    settings.ensure_dirs()
    engine = create_engine_from_url(database_url or settings.database_url)
    init_db(engine)
    factory = create_session_factory(engine)

    manager = WorkspaceManager(cache_root=settings.repo_cache_dir())
    workspace_root = settings.cache_dir / "cli-workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    target = workspace_root / f"index-{secrets.token_hex(6)}"

    try:
        try:
            workspace = manager.create(
                source=repo, base_commit=base_commit, target=target, mode=ws_mode
            )
        except WorkspaceError as exc:
            typer.secho(f"✗ 无法准备 workspace: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        try:
            with session_scope(factory) as session:
                stats = Indexer().index(Stores(session), workspace)
        finally:
            if keep_workspace:
                typer.echo(f"workspace 保留在: {workspace.path}")
            else:
                manager.remove(workspace)
    finally:
        engine.dispose()

    _render_index_stats(stats)


def _render_index_stats(stats: IndexStats) -> None:
    """打印 Files / Symbols（按类型）/ Edges（按类型与 confidence）/ Tests 四类计数。"""
    typer.echo(f"Repository  : {stats.repository_id}")
    typer.echo(f"Base commit : {stats.base_commit}")
    typer.echo(f"Index mode  : {'incremental' if stats.incremental else 'full'}")
    typer.echo(f"Duration    : {stats.duration_seconds:.2f}s")
    typer.echo()

    typer.echo(f"Files       : {stats.files}")
    skipped = {k: v for k, v in stats.scan.items() if k != "files" and v}
    if skipped:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items()))
        typer.echo(f"  scan skipped: {detail}")

    typer.echo("Symbols     :")
    for kind, count in sorted(stats.symbols_by_kind.items()):
        typer.echo(f"  {kind:<14} {count}")

    typer.echo("Edges       :")
    for (kind, confidence), count in sorted(stats.edges_by_kind_confidence.items()):
        typer.echo(f"  {kind:<10} {confidence:<7} {count}")

    typer.echo(f"Tests       : {stats.tests}")

    if stats.parse_errors:
        typer.echo()
        typer.secho(
            f"解析失败 {len(stats.parse_errors)} 个文件（已跳过其符号，未中断索引）：",
            fg=typer.colors.YELLOW,
        )
        for error in stats.parse_errors:
            typer.echo(f"  {error}")


# ---------------------------------------------------------------- 未实现的子命令


def _not_yet(command: str) -> None:
    phase = _PLANNED[command]
    typer.secho(
        f"✗ `{command}` 尚未实现。计划于 {phase} 落地。",
        fg=typer.colors.RED,
        err=True,
    )
    typer.secho(
        "  当前仓库只完成了 Phase 0（契约、配置、存储、可观测、LLM 调用层）。",
        fg=typer.colors.YELLOW,
        err=True,
    )
    typer.echo(f"  进度见 {PROJECT_ROOT / 'docs' / '05-精细化待办清单.md'}", err=True)
    raise typer.Exit(code=2)


def _looks_like_path(text: str) -> bool:
    """Decide whether ``--issue`` carried a file path rather than issue prose.

    ``Please fix handling of README.md`` is a one-line issue, not a missing file, so
    only separated, prefixed or single-token spellings ask for the path lookup. An
    existing path always wins that check because ``retrieve`` probes it first.
    """
    if "/" in text or "\\" in text:
        return True
    if text.startswith((".", "~")):
        return True
    return " " not in text.strip()


@app.command("retrieve")
def retrieve(
    query: str | None = typer.Argument(None, help="检索语句（兼容原有位置参数）。"),
    repo: str = typer.Option(..., "--repo", help="仓库路径或 URL。"),
    base_commit: str = typer.Option("HEAD", "--base-commit", help="基准提交。"),
    channel: str = typer.Option(
        "bm25", "--channel", help="逗号分隔：bm25/ripgrep/semantic/structural/test/history。"
    ),
    limit: int = typer.Option(20, "--limit", min=1, help="最多返回的 symbol 数量。"),
    issue: str | None = typer.Option(None, "--issue", help="Issue 文本或 UTF-8 文件路径。"),
    config: Path | None = typer.Option(
        None, "--config", help="检索 JSON 配置（channels/method/weights/embedding）。"
    ),
    ranking: str | None = typer.Option(None, "--ranking", help="weighted 或 rrf。"),
    embedding: str | None = typer.Option(
        None, "--embedding", help="none 或 offline-hash（非学习语义模型）。"
    ),
) -> None:
    """在隔离基准版本中检索，输出 Symbol / File 与完整通道评分 JSON。"""
    import json

    from codepilot.repo.gitcmd import GitError
    from codepilot.repo.workspace import WorkspaceError
    from codepilot.retrieval.config import CHANNELS
    from codepilot.retrieval.documents import RetrievalError
    from codepilot.retrieval.input import RetrievalInput
    from codepilot.retrieval.runner import RetrievalOptions, indexed_repository

    if (query is None) == (issue is None):
        raise typer.BadParameter("请提供位置参数 query 或 --issue，二者只能选一项")
    try:
        text = issue if issue is not None else query
        assert text is not None
        if issue is not None:
            file = Path(issue)
            if file.is_file():
                text = file.read_text(encoding="utf-8")
            elif (
                "\n" not in issue
                and file.suffix.lower() in {".md", ".txt"}
                and _looks_like_path(issue)
            ):
                raise ValueError(f"Issue 文件不存在: {file}")
        if not text.strip():
            raise ValueError("Issue 不能为空")
        if config is not None:
            if channel != "bm25":
                raise ValueError("--config 与 --channel 不能同时指定")
            options_data = json.loads(config.read_text(encoding="utf-8"))
            if not isinstance(options_data, dict):
                raise ValueError("配置必须是 JSON 对象")
        else:
            names = channel.split(",")
            if (
                not names
                or any(name not in CHANNELS for name in names)
                or len(set(names)) != len(names)
            ):
                raise ValueError(f"未知或重复通道: {channel}")
            options_data = {"channels": {name: name in names for name in CHANNELS}}
        if ranking is not None:
            options_data["method"] = ranking
        elif issue is not None and "method" not in options_data:
            options_data["method"] = "weighted"
        if embedding is not None:
            options_data["embedding"] = embedding
        options = RetrievalOptions.model_validate(options_data)
        task = RetrievalInput(task_id="cli", repo=repo, base_commit=base_commit, issue=text)
    except (ValueError, OSError) as error:
        raise typer.BadParameter(str(error)) from error
    settings = get_settings()
    settings.ensure_dirs()
    try:
        with indexed_repository(task, settings.cache_dir) as indexed:
            result, info = indexed.retrieve(task, options, limit=limit)
            if issue is None and config is None and ranking is None and embedding is None:
                typer.echo(result.model_dump_json(indent=2))
            else:
                data = result.model_dump(mode="json")
                data["execution"] = info.model_dump(mode="json")
                typer.echo(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
    except (WorkspaceError, RetrievalError, GitError, ValueError, OSError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error


@app.command("eval-retrieval")
def eval_retrieval(
    dataset: Path = typer.Option(..., "--dataset", help="合成检索评估 JSONL。"),
    output: Path | None = typer.Option(None, "--output", help="完整统计 / 配置 / 原始排名 JSON。"),
    embedding: str = typer.Option(
        "offline-hash", "--embedding", help="offline-hash（明确的非学习基线）或 none。"
    ),
    ranking: str = typer.Option("weighted", "--ranking", help="融合方法 weighted 或 rrf。"),
    config: Path | None = typer.Option(
        None, "--config", help="融合配置 JSON，独立消融行仍分别启用每个通道。"
    ),
) -> None:
    """运行六个独立通道与融合的离线 Recall@1/5/10 消融，保存可复现证据。"""
    from codepilot.retrieval.dataset import load_dataset
    from codepilot.retrieval.evaluation import render_table, run_evaluation
    from codepilot.retrieval.runner import RetrievalOptions
    from codepilot.schemas.retrieval import FusionMethod

    try:
        cases = load_dataset(dataset)
        if embedding not in {"none", "offline-hash"} or ranking not in {"weighted", "rrf"}:
            raise ValueError("embedding 必须为 offline-hash/none；ranking 必须为 weighted/rrf")
        options = (
            RetrievalOptions.model_validate_json(config.read_text(encoding="utf-8"))
            if config
            else None
        )
        if options and (
            options.method not in {None, FusionMethod(ranking)} or options.embedding != embedding
        ):
            raise ValueError("配置 method/embedding 必须与命令行一致")
        if options and any(
            not getattr(options.channels, name)
            for name in ("bm25", "ripgrep", "structural", "test", "history")
        ):
            raise ValueError("融合配置必须启用所有消融通道（semantic 可由 none 禁用）")
        if options and options.channels.semantic != (embedding == "offline-hash"):
            raise ValueError("融合配置 semantic 开关必须与 embedding 模式一致")
    except (ValueError, OSError) as error:
        raise typer.BadParameter(str(error)) from error
    settings = get_settings()
    settings.ensure_dirs()
    try:
        report = run_evaluation(
            cases,
            dataset.resolve().parent,
            settings.cache_dir,
            embedding="offline-hash" if embedding == "offline-hash" else "none",
            method=FusionMethod(ranking),
            weights=options.weights if options else None,
            rrf_k=options.rrf_k if options else 60,
            seed_channel=options.seed_channel if options else "bm25",
        )
        destination = output or settings.artifacts_dir / "phase2/retrieval-evaluation.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        typer.echo(render_table(report))
        typer.echo(f"Evidence: {destination}")
        if any(row.failures for row in report.rows):
            raise typer.Exit(code=1)
    except (ValueError, OSError) as error:
        raise typer.BadParameter(str(error)) from error


@app.command("run-tests")
def run_tests(
    repo: str = typer.Option(..., "--repo", help="本地 Git 仓库路径。"),
    tests: str | None = typer.Option(
        None,
        "--tests",
        help="空格分隔的 pytest node id；省略时执行 EnvSpec 的全量选择。",
    ),
    base_commit: str = typer.Option("HEAD", "--base-commit", help="固定基准提交。"),
    env_spec: Path | None = typer.Option(None, "--env-spec", help="显式 env.yaml。"),
    allow_build_network: bool = typer.Option(
        False,
        "--allow-build-network",
        help="允许首次环境构建联网安装依赖；测试执行始终断网。",
    ),
) -> None:
    """在隔离 Docker 容器中执行测试并输出 ExecutionResult JSON。"""
    from docker.errors import DockerException

    from codepilot.artifacts import ArtifactStore
    from codepilot.repo.workspace import WorkspaceError, WorkspaceManager, normalize_source
    from codepilot.sandbox.docker import DockerSandbox
    from codepilot.sandbox.env_spec import EnvSpecError, resolve_env_spec
    from codepilot.schemas.enums import ExecutionStatus
    from codepilot.schemas.execution import ExecutionResult

    if tests is not None and not tests.strip():
        raise typer.BadParameter("--tests 不能是空字符串；省略该选项表示全量测试")
    try:
        selected = tuple(shlex.split(tests, posix=True)) if tests is not None else ()
    except ValueError as exc:
        raise typer.BadParameter(f"--tests 无法解析: {exc}") from exc
    if tests is not None and not selected:
        raise typer.BadParameter("--tests 必须至少包含一个 pytest node id")

    settings = get_settings()
    settings.ensure_dirs()
    run_id = "sandbox-" + secrets.token_hex(8)
    artifacts = ArtifactStore(settings.artifacts_dir, run_id)
    manager = WorkspaceManager(cache_root=settings.repo_cache_dir())
    workspace = None
    try:
        source = normalize_source(repo)
        if not source.is_local:
            raise ValueError("run-tests 当前只接受本地仓库；远端仓库请先显式检出并审阅")
        workspace = manager.create(
            source=source,
            base_commit=base_commit,
            target=artifacts.run_dir / "workspace",
        )
        spec = resolve_env_spec(workspace.path, explicit=env_spec)
        sandbox = DockerSandbox(
            artifacts=artifacts,
            allowed_workspace_root=settings.runs_dir(),
            log_tail_bytes=settings.sandbox_log_tail_bytes,
        )
        controlled_root = (PROJECT_ROOT / "tests" / "fixtures" / "repos").resolve()
        controlled_fixture = bool(
            source.local_path and source.local_path.resolve().is_relative_to(controlled_root)
        )
        environment = sandbox.build_env(
            repository=source.normalized,
            base_commit=workspace.base_commit,
            workspace=workspace.path,
            env_spec=spec,
            allow_network=allow_build_network or controlled_fixture,
        )
        if environment.succeeded:
            result = sandbox.run_tests(
                workspace=workspace.path,
                environment=environment,
                env_spec=spec,
                tests=selected,
                artifact_iteration=1,
            )
            result = result.model_copy(
                update={
                    "diagnostics": {
                        **result.diagnostics,
                        "environment_cache_key": environment.cache_key,
                        "environment_cache_hit": environment.from_cache,
                        "base_commit": workspace.base_commit,
                    }
                }
            )
        else:
            result = ExecutionResult(
                status=environment.status,
                duration_seconds=environment.duration_seconds,
                error_reason=(
                    "ENVIRONMENT_BUILD_FAILED"
                    if environment.status is ExecutionStatus.ENV_ERROR
                    else "DOCKER_UNAVAILABLE"
                ),
                diagnostics={
                    "detail": environment.error or "环境构建失败",
                    "environment_cache_key": environment.cache_key,
                    "environment_cache_hit": False,
                },
            )
    except DockerException as exc:
        result = ExecutionResult(
            status=ExecutionStatus.SANDBOX_ERROR,
            error_reason="DOCKER_UNAVAILABLE",
            diagnostics={"detail": redact_text(str(exc))},
        )
    except (WorkspaceError, EnvSpecError, OSError, ValueError) as exc:
        result = ExecutionResult(
            status=ExecutionStatus.SANDBOX_ERROR,
            error_reason="WORKSPACE_OR_ENVSPEC_ERROR",
            diagnostics={"detail": str(exc)},
        )
    finally:
        if workspace is not None:
            manager.remove(workspace)

    typer.echo(result.model_dump_json(indent=2))
    if result.status is not ExecutionStatus.PASS:
        raise typer.Exit(code=1)


@app.command("solve")
def solve(
    repo: str = typer.Option(..., "--repo", help="仓库路径或 URL。"),
    issue: str = typer.Option(..., "--issue", help="Issue 文本或文件路径。"),
    base_commit: str = typer.Option("HEAD", "--base-commit", help="固定基准提交。"),
    fake_llm_script: Path | None = typer.Option(
        None,
        "--fake-llm-script",
        help="FakeLLM JSON 脚本；离线测试模式，不访问网络。",
    ),
    fake_sandbox: bool = typer.Option(
        False,
        "--fake-sandbox",
        help="使用明确标识的 FakeSandbox；仅用于离线集成测试。",
    ),
    allow_live_llm: bool = typer.Option(
        False,
        "--allow-live-llm",
        help="显式授权使用 configs/llm.yaml 中的真实 Provider（可能产生费用）。",
    ),
    llm_model: str | None = typer.Option(
        None,
        "--llm-model",
        help="本次运行把**所有**角色都绑定到这个模型别名（configs/llm.yaml 的 models 键）。"
        "不传则按配置里的 roles 逐角色取值。",
    ),
    glm_platform: str | None = typer.Option(
        None,
        "--glm-platform",
        help="GLM 平台：zai 或 bigmodel（等价于设置 GLM_PLATFORM 环境变量）。",
    ),
    allow_build_network: bool = typer.Option(
        False,
        "--allow-build-network",
        help="允许 Docker 首次构建环境时联网安装依赖；测试执行仍断网。",
    ),
    env_spec: Path | None = typer.Option(None, "--env-spec", help="显式 env.yaml。"),
    keep_workspace: bool = typer.Option(
        False, "--keep-workspace", help="保留受控 workspace 以便检查补丁。"
    ),
) -> None:
    """运行一次受控的定位、规划、补丁生成、校验与测试闭环。"""
    import json
    from datetime import UTC, datetime
    from typing import cast

    from docker.errors import DockerException

    from codepilot.agents.coder import CodingAgent
    from codepilot.agents.impact_analyzer import ImpactAnalyzer
    from codepilot.agents.issue_analyzer import IssueAnalyzer
    from codepilot.agents.localizer import Localizer
    from codepilot.agents.planner import Planner
    from codepilot.artifacts import ArtifactStore, RetrievalSnapshotStore
    from codepilot.graph.minimal import MinimalDependencies, run_minimal_graph
    from codepilot.llm.base import LLMProvider
    from codepilot.llm.fake import FakeLLM, Reply
    from codepilot.llm.registry import LLMRegistry
    from codepilot.llm.tracking import UsageTrackingProvider
    from codepilot.patch.validator import PatchValidator
    from codepilot.repo.graph import RepoGraph
    from codepilot.repo.index import Indexer, RepositoryIndex
    from codepilot.repo.workspace import WorkspaceError, WorkspaceManager
    from codepilot.retrieval.service import retrieve_symbols
    from codepilot.sandbox.base import Sandbox
    from codepilot.sandbox.docker import DockerSandbox
    from codepilot.sandbox.env_spec import EnvSpecError, resolve_env_spec
    from codepilot.sandbox.fake import FakeSandbox, scripted_result
    from codepilot.schemas.enums import ExecutionStatus, RunStatus
    from codepilot.schemas.retrieval import FusionMethod, RetrievalResult
    from codepilot.schemas.state import RepairState
    from codepilot.schemas.usage import Usage
    from codepilot.storage.db import (
        create_engine_from_url,
        create_session_factory,
        init_db,
        session_scope,
    )
    from codepilot.storage.repository import Stores
    from codepilot.tools.base import WorkspacePathPolicy
    from codepilot.tools.registry import build_tool_registry
    from codepilot.tools.write_tools import TestRunBudget

    if fake_llm_script is None and not allow_live_llm:
        raise typer.BadParameter(
            "必须提供 --fake-llm-script，或显式传 --allow-live-llm；默认不会调用付费 API"
        )
    if fake_llm_script is not None and allow_live_llm:
        raise typer.BadParameter("--fake-llm-script 与 --allow-live-llm 不能同时使用")
    try:
        issue_path = Path(issue)
        issue_text = issue_path.read_text(encoding="utf-8") if issue_path.is_file() else issue
        if not issue_text.strip():
            raise ValueError("Issue 不能为空")
        fake_replies: list[Reply] | None = None
        if fake_llm_script is not None:
            raw = json.loads(fake_llm_script.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("FakeLLM 脚本必须是 JSON 数组")
            fake_replies = cast("list[Reply]", raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    settings = get_settings()
    settings.ensure_dirs()
    run_id = "solve-" + secrets.token_hex(8)
    artifacts = ArtifactStore(settings.artifacts_dir, run_id)
    manager = WorkspaceManager(cache_root=settings.repo_cache_dir())
    workspace = None
    registry_owner: LLMRegistry | None = None
    engine = create_engine_from_url("sqlite://")
    init_db(engine)
    factory = create_session_factory(engine)
    try:
        workspace = manager.create(
            source=repo,
            base_commit=base_commit,
            target=artifacts.run_dir / "workspace",
        )
        spec = resolve_env_spec(workspace.path, explicit=env_spec)
        sandbox: Sandbox
        if fake_sandbox:
            sandbox = FakeSandbox([scripted_result(ExecutionStatus.PASS)])
        else:
            sandbox = DockerSandbox(
                artifacts=artifacts,
                allowed_workspace_root=settings.runs_dir(),
                log_tail_bytes=settings.sandbox_log_tail_bytes,
            )
        environment = sandbox.build_env(
            repository=workspace.source.normalized,
            base_commit=workspace.base_commit,
            workspace=workspace.path,
            env_spec=spec,
            allow_network=allow_build_network,
        )
        with session_scope(factory) as session:
            stores = Stores(session)
            Indexer().index(stores, workspace)
            index_data = RepositoryIndex(stores, workspace.repository_id)
            graph = RepoGraph(index_data)
            symbols = index_data.symbol_objects()
            policy = WorkspacePathPolicy(workspace.path)
            tools = build_tool_registry(
                policy=policy,
                index=index_data,
                graph=graph,
                base_commit=workspace.base_commit,
                sandbox=sandbox,
                environment=environment,
                env_spec=spec,
                test_budget=TestRunBudget(12),
            )
            localizer_provider: LLMProvider
            planner_provider: LLMProvider
            coder_provider: LLMProvider
            if fake_replies is not None:
                run_usage = Usage()
                shared_provider = UsageTrackingProvider(FakeLLM(fake_replies), usage=run_usage)
                localizer_provider = planner_provider = coder_provider = shared_provider
                simulated_llm = True
            else:
                registry_owner = LLMRegistry(
                    _effective_llm_config(alias=llm_model, platform=glm_platform)
                )
                run_usage = registry_owner.usage
                localizer_provider = registry_owner.for_role("localizer")
                planner_provider = registry_owner.for_role("planner")
                coder_provider = registry_owner.for_role("coder")
                simulated_llm = False
            snapshots = RetrievalSnapshotStore(artifacts)

            def retrieve_for_solve(text: str) -> RetrievalResult:
                return retrieve_symbols(
                    workspace.path,
                    symbols,
                    text,
                    channel="bm25",
                    fusion_method=FusionMethod.WEIGHTED,
                    channel_weights={"bm25": 1.0},
                    graph=graph,
                )

            deps = MinimalDependencies(
                repository=workspace.source.normalized,
                workspace=workspace.path,
                index=index_data,
                graph=graph,
                issue_analyzer=IssueAnalyzer(),
                retriever=retrieve_for_solve,
                localizer=Localizer(localizer_provider, index_data, workspace.path, snapshots),
                impact_analyzer=ImpactAnalyzer(index_data, graph),
                planner=Planner(planner_provider, tools, policy, index_data),
                coder=CodingAgent(coder_provider, tools),
                validator=PatchValidator(policy, workspace.base_commit, index=index_data),
                sandbox=sandbox,
                environment=environment,
                env_spec=spec,
                artifacts=artifacts,
                usage=run_usage,
                simulated_llm=simulated_llm,
            )
            initial = RepairState(
                task_id=f"cli/{run_id}",
                run_id=run_id,
                repository_id=workspace.repository_id,
                base_commit=workspace.base_commit,
                issue=issue_text,
                started_at=datetime.now(UTC),
            )
            final = run_minimal_graph(initial, deps)
        report_path = artifacts.run_dir / "report.json"
        typer.echo(report_path.read_text(encoding="utf-8"))
        typer.echo(f"Run artifacts: {artifacts.run_dir}", err=True)
        if final.status is not RunStatus.PROVISIONALLY_VERIFIED:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (DockerException, WorkspaceError, EnvSpecError, OSError, ValueError) as exc:
        detail = redact_text(str(exc))
        typer.echo(
            json.dumps(
                {
                    "status": "error",
                    "stop_reason": "configuration_or_environment",
                    "detail": detail,
                },
                ensure_ascii=False,
            ),
            err=True,
        )
        raise typer.Exit(code=1) from exc
    finally:
        if registry_owner is not None:
            registry_owner.close()
        if workspace is not None:
            if keep_workspace:
                typer.echo(f"workspace 保留在: {workspace.path}", err=True)
            else:
                manager.remove(workspace)
        engine.dispose()


@app.command("bench")
def bench(
    suite: str = typer.Option("synthetic", "--suite", help="评测套件名。"),
    config: Path = typer.Option(
        Path("configs/full.yaml"), "--config", help="运行配置（预算、seed、整批上限）。"
    ),
    seeds: str = typer.Option(
        ...,
        "--seeds",
        help="seed 数量（如 3 → 0,1,2）或逗号分隔的显式列表（如 7,8,9）。",
    ),
    suite_root: Path = typer.Option(
        Path("benchmarks/synthetic"), "--suite-root", help="合成 fixture 目录。"
    ),
    task: list[str] = typer.Option(
        [],
        "--task",
        help="只派发这些 task_id（可重复）。用于单任务真实 Agent 验证；"
        "分母随之缩小，评测口径与预算不变。",
    ),
    max_total_cost: float | None = typer.Option(
        None, "--max-total-cost", help="整批费用上限，达到后停止派发新任务。"
    ),
    max_in_flight: int | None = typer.Option(
        None, "--max-in-flight", help="同时在途任务数（>1 需要配合每任务预留额）。"
    ),
    fake_llm_script_dir: Path | None = typer.Option(
        None,
        "--fake-llm-script-dir",
        help="按 <task_id>.json 提供 FakeLLM 脚本；离线工程 smoke，不访问网络。",
    ),
    fake_sandbox: bool = typer.Option(
        False, "--fake-sandbox", help="使用明确标识的 FakeSandbox；仅工程测试。"
    ),
    allow_live_llm: bool = typer.Option(
        False, "--allow-live-llm", help="显式授权使用真实 Provider（会产生费用）。"
    ),
    llm_model: str | None = typer.Option(
        None,
        "--llm-model",
        help="整批把**所有**角色绑定到这个模型别名；不传则按 configs/llm.yaml 的 roles 取值。",
    ),
    glm_platform: str | None = typer.Option(
        None, "--glm-platform", help="GLM 平台：zai 或 bigmodel。"
    ),
    resume: Path | None = typer.Option(
        None, "--resume", help="从既有 run 目录续跑，已完成任务不重跑。"
    ),
    checkpoint: bool = typer.Option(
        True,
        "--checkpoint/--no-checkpoint",
        help="每个 (task, seed) 用独立 thread 的节点级 checkpoint；进程中断后可从未完成的节点接上。"
        "--no-checkpoint 只保留任务级续跑。",
    ),
    record_llm: bool | None = typer.Option(
        None,
        "--record-llm/--no-record-llm",
        help="把完整 LLM 请求体录制到每个 run 目录的 llm_calls.jsonl（落盘前脱敏）。"
        "缺省取 settings.record_llm。",
    ),
    require_validated: bool = typer.Option(
        True,
        "--require-validated",
        help="只跑已经通过 E-02 校验的 fixture；缺记录就拒绝开跑。",
    ),
) -> None:
    """运行合成 benchmark：多 seed、全局预算、可续跑，输出四份产物。

    默认不碰付费模型：必须显式 ``--allow-live-llm``。FakeLLM 跑出来的结果会带上
    ``simulated_llm=True``，报告标题写成"工程 smoke"，不会被当成模型效果。
    """
    import json as _json

    from codepilot.eval.evaluation import GoldEvaluator
    from codepilot.eval.report import write_benchmark_report
    from codepilot.eval.runner import assembly_factory, load_bench_config, provider_config_hash
    from codepilot.eval.synthetic import validate_suite

    parsed_seeds = _parse_seeds(seeds)
    if fake_llm_script_dir is not None and allow_live_llm:
        raise typer.BadParameter("--fake-llm-script-dir 与 --allow-live-llm 不能同时使用")
    if fake_llm_script_dir is None and not allow_live_llm:
        raise typer.BadParameter(
            "必须提供 --fake-llm-script-dir，或显式传 --allow-live-llm；默认不会调用付费 API"
        )

    settings = get_settings()
    settings.ensure_dirs()
    # 生效配置只在这里读**一次**：它同时用于费用哈希、身份字符串和真正跑起来的
    # provider。分开读的话，GLM_* 覆盖只要在其中一处没生效，报表就会给出一份
    # "记录了但没跑过"的配置指纹——而这两种配置长得几乎一样。
    live_config = None
    model_identity = "FakeLLM"
    llm_payload: dict[str, Any] = {}
    if fake_llm_script_dir is None:
        try:
            live_config = _effective_llm_config(alias=llm_model, platform=glm_platform)
            llm_payload = cast("dict[str, Any]", redact(live_config.model_dump(mode="json")))
            model_identity = role_model_identity(live_config)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            raise typer.BadParameter(f"LLM 配置读取失败：{exc}") from exc
        currency_gaps = live_config.check().currency_unmatched
        if currency_gaps:
            detail = ", ".join(f"{alias}({currency})" for alias, currency in currency_gaps.items())
            raise typer.BadParameter(
                f"角色所用模型的单价币种与预算币种 {live_config.budget_currency} 不一致且缺汇率："
                f"{detail}。请在 configs/llm.yaml 的 exchange_rates 里声明该币种汇率，"
                "或把 budget_currency 改成同一币种——按 1.0 混算出来的总额是错的，不接受。"
            )

    bench_config = load_bench_config(
        config,
        seeds_override=parsed_seeds,
        max_total_cost_override=max_total_cost,
        max_in_flight_override=max_in_flight,
        model_identity=model_identity,
        provider_config=provider_config_hash(llm_payload),
    )
    if bench_config.suite != suite:
        raise typer.BadParameter(
            f"配置里的 suite={bench_config.suite!r} 与 --suite={suite!r} 不一致"
        )

    loaded = validate_suite(suite_root)
    for task_id, reasons in sorted(loaded.invalid.items()):
        typer.echo(f"[INVALID_FIXTURE] {task_id}: {reasons[0]}", err=True)
    if not loaded.fixtures:
        typer.echo("没有可用 fixture。", err=True)
        raise typer.Exit(code=2)
    if loaded.invalid:
        typer.echo(
            f"警告：{len(loaded.invalid)} 个 fixture schema 不合格，已从本次分母中排除并记录。",
            err=True,
        )

    work_root = settings.artifacts_dir / "bench-work"
    validation_dir = settings.artifacts_dir / "phase10" / "fixture-validation"
    validated = _validated_fixture_versions(validation_dir) if require_validated else {}
    if require_validated:
        missing = [item.task_id for item in loaded.fixtures if item.task_id not in validated]
        if missing:
            typer.echo(
                "以下 fixture 没有真实 Docker 校验记录，拒绝进入 benchmark 分母：\n  "
                + "\n  ".join(missing[:10])
                + "\n先运行：uv run python scripts/validate_fixtures.py",
                err=True,
            )
            raise typer.Exit(code=2)
        loaded_fixtures = [item for item in loaded.fixtures if item.task_id in validated]
    else:
        loaded_fixtures = list(loaded.fixtures)

    if task:
        # ``--task`` 只是把同一套完整流程缩小到几个 fixture，不是第二条评测通路：
        # 分母变小了，但 fixture 校验、预算、Docker 后端与报告口径全都照旧。
        wanted = {item.strip() for item in task if item.strip()}
        known = {item.task_id for item in loaded_fixtures}
        unknown = sorted(wanted - known)
        if unknown:
            typer.echo(
                f"[UNKNOWN_TASK] {', '.join(unknown)}；当前套件可用 fixture："
                + ", ".join(sorted(known)[:10])
                + (f" …（共 {len(known)} 个）" if len(known) > 10 else ""),
                err=True,
            )
            raise typer.Exit(code=2)
        loaded_fixtures = [item for item in loaded_fixtures if item.task_id in wanted]
        typer.echo(
            f"[task filter] 只派发 {len(loaded_fixtures)} 个 fixture × "
            f"{len(parsed_seeds)} seed；报告分母随之缩小。",
            err=True,
        )

    sandbox_factory = _bench_sandbox_factory(fake_sandbox=fake_sandbox)
    eval_work_root = work_root / "evaluation"
    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=work_root / "repo-cache"),
        sandbox_factory=sandbox_factory,
        work_root=eval_work_root,
    )
    providers = _bench_provider_factory(
        fake_llm_script_dir=fake_llm_script_dir,
        allow_live_llm=allow_live_llm,
        live_config=live_config,
        budget_currency=bench_config.budget_currency,
        record_llm=settings.record_llm if record_llm is None else record_llm,
    )
    runner = BenchmarkRunner(
        config=bench_config,
        fixtures=loaded_fixtures,
        assembly_factory=assembly_factory(
            work_root=work_root,
            cache_root=settings.repo_cache_dir(),
            sandbox_factory=sandbox_factory,
            providers=providers,
            evaluator=evaluator,
            budget=bench_config.per_task_budget,
            record_llm=settings.record_llm if record_llm is None else record_llm,
            checkpoint=checkpoint,
        ),
        evaluator=evaluator,
        journal_directory=resume,
    )
    if allow_live_llm:
        _confirm_live_cost(bench_config, len(loaded_fixtures))
    typer.echo(f"run dir: {runner.run_dir}", err=True)
    result = runner.run()
    paths = write_benchmark_report(result, runner.run_dir)
    typer.echo(
        _json.dumps(
            {
                "run_id": result.run_id,
                "integrity": result.integrity,
                "counts": result.counts(),
                "budget": result.ledger,
                "artifacts": {
                    "results": str(paths.results),
                    "metrics": str(paths.metrics),
                    "summary": str(paths.summary),
                    "manifest": str(paths.manifest),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    completed = sum(1 for item in result.records if item.counted)
    if not completed:
        raise typer.Exit(code=1)


def _parse_seeds(text: str) -> tuple[int, ...]:
    """``--seeds 3`` 是数量，``--seeds 7,8,9`` 是显式列表。

    两种写法都要支持：04 的命令示例用的是数量，而续跑与消融要能指定具体 seed。
    """
    raw = text.strip()
    if not raw:
        raise typer.BadParameter("--seeds 不能为空")
    try:
        if "," in raw:
            values = tuple(int(item) for item in raw.split(",") if item.strip())
        elif raw.isdigit() and int(raw) > 0:
            values = tuple(range(int(raw)))
        else:
            values = (int(raw),)
    except ValueError as exc:
        raise typer.BadParameter(f"--seeds 解析失败: {exc}") from exc
    if not values:
        raise typer.BadParameter("--seeds 至少要有一个")
    if min(values) < 0:
        raise typer.BadParameter("--seeds 不能为负")
    if len(set(values)) != len(values):
        raise typer.BadParameter("--seeds 不能有重复值")
    return values


def _validated_fixture_versions(directory: Path) -> dict[str, str]:
    """读取 E-02 的校验记录：``task_id -> fixture_version``。

    只认 ``status=VALID`` 且带真实执行证据的记录。``ENV_ERROR`` 不是"任务无效"，
    但也不能当作"已验证有效"——那会把一批从没测出结论的 fixture 放进分母。
    """
    summary = Path(directory) / "summary.json"
    if not summary.is_file():
        return {}
    try:
        payload = json.loads(summary.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not payload.get("real_execution_evidence"):
        return {}
    out: dict[str, str] = {}
    for record in payload.get("records", []):
        if record.get("status") == "VALID" and record.get("task_id"):
            out[str(record["task_id"])] = str(record.get("fixture_version", ""))
    return out


def _bench_sandbox_factory(*, fake_sandbox: bool) -> Any:
    from codepilot.sandbox.base import Sandbox
    from codepilot.sandbox.docker import DockerSandbox
    from codepilot.sandbox.fake import FakeSandbox, scripted_result
    from codepilot.schemas.enums import ExecutionStatus

    def factory(artifacts: ArtifactStore) -> Sandbox:
        if fake_sandbox:
            return FakeSandbox([scripted_result(ExecutionStatus.PASS)])
        return DockerSandbox(
            artifacts=artifacts,
            allowed_workspace_root=get_settings().artifacts_dir,
            log_tail_bytes=get_settings().sandbox_log_tail_bytes,
        )

    return factory


def _bench_provider_factory(
    *,
    fake_llm_script_dir: Path | None,
    allow_live_llm: bool,
    live_config: LLMConfig | None,
    budget_currency: str,
    record_llm: bool = False,
) -> Any:
    from codepilot.eval.assembly import ProviderBundle

    if fake_llm_script_dir is None:
        assert allow_live_llm
        assert live_config is not None  # bench 里两者同时成立；缺一份就是装配错误

        def live_providers(_fixture: Any, _seed: int) -> ProviderBundle:
            # 每个 (task, seed) 一份新的 ProviderBundle：共用一份会让两个任务写同一个
            # Usage 对象，于是 A 的费用记到 B 头上——顺序跑看不出来，只在并发下出现。
            # budget_currency 用运行配置的值而不是默认 CNY：Z.AI 以 USD 计价时，
            # 按 CNY 归集会把每个数都变成口径不明的数。
            # record_llm 以前只是被 CLI 打印出来，从没接到真实执行路径上：那次真实
            # GLM 实验开了录制也不会有 llm_calls.jsonl。
            return ProviderBundle.live(
                live_config, budget_currency=budget_currency, record_llm=record_llm
            )

        return live_providers

    def providers(fixture: Any, seed: int) -> ProviderBundle:
        path = Path(fake_llm_script_dir) / f"{fixture.task_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"缺少 FakeLLM 脚本 {path}；没有脚本时拒绝伪造模型应答。")
        queues = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(queues, dict):
            raise ValueError(f"{path.name} 必须是 purpose -> 回复列表 的映射")
        return ProviderBundle.scripted(
            {key: list(value) for key, value in queues.items()},
            model_identity=f"FakeLLM/{path.name}@s{seed}",
        )

    return providers


def _confirm_live_cost(config: Any, fixture_count: int) -> None:
    """跑真实模型前把费用上界说出来（02 §17.4：运行前写进实验计划）。

    说"上界"而不是"预计"：预算按预留额判定，而不可预测的外部账单只能靠单任务上限
    约束，这里没法保证总额一定不超过某个数。
    """
    observations = fixture_count * len(config.seeds)
    ceiling = (
        "不设上限"
        if config.max_total_cost is None
        else f"{config.max_total_cost} {config.budget_currency}"
    )
    per_task = (
        "未设"
        if config.per_task_budget.max_cost is None
        else f"{config.per_task_budget.max_cost} {config.budget_currency}"
    )
    typer.echo(
        f"[live] 将派发 {observations} 次运行（{fixture_count} fixture × {len(config.seeds)} seed）；"
        f"整批上限 {ceiling}，单任务上限 {per_task}。"
        "上界依赖服务商实际计价，不保证账单一定不超过该值。",
        err=True,
    )


@app.command("report")
def report(
    run_id: str = typer.Argument(..., help="运行 ID。"),
) -> None:
    """[未实现] 渲染运行报告。"""
    del run_id
    _not_yet("report")


if __name__ == "__main__":  # pragma: no cover
    app()
