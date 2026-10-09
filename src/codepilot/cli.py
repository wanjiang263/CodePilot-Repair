"""命令行入口（F-17）。

Phase 0 只把 ``config`` 与 ``doctor`` 做实。其余子命令**注册但不假装可用**：
它们会打印"尚未实现 + 计划阶段"并以退出码 2 结束。

为什么不写成看起来正常的空实现：03 §2.2 明确禁止把未完成的实现呈现为完成。
一个静默返回 0 的 ``solve`` 比不存在的 ``solve`` 危险得多——CI 里它会一路
绿灯，直到有人真的去看产物。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import typer

from codepilot.config import PROJECT_ROOT, get_settings
from codepilot.observability.logging import configure_logging

__all__ = ["app"]

#: 尚未实现的子命令 → 计划落地的阶段。值会出现在报错信息里。
_PLANNED: dict[str, str] = {
    "index": "Phase 1（Repository Intelligence）",
    "retrieve": "Phase 2（多通道检索与融合）",
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
    rows.append(("ripgrep", rg is not None, rg or "未找到——将退回 Python 实现（较慢）"))

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


@app.command("index")
def index(
    repo: str = typer.Argument(..., help="仓库路径或 URL。"),
    base_commit: str = typer.Option(..., "--base-commit", help="基准提交。"),
) -> None:
    """[未实现] 建立仓库索引：文件树、符号表、调用图。"""
    del repo, base_commit
    _not_yet("index")


@app.command("retrieve")
def retrieve(
    query: str = typer.Argument(..., help="检索语句。"),
) -> None:
    """[未实现] 多通道检索并融合排序。"""
    del query
    _not_yet("retrieve")


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
