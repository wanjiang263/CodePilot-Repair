"""Repository 获取与 workspace 隔离（02 §6，R-01 / R-02 / R-03）。

```text
repo_url + base_commit
   ↓
cache/repos/<repo_hash>/          # bare clone，多个任务共享，只读
   ↓  git fetch <cache> <base>（默认；也可选 copy / worktree，见 WorkspaceMode）
runs/<run_id>/workspace/          # 每次运行独立，Agent 只能改这里
```

这一层的存在意义只有一条：**Agent 改坏的东西必须能被丢掉**。所以有三条硬规则，
每一条都对应一种会静默毁掉评测结果的失败：

1. **原目录绝不修改。** 本地路径输入也要先 clone 进缓存再从缓存取 workspace。
   直接 ``git clone <原目录> <workspace>`` 看起来更省事，但那样 workspace 与
   原目录共享对象库，一次 ``git gc`` 或误操作就可能回头改到用户自己的仓库。
2. **workspace 创建后必须是干净的。** ``git status --porcelain`` 非空意味着
   checkout 出错了（换行符转换、权限位、符号链接），此时导出的 ``patch.diff``
   会混进大量无关改动，而下游只会觉得"这个模型改了好多文件"。
3. **绝不读取 base_commit 之后的提交。** 那是修复本身。缓存是按仓库共享的，
   里面很可能已经有后续提交——所有历史查询都必须显式限定在 base 的祖先里
   （见 ``rev_parse`` 与 R-13；这里只保证 checkout 到 base）。
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from codepilot.fsutil import pid_alive, remove_tree, strip_reparse_points
from codepilot.observability.logging import get_logger
from codepilot.repo.gitcmd import GitError, run_git

log = get_logger(__name__)

__all__ = [
    "RepoSource",
    "Workspace",
    "WorkspaceError",
    "WorkspaceManager",
    "WorkspaceMode",
    "normalize_source",
    "repo_hash",
    "repository_id",
]

#: 远程 URL 的形态判断。只用于区分"本地路径"与"远端 URL"，不做白名单——
#: 白名单是 API 层的事（03 §29 只接受允许列表内的 https:// 仓库）。
_URL_SCHEME = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://")
_SCP_LIKE = re.compile(r"^(?!.*://)(?P<user>[^/@\s]+)@(?P<host>[^/:\s]+):(?P<path>.+)$")

#: ``git rev-parse --verify <rev>^{commit}`` 的输出是 40 位（sha1）或 64 位（sha256）十六进制。
_FULL_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")

#: workspace 的三种取法（docs/05 §23.1 Q13）。
#:
#: | 模式 | `.git` | 容器内 git 可用 | 能看到 base 之后的提交 | 代价 |
#: |---|---|---|---|---|
#: | ``fetch``（默认） | 目录 | 是 | 否 | 传一次 base 的祖先 |
#: | ``copy`` | 目录 | 是 | 否（事后截断） | 复制全库再 gc |
#: | ``worktree`` | 文件（宿主路径） | **否** | **是**（共享对象库） | 几乎为零 |
#:
#: ``fetch`` 从一开始就只拿 base 的祖先与已合入 base 的 tag，不需要事后删；
#: ``worktree`` 只适合用户明确不在意泄露、也不在沙箱里用 git 的 CLI 场景。
WorkspaceMode = Literal["fetch", "copy", "worktree"]


class WorkspaceError(RuntimeError):
    """workspace 相关的可预期失败（源不存在、commit 不在缓存里、workspace 不干净）。"""


@dataclass(frozen=True, slots=True)
class RepoSource:
    """一次运行要用的仓库来源。"""

    raw: str
    """用户原样给出的输入（URL、路径或 ``git@host:path``）。只用于日志与报错。"""

    normalized: str
    """规范化后的形式。缓存键与 ``repositories.url`` 都用它，
    否则同一个仓库的两种写法（带不带 ``.git``、带不带尾斜杠）会各建一份缓存。"""

    is_local: bool
    """本地路径为 True。调用方据此决定是否需要白名单校验（03 §29）。"""

    local_path: Path | None
    """``is_local`` 为 True 时的绝对路径。"""

    def __str__(self) -> str:
        return self.raw


@dataclass(frozen=True, slots=True)
class Workspace:
    """为一次运行准备好的可写工作副本。"""

    path: Path
    """workspace 根目录。Agent 只能改这棵子树。"""

    source: RepoSource
    base_commit: str
    """**解析后的完整 SHA**。短 SHA 或分支名在这里被固定下来，之后所有
    引用（patch 导出、历史查询、报告）都用这个值。"""

    repository_id: str
    """``(url, base_commit)`` 唯一标识，与 ``repositories`` 表的主键一致。"""

    @property
    def is_local(self) -> bool:
        return self.source.is_local

    @property
    def self_contained(self) -> bool:
        """git 元数据是否完全在这棵子树**内部**。

        这个属性决定了两件在设计上必须分清的事，而它们都只在"把 workspace 交给
        别人"时才暴露：

        * **沙箱里 git 能不能用。** 02 §13 规定测试执行阶段只挂载 ``/workspace``。
          复制模式的 ``.git`` 是目录，挂在里面，容器里 ``git log`` 正常；worktree
          模式的 ``.git`` 是一个内容为**宿主绝对路径**的文件，那个路径不在挂载
          范围内，容器里任何 git 命令都会失败（实测：``fatal: not a git
          repository``）。所以依赖 git 的测试（版本号推导之类）在 worktree 模式下
          于沙箱内是跑不起来的。
        * **base 之后的提交会不会被看到。** worktree 与缓存共享对象库和全部 refs，
          缓存里有 gold patch；自包含的复制模式则已被截断到 base。

        做成属性而不是写进注释，是为了让调用方**能断言**：沙箱层与 R-13 应当用
        它而不是靠"记得文档里那条规则"。
        """
        dot_git = self.path / ".git"
        if dot_git.is_dir():
            return True
        if not dot_git.is_file():
            return False  # 既不是目录也不是文件：不是一个 git 工作区
        declared = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        if not declared.startswith("gitdir:"):
            return False
        target = Path(declared.split(":", 1)[1].strip())
        if not target.is_absolute():
            target = (self.path / target).resolve()
        return target.is_relative_to(self.path.resolve())

    def assert_clean(self) -> None:
        """断言 workspace 无未提交改动；不干净则抛 ``WorkspaceError``。"""
        dirty = dirty_paths(self.path)
        if dirty:
            listing = "\n".join(f"  {line}" for line in dirty[:20])
            more = f"\n  …（共 {len(dirty)} 项）" if len(dirty) > 20 else ""
            raise WorkspaceError(
                "workspace 不干净，创建后应当没有任何改动。"
                "常见原因：换行符被 git 转换（core.autocrlf）、文件权限位差异、"
                "或 base_commit 本身就无法干净 checkout。\n"
                f"{listing}{more}"
            )


def normalize_source(raw: str) -> RepoSource:
    """把用户输入规范化成一个稳定的缓存键来源。

    规范化只做**不影响指向**的删减（尾斜杠、``.git`` 后缀、行首尾空白），
    不做小写化：GitHub 的 owner/repo 不区分大小写，但自建 Git 与本地路径区分，
    统一小写会让两个不同的仓库撞进同一份缓存。
    """
    text = raw.strip()
    if not text:
        raise WorkspaceError("仓库来源为空")

    scp = _SCP_LIKE.match(text)
    if scp:
        # ``git@host:path`` 是 ssh 的简写，等价于 ssh://git@host/path。
        path = scp.group("path").strip("/")
        normalized = f"ssh://{scp.group('user')}@{scp.group('host')}/{path}"
        return RepoSource(
            raw=raw, normalized=_strip_dot_git(normalized), is_local=False, local_path=None
        )

    scheme_match = _URL_SCHEME.match(text)
    if scheme_match:
        scheme = scheme_match.group("scheme").lower()
        if scheme == "file":
            # ``file://`` 在 03 §29 里是明确拒绝的形态，但它在 CLI 场景下
            # 只是"本地路径的另一种写法"。折中：按本地路径处理并在
            # normalized 里保留 file:// 形态，让白名单校验仍能识别并拒绝它。
            local = Path(text[len("file://") :].lstrip("/") if os.name == "nt" else text[7:])
            return RepoSource(
                raw=raw,
                normalized=text.rstrip("/"),
                is_local=True,
                local_path=local.expanduser().resolve(),
            )
        return RepoSource(raw=raw, normalized=_strip_dot_git(text), is_local=False, local_path=None)

    # Windows 盘符（C:\...）与 POSIX 绝对路径、相对路径都归为本地。
    path = Path(text).expanduser()
    resolved = path.resolve()
    return RepoSource(
        raw=raw,
        # 本地路径用 POSIX 形式做缓存键：Windows 上 ``C:\a\b`` 与 ``C:/a/b``
        # 是同一个目录，使用反斜杠会让两种写法各建一份缓存。
        normalized=resolved.as_posix(),
        is_local=True,
        local_path=resolved,
    )


def _strip_dot_git(url: str) -> str:
    return url.rstrip("/")[:-4] if url.rstrip("/").endswith(".git") else url.rstrip("/")


def repo_hash(source: RepoSource | str) -> str:
    """缓存目录名。**只由仓库来源决定**，与 commit 无关。

    02 §6 的缓存是"多个任务共享"的：同一个仓库的不同 commit 应当共用一份
    bare clone，否则每换一个 commit 就要重新克隆整个仓库。
    """
    src = normalize_source(source) if isinstance(source, str) else source
    return hashlib.sha256(src.normalized.encode("utf-8")).hexdigest()[:16]


def repository_id(source: RepoSource | str, base_commit: str) -> str:
    """``(repository_id, base_commit)`` 的主键。

    把 commit 算进 id，而不是让同一个 repository_id 横跨多个 commit：
    ``files``/``symbols``/``edges`` 都挂在 repository_id 上，若两个 commit
    共用一个 id，第一个 commit 的 symbol 会污染第二个 commit 的检索结果，
    而且症状是"召回了一些已经改名的函数"这种极难定位的现象。
    """
    src = normalize_source(source) if isinstance(source, str) else source
    payload = f"{src.normalized}@{base_commit}"
    return "repo_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class WorkspaceManager:
    """管理 bare clone 缓存与 per-run workspace。

    典型用法::

        manager = WorkspaceManager(cache_root=settings.repo_cache_dir())
        ws = manager.create(url="https://github.com/psf/requests", base_commit="abc123")
        ...  # ws.path 是唯一可写的目录
    """

    def __init__(
        self,
        *,
        cache_root: Path,
        allow_local: bool = True,
        timeout: float = 600.0,
    ) -> None:
        """
        ``allow_local=False`` 时拒绝一切非远端 URL 的来源。API 层必须这样构造
        （03 §29：拒绝 ``file://`` 和本地路径），CLI 层保持默认的 True。
        """
        # 缓存路径必须是绝对路径：``_fetch_base`` 把它当 git 远端传给
        # ``git fetch <cache>``，而那条命令的 cwd 是新建的 workspace，不是调用方
        # 目录。默认配置里 ``cache_dir`` 是相对的 ``cache``，实测（codepilot
        # run-tests）相对路径会解析成 workspace 下的不存在目录，报
        # "does not appear to be a git repository"，整个 workspace 建不出来。
        self.cache_root = Path(cache_root).expanduser().absolute()
        self.allow_local = allow_local
        self.timeout = timeout

    # ------------------------------------------------------------ 缓存

    def cache_path(self, source: RepoSource | str) -> Path:
        src = normalize_source(source) if isinstance(source, str) else source
        return self.cache_root / repo_hash(src)

    def ensure_cache(self, source: RepoSource | str) -> tuple[Path, RepoSource]:
        """确保 bare clone 存在，返回 ``(缓存路径, 规范化后的来源)``。

        并发安全：先克隆到同级临时目录再原子改名。两个进程同时处理同一个
        仓库时，后到的那个改名会失败（目标已存在），此时直接复用先到者的
        结果——比加锁简单，且不会留下半份克隆。
        """
        src = normalize_source(source) if isinstance(source, str) else source
        self._check_allowed(src)

        target = self.cache_path(src)
        target.parent.mkdir(parents=True, exist_ok=True)

        # 清扫放在**提前返回之前**。放在后面的话，缓存已经可用的那条路径根本
        # 走不到它——而那恰恰是残骸最容易堆积的场景：缓存建好之后再崩溃的克隆
        # 尝试，留下的每一份都是一个完整的 bare clone。
        self._sweep_stale_staging(target)

        if self._cache_usable(target):
            return target, src

        # 暂存目录名必须**每次调用都不同**。原来用的是 ``.tmp-<pid>``，而同一个
        # 进程里的多个 worker 线程 pid 相同 ——实测 4 个线程同时处理同一个仓库，
        # 3 个失败在 "destination path ... already exists"，因为先到的线程看到
        # 这个路径已存在就按"上次崩溃的残骸"把它删了，而那是另一个线程正在写的
        # 克隆。默认部署恰恰是进程内 worker 并行跑 benchmark，所以这条路径是
        # 常走的而不是边角。
        staging = target.with_name(f"{target.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")

        clone_from = str(src.local_path) if src.is_local else src.normalized
        try:
            run_git(
                [
                    "clone",
                    "--bare",
                    # --no-hardlinks：本地路径克隆时 git 默认把原仓库的
                    # .git/objects/** **硬链接**进缓存。实测（Windows 11 +
                    # git 2.51.1）缓存里的对象与用户仓库里的是同一个 inode、
                    # nlink=2。那样"缓存只读、原仓库绝不修改"就只在命令层面
                    # 成立：缓存按设计必须可写（worktree 的 index 与 commit
                    # 都写在缓存里），而 remove_tree 为了删只读对象会 chmod
                    # 共享 inode——一次误操作就静默改坏用户的对象库，直到 git
                    # 报对象损坏才暴露。复制模式早就用了这个参数，缓存这条更
                    # 关键的链接反而漏了。
                    "--no-hardlinks",
                    "--quiet",
                    clone_from,
                    str(staging),
                ],
                timeout=self.timeout,
            )
        except GitError as exc:
            remove_tree(staging, ignore_errors=True)
            raise WorkspaceError(self._explain_clone_failure(src, exc)) from exc
        except BaseException:
            # 只在自己的 clone 失败时删自己的暂存目录。以前是在开头无条件删掉
            # "同名的那个"，那正是并发下互相破坏的来源。
            remove_tree(staging, ignore_errors=True)
            raise
        try:
            self._apply_repo_config(staging)
            self._install_cache(staging, target, src)
        finally:
            # 安装成功时 staging 已经被改名搬走，这里的 remove 是个 no-op；
            # 失败时它负责把半成品收掉——否则每失败一次就留下一个完整的
            # bare clone，磁盘上悄悄涨。
            remove_tree(staging, ignore_errors=True)
        return target, src

    def _sweep_stale_staging(self, target: Path) -> None:
        """清掉别人留下的暂存目录，**只清已经没有主的那些**。

        以前的清理只认 ``.tmp-<当前pid>`` 这一个名字，于是别的进程崩溃留下的
        残骸永远没人管——每一份都是一整个 bare clone。
        """
        for path in target.parent.glob(f"{target.name}.tmp-*"):
            owner = _staging_owner(path.name, target.name)
            if owner is None or owner == os.getpid() or pid_alive(owner):
                continue
            if remove_tree(path, ignore_errors=True):
                log.info("workspace.swept_stale_staging", staging=str(path), owner_pid=owner)

    def rev_parse(self, repo: Path, rev: str) -> str | None:
        """把 rev 解析成完整 SHA；不存在时返回 ``None``。

        ``^{commit}`` 后缀让 tag 与 annotated tag 都解析到 commit 本身，
        而不是 tag 对象——传 tag 名进来是很常见的用法。
        """
        result = run_git(
            ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            cwd=repo,
            check=False,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha if _FULL_SHA.match(sha) else None

    def has_commit(self, repo: Path, commit: str) -> bool:
        return self.rev_parse(repo, commit) is not None

    # ------------------------------------------------------------ workspace

    def create(
        self,
        *,
        source: str | RepoSource,
        base_commit: str,
        target: Path,
        mode: WorkspaceMode = "fetch",
    ) -> Workspace:
        """从缓存取出一份可写的 workspace，checkout 到 ``base_commit``。

        ``mode`` 的取舍见 ``WorkspaceMode``。默认 ``fetch``：自包含（沙箱里 git
        可用）、看不到 base 之后的提交，而且比 ``copy`` 快——它从一开始就不
        传那些对象，不需要事后删。``worktree`` 几乎是瞬时的，但与缓存共享对象库，
        只适合不在意泄露、也不在沙箱里用 git 的场景。

        创建完成后会断言 workspace 干净（02 §6）。不干净时抛
        ``WorkspaceError`` 而不是返回一个"能用但 patch 会是垃圾"的目录。
        """
        cache, src = self.ensure_cache(source)

        resolved = self.rev_parse(cache, base_commit)
        if resolved is None:
            # 缓存可能是"HEAD 还能解析、但别的对象已经丢了"的半损坏状态——
            # `_cache_usable` 挡不住这一种。重建一次再试，让这种情况能自愈；
            # 仍然失败才认定是 base_commit 本身的问题。
            log.warning(
                "workspace.cache_missing_base_commit_rebuilding",
                cache=str(cache),
                base_commit=base_commit,
            )
            cache, src = self._rebuild_cache(cache, src)
            resolved = self.rev_parse(cache, base_commit)
            if resolved is None:
                raise WorkspaceError(
                    f"base_commit 不在缓存中: {base_commit!r}\n"
                    f"  仓库: {src.normalized}\n"
                    f"  缓存: {cache}\n"
                    "已重建过缓存仍然找不到该 commit，所以更可能是这个 SHA 打错了，"
                    "或者它还没有被推送到远端。"
                )

        target = Path(target).resolve()
        if target.exists() and any(target.iterdir()):
            raise WorkspaceError(
                f"workspace 目标目录已存在且非空: {target}\n"
                "每次都应当用一个新的目录，复用会残留上一轮的改动。"
            )
        target.parent.mkdir(parents=True, exist_ok=True)

        if mode not in ("fetch", "copy", "worktree"):
            raise WorkspaceError(f"未知的 workspace 模式: {mode!r}（可选 fetch / copy / worktree）")

        try:
            if mode == "worktree":
                self._worktree_add(cache, resolved, target)
            elif mode == "copy":
                self._clone_copy(cache, resolved, target)
            else:
                self._fetch_base(cache, resolved, target)

            workspace = Workspace(
                path=target,
                source=src,
                base_commit=resolved,
                repository_id=repository_id(src, resolved),
            )
            workspace.assert_clean()
        except BaseException:
            # 中途失败必须回滚。实测（修复前）：worktree 建好之后 assert_clean
            # 失败，target 目录留在磁盘上、worktree 注册也留在缓存里。后果是
            # 用同一个 target 重试会撞上"目标目录已存在且非空"，而缓存的
            # worktrees/ 会一次次堆积，`git worktree list` 越来越慢。
            #
            # 先注销注册再删目录：反过来的话 git 找不到工作树，注册项会变成
            # 悬空的，下一次 `git worktree prune` 之前都清不掉。
            if mode == "worktree":
                run_git(
                    ["worktree", "remove", "--force", str(target)],
                    cwd=cache,
                    check=False,
                    timeout=self.timeout,
                )
            remove_tree(target, ignore_errors=True)
            run_git(["worktree", "prune"], cwd=cache, check=False, timeout=self.timeout)
            raise

        return workspace

    def reset(self, workspace: Workspace | Path) -> None:
        """回退到 base：``git checkout -- . && git clean -fd``（02 §6）。

        只作用于 workspace。``-x`` 不加：被忽略的文件（``.venv``、构建产物）
        是环境搭建的产物，回退代码不该把它们删掉——那会让下一轮重新装一遍依赖。

        **在 check out 之前先摘掉重解析点。** 这两条 git 命令都沿着路径递归，
        而 git 在 Windows 上把 junction 当普通目录：``checkout -- .`` 会顺着
        链接把 base 版本写进**目标目录**，``clean -fd`` 会删掉目标目录里未跟踪
        的文件。实测把 workspace 的 ``src`` 换成指向用户原仓库的 junction 后，
        用户未提交的改动被覆盖、未跟踪的文件被删除，而用户那边的 ``git status``
        事后是干净的——损失没有任何痕迹。

        被跟踪的符号链接不受影响：它们本来就该由 ``checkout`` 重新建出来，
        建出来之后是链接而不是 junction，git 不会写穿它。
        """
        path = workspace.path if isinstance(workspace, Workspace) else Path(workspace)
        path = Path(path)
        removed = strip_reparse_points(path)
        if removed:
            log.warning("workspace.reset_stripped_links", workspace=str(path), paths=removed)
        run_git(["checkout", "--", "."], cwd=path, timeout=self.timeout)
        run_git(["clean", "-fd", "--quiet"], cwd=path, timeout=self.timeout)

    def remove(self, workspace: Workspace | Path) -> None:
        """删除 workspace。worktree 需要先从缓存注销，否则缓存里会留下
        悬空的 ``worktrees/<name>`` 记录，积累多了会让 ``git worktree list``
        越来越慢。

        **同样要先摘掉重解析点。** ``git worktree remove --force`` 在 Windows 上
        会穿过 junction 递归删除：实测把它指向一个目录，该目录里的文件会被删光
        而退出码是 0、stderr 是空的。指向 ``cache/repos/<hash>`` 的话，整个共享
        缓存（连同其他任务的 worktree 注册）会被清空。
        """
        path = workspace.path if isinstance(workspace, Workspace) else Path(workspace)
        path = Path(path)
        if not path.exists():
            return
        removed = strip_reparse_points(path)
        if removed:
            log.warning("workspace.remove_stripped_links", workspace=str(path), paths=removed)
        # 先尝试正式注销；失败（不是 worktree、缓存已删）就直接删目录。
        run_git(["worktree", "remove", "--force", str(path)], cwd=path, check=False)
        if path.exists():
            remove_tree(path, ignore_errors=True)

    # ------------------------------------------------------------ 内部

    def _cache_usable(self, path: Path) -> bool:
        """缓存能不能用——**真的去解析一次 HEAD，而不是看目录在不在**。

        只检查 ``HEAD``/``objects``/``refs`` 三个目录是否存在是不够的：实测把
        base commit 的 loose object 从 ``objects/`` 里删掉之后，这个判据仍然返回
        ``True``，于是残缺的缓存被当成可用，此后每次 ``create()`` 都以
        "base_commit 不在缓存中" 失败——**永不自愈**，而报错还在提示"该 commit
        可能在克隆之后才被推送"，把排查方向指向远端。

        ``rev-parse --verify HEAD^{commit}`` 会真的去对象库里走一遍，HEAD 悬空、
        对象缺失都会失败。它比 ``fsck`` 便宜得多，能挡住"克隆被打断"这类真实
        损坏（也正是 ``_is_bare_repo`` 的 docstring 描述的那种）。
        """
        if not _is_bare_repo(path):
            return False
        result = run_git(
            ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            cwd=path,
            check=False,
            timeout=self.timeout,
        )
        return result.returncode == 0

    def _install_cache(self, staging: Path, target: Path, src: RepoSource) -> None:
        """把克隆好的临时目录换到最终位置。

        ``Path.replace`` 底层是 ``os.replace``：在 POSIX 上可以原子地覆盖一个
        已存在的目录，**在 Windows 上不行**——目标目录存在时直接抛 ``OSError``。
        分三种情况处理：

        1. 直接成功：正常路径。
        2. 目标此时已是一个**可用**的缓存：另一个进程抢先建好了，丢掉自己这份。
        3. 目标存在但不可用：上次崩溃留下的残骸。必须先删掉它再改名——
           否则损坏的缓存永远修不好，每次调用都重新克隆一遍再把它丢掉，
           而表现是"索引结果一直是空的"，与真正的原因毫无关系。
        """
        try:
            Path(staging).replace(target)
            return
        except OSError:
            pass

        if self._cache_usable(target):
            remove_tree(staging, ignore_errors=True)
            return

        # 走到这里说明目标位置的缓存是坏的，要删掉重建。先确认没有别的任务
        # 正靠它活着——删掉会让那些 workspace 的 .git 一起消失。
        self._refuse_if_in_use(target, src)

        if not remove_tree(target, ignore_errors=True):
            remove_tree(staging, ignore_errors=True)
            raise WorkspaceError(
                f"缓存目录损坏且无法删除: {target}\n"
                f"  仓库: {src.normalized}\n"
                "通常是另一个进程正在使用它。删掉该目录后重试。"
            )

        try:
            Path(staging).replace(target)
        except OSError as exc:
            remove_tree(staging, ignore_errors=True)
            if not self._cache_usable(target):
                raise WorkspaceError(
                    f"无法把克隆好的缓存放到 {target}\n  仓库: {src.normalized}\n  {exc}"
                ) from exc

    def _rebuild_cache(self, cache: Path, src: RepoSource) -> tuple[Path, RepoSource]:
        """删掉缓存重新克隆一次。有活跃 workspace 时拒绝执行。"""
        self._refuse_if_in_use(cache, src)
        if not remove_tree(cache, ignore_errors=True):
            raise WorkspaceError(
                f"缓存损坏且无法删除: {cache}\n"
                f"  仓库: {src.normalized}\n"
                "通常是另一个进程正在使用它。"
            )
        return self.ensure_cache(src)

    def _active_worktrees(self, cache: Path) -> list[str]:
        """缓存上还注册着哪些 worktree（不含缓存自身）。

        用于在**删缓存之前**确认没有别的任务正靠它活着。
        """
        registered = cache / "worktrees"
        if not registered.is_dir():
            return []
        return sorted(p.name for p in registered.iterdir() if p.is_dir())

    def _refuse_if_in_use(self, cache: Path, src: RepoSource) -> None:
        """缓存上还有活跃 worktree 时，**拒绝**做任何会删掉它的修复。

        实测：任务 A 拿着一个 workspace，任务 B 发现缓存不可用走了修复分支，
        ``remove_tree(target)`` 把缓存目录删掉再重新克隆——A 的文件还在，但它
        的 ``.git`` 没了，``git status`` 变成 ``fatal: not a git repository``。
        A 已经跑出来的改动再也出不了 diff，而它自己完全不知道发生了什么。

        这里宁可让 B 失败。修缓存是可以等的事，毁掉另一个任务的 workspace 不是：
        B 的报错会写明是哪个 worktree 挡着，而 A 的数据损坏是无声的。
        """
        live = self._active_worktrees(cache)
        if not live:
            return
        raise WorkspaceError(
            f"缓存损坏，但上面还挂着 {len(live)} 个活跃 workspace，不能重建: {cache}\n"
            f"  仓库: {src.normalized}\n"
            f"  活跃 workspace: {', '.join(live)}\n"
            "重建会删掉缓存目录，那些 workspace 的 .git 会一起消失，"
            "它们已有的改动将无法再生成 diff。\n"
            "请等这些任务结束后重试；若它们已经不存在，"
            f"删掉 {cache / 'worktrees'} 下对应的目录再重试。"
        )

    def _check_allowed(self, src: RepoSource) -> None:
        if src.is_local and not self.allow_local:
            raise WorkspaceError(
                f"本地路径不被允许: {src.raw}\n"
                "该入口只接受远端仓库 URL（03 §29：API 拒绝 file:// 与本地路径）。"
            )

    def _worktree_add(self, cache: Path, commit: str, target: Path) -> None:
        result = run_git(
            # --detach：workspace 必须是游离 HEAD。若停在分支上，之后任何
            # checkout/commit 都会改动缓存里的分支指针，下一个任务看到的就是
            # 被上一个任务移动过的分支，而症状是"base_commit 没错但代码不对"。
            ["worktree", "add", "--detach", "--quiet", str(target), commit],
            cwd=cache,
            check=False,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            raise WorkspaceError(
                "git worktree add 失败，无法从缓存创建工作副本。\n"
                f"  缓存: {cache}\n  目标: {target}\n  提交: {commit}\n"
                f"  stderr: {result.stderr.strip()}"
            )

    def _fetch_base(self, cache: Path, commit: str, target: Path) -> None:
        """新建一个空仓库，只从缓存 fetch base 的祖先与已合入 base 的 tag。

        与 ``copy`` 的区别在于**从不传入** base 之后的对象，而不是传进来再删：
        没有远端（直接给缓存路径，不 ``remote add``），没有分支。实测（requests
        v2.31.0，6155 个祖先提交）2.00s，比 copy 的 2.36s 快，``.git`` 体积相同。

        ``--no-tags`` 是声明意图而不是承重的：git 的自动跟随只拉"指向本次已下载
        对象"的 tag，而下载的全是 base 的祖先，跟进来的 tag 指向的都是 base 里
        已有的对象，不会引入泄露。变异验证里删掉它没有测试变红，这是等价变异，
        不是漏测。真正挡住泄露的是 refspec 只有 base 与 ``--merged`` 的 tag。

        tag 只取 ``--merged <base>`` 的那些：``git describe`` 与依赖它推导版本号
        的测试（setuptools-scm 之类）需要它们，而 copy 模式因为删光了全部 ref
        会报 ``fatal: No names found``。
        """
        init = run_git(
            # --template=：不要 hooks 样例与 info/exclude 之类的模板文件，
            # workspace 里不该有任何不来自仓库本身的东西。
            ["init", "--quiet", "--template=", str(target)],
            check=False,
            timeout=self.timeout,
        )
        if init.returncode != 0:
            raise WorkspaceError(
                f"初始化 workspace 失败: {target}\n  stderr: {init.stderr.strip()}"
            )
        self._apply_repo_config(target)

        tags = run_git(
            ["for-each-ref", "--merged", commit, "--format=%(refname)", "refs/tags/"],
            cwd=cache,
            check=False,
            timeout=self.timeout,
        )
        if tags.returncode != 0:
            raise WorkspaceError(
                f"列出已合入 base 的 tag 失败。\n  缓存: {cache}\n  stderr: {tags.stderr.strip()}"
            )
        # 分批放在命令行上，而不是 ``fetch --stdin``：tag 多的仓库（数千个）
        # 会撞上 Windows 32K 的命令行上限，但 stdin 在这里不能用——``run_git``
        # 是文本模式，Windows 上写进去的 ``\n`` 会变成 ``\r\n``，实测 git 把
        # refspec 读成 ``<sha>\r`` 并报 ``invalid refspec``。第一批带上 base
        # 本身，它传完了全部祖先对象；后续批次只补 annotated tag 对象，很便宜。
        refspecs = [f"+{ref}:{ref}" for ref in tags.lines]
        for batch in _batched([commit, *refspecs], limit=_CMDLINE_BUDGET):
            fetched = run_git(
                [
                    "-c",
                    "gc.auto=0",
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    *(["--depth=1"] if (cache / "shallow").is_file() else []),
                    str(cache),
                    *batch,
                ],
                cwd=target,
                check=False,
                timeout=self.timeout,
            )
            if fetched.returncode != 0:
                raise WorkspaceError(
                    "从缓存 fetch base commit 失败。\n"
                    f"  缓存: {cache}\n  目标: {target}\n  提交: {commit}\n"
                    f"  stderr: {fetched.stderr.strip()}"
                )
        checkout = run_git(
            ["checkout", "--detach", "--quiet", commit],
            cwd=target,
            check=False,
            timeout=self.timeout,
        )
        if checkout.returncode != 0:
            raise WorkspaceError(
                f"在 workspace 中 checkout {commit} 失败。\n  stderr: {checkout.stderr.strip()}"
            )

    def _clone_copy(self, cache: Path, commit: str, target: Path) -> None:
        result = run_git(
            # --no-hardlinks：不复用对象文件，workspace 与缓存完全独立。
            # 没有它时 git 会硬链接对象，Windows 上磁盘占用看起来很小，
            # 但一个在 workspace 里误删对象的操作会连带毁掉缓存。
            ["clone", "--no-hardlinks", "--no-checkout", "--quiet", str(cache), str(target)],
            timeout=self.timeout,
            check=False,
        )
        if result.returncode != 0:
            raise WorkspaceError(
                "复制缓存创建 workspace 失败。\n"
                f"  缓存: {cache}\n  目标: {target}\n  stderr: {result.stderr.strip()}"
            )
        self._apply_repo_config(target)
        checkout = run_git(
            ["checkout", "--detach", "--quiet", commit],
            cwd=target,
            check=False,
            timeout=self.timeout,
        )
        if checkout.returncode != 0:
            raise WorkspaceError(
                f"在 workspace 中 checkout {commit} 失败。\n  stderr: {checkout.stderr.strip()}"
            )
        self._truncate_history_to(target, commit)

    def _truncate_history_to(self, target: Path, commit: str) -> None:
        """让 workspace 里**不存在** base 之后的提交（03 §11.2）。

        复制模式的 git 目录就在 workspace 内部，``git clone`` 会把远端全部分支
        与标签一起复制进来。实测修复前在 workspace 里：

        * ``git log --all`` → ``473f4ad fix: return 2 | 2721cb4 initial``
        * ``git show v-fix:app.py`` → ``'def f():\\n    return 2  # THE FIX'``

        这不是"结果偏高"的问题：SWE-bench 的 gold patch 就在那里，agent 或它
        跑起来的测试代码可以直接读出来，于是 resolve 率这个数字**不能报告**。

        做法是断掉一切指向 base 之外的引用，再把因此不可达的对象真的删掉——
        只删引用不够，``git cat-file --batch-all-objects`` 仍然能把它们列出来。
        base 的祖先全部保留（R-13 要按 ``git log base_commit`` 读修改历史），
        被删掉的只有 base 之后的部分。

        默认的 fetch 模式不走这里——它从一开始就不传那些对象。
        worktree 模式与缓存共享对象库和全部 refs，
        从 workspace 里就能看到缓存的引用，本地无论如何删不干净；那种模式的
        隔离靠沙箱只挂载 workspace 本身（缓存在 workspace 之外，容器里没有
        对象库可用）。见 docs/05 §23.1 Q12。
        """
        # 先断远端，否则删掉的引用会被一次 fetch 重新拉回来。
        run_git(["remote", "remove", "origin"], cwd=target, check=False, timeout=self.timeout)
        refs = run_git(
            ["for-each-ref", "--format=%(refname)"], cwd=target, check=False, timeout=self.timeout
        )
        for ref in refs.lines:
            run_git(["update-ref", "-d", ref], cwd=target, check=False, timeout=self.timeout)
        # reflog 还记着原来那些提交，不清掉的话它们仍然可达。
        run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            cwd=target,
            check=False,
            timeout=self.timeout,
        )
        run_git(["gc", "--prune=now", "--quiet"], cwd=target, check=False, timeout=self.timeout)

        remaining = run_git(
            ["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
            cwd=target,
            check=False,
            timeout=self.timeout,
        )
        if remaining.returncode != 0:
            raise WorkspaceError(
                f"截断历史时把 base commit 也弄丢了: {commit}\n  目标: {target}\n"
                "这是一个 bug，不是环境问题——workspace 必须始终含 base 及其祖先。"
            )

    def _apply_repo_config(self, repo: Path) -> None:
        """把 checkout 语义写进仓库自身的配置（02 §「Windows 环境差异」）。

        ``gitcmd`` 已经在每次调用时用 ``-c`` 覆盖了这两项，这里再写一份是为了让
        **不经过本模块**的 git 命令（沙箱里的构建脚本、用户手工排查）看到同样的
        行为。``git clone`` 不会复制 ``core.*``，所以必须显式设置。

        worktree 与它的缓存仓库共享同一个配置文件，因此对 worktree 调用这个
        方法等价于设置缓存——这正是我们要的。这也是"缓存只读"的唯一例外：
        写入的只有这两项配置，没有引用与对象。
        """
        for key, value in (("core.autocrlf", "false"), ("core.longpaths", "true")):
            run_git(["config", key, value], cwd=repo, check=False, timeout=self.timeout)

    def _explain_clone_failure(self, src: RepoSource, exc: GitError) -> str:
        stderr = exc.stderr.strip()
        hints: list[str] = []
        if src.is_local:
            assert src.local_path is not None
            if not src.local_path.exists():
                hints.append(f"本地路径不存在: {src.local_path}")
            elif not (src.local_path / ".git").exists():
                hints.append(
                    f"本地路径不是 git 仓库（没有 .git）: {src.local_path}。"
                    "本系统需要 git 历史来定位 base_commit 与统计修改频率。"
                )
        else:
            if "could not resolve host" in stderr.lower() or "unable to access" in stderr.lower():
                hints.append(
                    "无法访问远端。若是私有仓库，请确认凭据已配置——"
                    "本系统禁止交互式提示，认证失败会直接报错而不是等待输入。"
                )
        detail = ("\n".join(f"  {hint}" for hint in hints) + "\n") if hints else ""
        return f"git clone 失败: {src.normalized}\n{detail}  stderr: {stderr}"


def dirty_paths(workspace: Path, *, timeout: float = 120.0) -> list[str]:
    """``git status --porcelain`` 的非空行。"""
    result = run_git(["status", "--porcelain"], cwd=workspace, check=False, timeout=timeout)
    if result.returncode != 0:
        raise WorkspaceError(f"git status 失败（{workspace}）: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


#: 单条 git 命令里 refspec 的总字符数上限。Windows 的命令行上限是 32767，
#: 留出 git 路径、``-c`` 覆盖与缓存路径的余量。
_CMDLINE_BUDGET = 24_000


def _batched(items: list[str], *, limit: int) -> list[list[str]]:
    """按总字符数把 ``items`` 切成若干批，每批至少一项。"""
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for item in items:
        if current and size + len(item) + 1 > limit:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item) + 1
    if current:
        batches.append(current)
    return batches


def _staging_owner(dirname: str, target_name: str) -> int | None:
    """从暂存目录名里取出它的主人 pid，取不到返回 ``None``。

    名字形如 ``<name>.tmp-<pid>-<token>``。解析失败一律返回 ``None``——
    调用方据此**不删**它：宁可留一个陌生目录，也不要删掉认不出来的东西。
    """
    suffix = dirname[len(target_name) :]
    if not suffix.startswith(".tmp-"):
        return None
    pid_text = suffix[len(".tmp-") :].split("-", 1)[0]
    try:
        return int(pid_text)
    except ValueError:
        return None


def _is_bare_repo(path: Path) -> bool:
    """判断目录是不是一个可用的 bare 仓库。

    只检查 ``HEAD`` 不够：一次中断的克隆会留下 HEAD 却没有对象库，
    后续 ``rev-parse`` 会失败，而错误信息会指向"commit 不存在"这个
    完全错误的方向。
    """
    if not (path / "HEAD").exists():
        return False
    return (path / "objects").is_dir() and (path / "refs").is_dir()
