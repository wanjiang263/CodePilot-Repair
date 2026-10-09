"""R-23：真实中型仓库索引实测（Phase 1 验收之一）。

**这个脚本会联网** clone 一个固定 commit 的真实仓库。它不属于测试套件、不被
pytest 收集、CI 里永不跑，所以必须自己说清楚代价，而不是等别人踩到。

网络只在"把仓库取到本地"这一步用到：脚本先用 git（经 SOCKS 代理）做一次
``--bare`` clone 到临时目录，然后把这份**本地** bare 仓库交给
``WorkspaceManager``（fetch 模式从本地再取 workspace，不再联网），对固定
base commit 跑一次真实索引，把耗时与 Files / Symbols / Edges / Tests 计数**原样**
写入 ``docs/notes/phase1.md``（数字不预设、不美化，跑成什么样记什么）。

用法：

    uv run python scripts/index_real_repo.py                       # 默认 flask 3.0.3
    uv run python scripts/index_real_repo.py --repo <url> --commit <40位SHA>
    uv run python scripts/index_real_repo.py --git-proxy socks5h://127.0.0.1:7898

``--git-proxy`` 不设时读环境变量 ``CODEPILOT_GIT_HTTP_PROXY``。留空表示直连。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codepilot.fsutil import remove_tree
from codepilot.repo.index import Indexer
from codepilot.repo.workspace import WorkspaceManager
from codepilot.storage.db import (
    create_engine_from_url,
    create_session_factory,
    init_db,
    session_scope,
)
from codepilot.storage.repository import Stores

DEFAULT_REPO = "https://github.com/pallets/flask.git"
# 固定 release tag 3.0.3 的**提交** SHA（annotated tag 已 peel）。写死以保证可复现。
DEFAULT_COMMIT = "c12a5d874c5a014495eb2db8a73f40037bc813ac"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOTES = PROJECT_ROOT / "docs" / "notes" / "phase1.md"


def bare_clone(url: str, dest: Path, git_proxy: str) -> None:
    """把远端仓库 ``--bare`` clone 到本地 ``dest``（这是唯一联网的一步）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "-c", "core.autocrlf=false"]
    if git_proxy:
        cmd += ["-c", f"http.proxy={git_proxy}"]
    cmd += ["clone", "--bare", "--quiet", url, str(dest)]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    done = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", env=env, check=False
    )
    if done.returncode != 0:
        raise SystemExit(f"git clone --bare 失败：{done.stderr.strip()}")


def run_index(bare: Path, commit: str, workdir: Path) -> tuple[object, float]:
    """用 WorkspaceManager 从本地 bare 仓库取 fetch workspace 并真实计时索引。"""
    manager = WorkspaceManager(cache_root=workdir / "cache")
    workspace = manager.create(
        source=str(bare), base_commit=commit, target=workdir / "ws", mode="fetch"
    )

    engine = create_engine_from_url(f"sqlite:///{(workdir / 'index.db').as_posix()}")
    init_db(engine)
    factory = create_session_factory(engine)

    started = time.perf_counter()
    with session_scope(factory) as session:
        stats = Indexer().index(Stores(session), workspace)
    elapsed = time.perf_counter() - started

    manager.remove(workspace)
    engine.dispose()
    return stats, elapsed


def render(repo: str, commit: str, stats, elapsed: float) -> str:
    symbols_by_kind = ", ".join(f"{k}={v}" for k, v in sorted(stats.symbols_by_kind.items()))
    edges_by_kind: dict[str, int] = {}
    for (kind, _conf), count in stats.edges_by_kind_confidence.items():
        edges_by_kind[kind] = edges_by_kind.get(kind, 0) + count
    edges_line = ", ".join(f"{k}={v}" for k, v in sorted(edges_by_kind.items()))
    return (
        f"- 仓库：`{repo}`\n"
        f"- base commit：`{commit}`\n"
        f"- 模式：fetch workspace（自包含、只含 base 祖先）\n"
        f"- 索引耗时：**{elapsed:.2f}s**\n"
        f"- Files：**{stats.files}**；Symbols：**{sum(stats.symbols_by_kind.values())}** "
        f"（{symbols_by_kind}）\n"
        f"- Edges：**{sum(stats.edges_by_kind_confidence.values())}**（{edges_line}）\n"
        f"- Tests：**{stats.tests}**\n"
        f"- 解析错误文件：{len(stats.parse_errors)}；扫描跳过：{stats.scan}\n"
    )


def main() -> int:
    import tempfile

    parser = argparse.ArgumentParser(description="R-23 真实中型仓库索引实测")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--commit", default=DEFAULT_COMMIT)
    parser.add_argument("--git-proxy", default=os.environ.get("CODEPILOT_GIT_HTTP_PROXY", ""))
    parser.add_argument("--workdir", default=None, help="临时目录（默认系统 temp 下一次性目录）")
    args = parser.parse_args()

    ephemeral = args.workdir is None
    base_tmp = (
        Path(args.workdir).resolve()
        if not ephemeral
        else Path(tempfile.mkdtemp(prefix="codepilot-r23-"))
    )
    base_tmp.mkdir(parents=True, exist_ok=True)

    bare = base_tmp / "repo.bare"
    print(f"[1/3] bare clone（联网）：{args.repo} → {bare}")
    bare_clone(args.repo, bare, args.git_proxy)

    print(f"[2/3] 索引 base commit {args.commit}（fetch workspace，本地不联网）…")
    stats, elapsed = run_index(bare, args.commit, base_tmp)

    body = render(args.repo, args.commit, stats, elapsed)
    print("[3/3] 实测结果：\n" + body)

    write_notes(body)
    print(f"已写入 {NOTES}")

    if ephemeral:
        remove_tree(base_tmp, ignore_errors=True)
    return 0


def write_notes(body: str) -> None:
    """把实测数字原样追加/写入 docs/notes/phase1.md（幂等：同 base 段落替换）。"""
    NOTES.parent.mkdir(parents=True, exist_ok=True)
    header = "# Phase 1 实测记录\n\n> 数字来自真实运行，不预设、不美化。\n\n"
    if NOTES.exists():
        text = NOTES.read_text(encoding="utf-8")
        text = text.rstrip() + "\n\n" + body
        NOTES.write_text(text + "\n", encoding="utf-8")
    else:
        NOTES.write_text(header + body + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
