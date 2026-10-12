import pytest

from codepilot.context.skeleton import SkeletonBuilder, SkeletonError
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from support.gitfixture import init_repo


def indexed(stores, root, files):
    fixture = init_repo(root, files)
    workspace = Workspace(
        path=root,
        source=normalize_source(str(root)),
        base_commit=fixture.head,
        repository_id="skeleton",
    )
    Indexer().index(stores, workspace)
    return RepositoryIndex(stores, "skeleton")


def test_structure_without_bodies(stores, tmp_path):
    root = tmp_path / "repo"
    index = indexed(
        stores,
        root,
        {
            "a.py": """class Outer(Base):
    class Inner:
        @staticmethod
        async def run(
            x: int = 2,
        ) -> str:
            # SECRET_COMMENT
            return "SECRET_BODY"
    @classmethod
    def create(cls):
        def nested(a=1):
            return "SECRET_NESTED"
        return nested()
def ordinary(x): return "SECRET_INLINE"
"""
        },
    )
    builder = SkeletonBuilder(root, index)
    skeleton = builder.build("a.py")
    assert "class Outer(Base)" in skeleton.text
    assert "@staticmethod" in skeleton.text and "@classmethod" in skeleton.text
    assert "async def run" in skeleton.text and "x: int = 2" in skeleton.text
    assert "<locals>.nested" in skeleton.text and "class Inner" in skeleton.text
    assert "SECRET" not in skeleton.text and "return" not in skeleton.text
    assert skeleton == builder.build("a.py")
    assert [(s.id, s.start_line, s.end_line) for s in skeleton.symbols] == [
        (s.id, s.start_line, s.end_line)
        for s in sorted(
            index.symbol_objects(), key=lambda s: (s.start_line, s.qualified_name, s.id)
        )
        if s.kind != "module"
    ]


def test_empty_and_syntax_error(stores, tmp_path):
    root = tmp_path / "repo"
    index = indexed(stores, root, {"empty.py": "", "broken.py": "def nope(:\n"})
    builder = SkeletonBuilder(root, index)
    assert builder.build("empty.py").symbols == ()
    broken = builder.build("broken.py")
    assert broken.parse_error and broken.symbols == ()
    assert "def nope" not in broken.text


@pytest.mark.parametrize(
    "path", ["../outside.py", "/outside.py", "C:\\outside.py", "a.py:2", "missing.py"]
)
def test_safe_paths(stores, tmp_path, path):
    root = tmp_path / "repo"
    index = indexed(stores, root, {"a.py": "def f(): pass\n"})
    with pytest.raises(SkeletonError):
        SkeletonBuilder(root, index).build(path)


def test_stale_index_and_encoding(stores, tmp_path):
    root = tmp_path / "repo"
    index = indexed(stores, root, {"a.py": "def f(): pass\n"})
    (root / "a.py").write_text("def g(): pass\n", encoding="utf-8")
    with pytest.raises(SkeletonError, match="stale"):
        SkeletonBuilder(root, index).build("a.py")


def test_real_symlink_escape(stores, tmp_path):
    root = tmp_path / "repo"
    index = indexed(stores, root, {"a.py": "def f(): pass\n"})
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET", encoding="utf-8")
    (root / "a.py").unlink()
    try:
        (root / "a.py").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Cannot create real symlink on this host: {error}; Linux CI NOT RUN")
    with pytest.raises(SkeletonError, match="Linked|escapes"):
        SkeletonBuilder(root, index).build("a.py")
