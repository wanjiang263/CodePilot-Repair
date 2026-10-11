"""命令行入口（F-17）。

Phase 0 只把 ``config`` 与 ``doctor`` 做实。其余子命令**注册但不假装可用**：
它们会打印"尚未实现 + 计划阶段"并以退出码 2 结束。

为什么不写成看起来正常的空实现：03 §2.2 明确禁止把未完成的实现呈现为完成。
一个静默返回 0 的 ``solve`` 比不存在的 ``solve`` 危险得多——CI 里它会一路
绿灯，直到有人真的去看产物。
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from codepilot.config import PROJECT_ROOT, get_settings
from codepilot.observability.logging import configure_logging

if TYPE_CHECKING:
    from codepilot.repo.index import IndexStats

__all__ = ["app"]

#: 尚未实现的子命令 → 计划落地的阶段。值会出现在报错信息里。
#: ``index`` 已在 R-18 落地，不再列在这里——``test_unimplemented_commands_fail_loudly``
#: 以 ``sorted(_PLANNED)`` 参数化，删键即自动不再要求 index 退出码 2。
_PLANNED: dict[str, str] = {
    "run-tests": "Phase 4（Docker 沙箱）",
    "solve": "Phase 5（最小单轮闭环，M1）",
    "bench": "Phase 11（SWE-bench Lite，M3）",
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
    repo: str = typer.Argument(..., help="仓库路径。"),
) -> None:
    """[未实现] 在 Docker 沙箱中执行测试。"""
    del repo
    _not_yet("run-tests")


@app.command("solve")
def solve(
    repo: str = typer.Argument(..., help="仓库路径或 URL。"),
    issue: str = typer.Option(..., "--issue", help="Issue 文本或文件路径。"),
    base_commit: str = typer.Option(..., "--base-commit", help="基准提交。"),
) -> None:
    """[未实现] 端到端修复一个 Issue。"""
    del repo, issue, base_commit
    _not_yet("solve")


@app.command("bench")
def bench(
    subset: Path = typer.Option(Path("configs/subsets/swe_lite.json"), "--subset"),
) -> None:
    """[未实现] 在 SWE-bench Lite 子集上评测。"""
    del subset
    _not_yet("bench")


@app.command("report")
def report(
    run_id: str = typer.Argument(..., help="运行 ID。"),
) -> None:
    """[未实现] 渲染运行报告。"""
    del run_id
    _not_yet("report")


if __name__ == "__main__":  # pragma: no cover
    app()
