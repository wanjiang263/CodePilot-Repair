"""F-19 的证明：目录结构逐项匹配 02 §18.3；写入可读回；路径不逃逸 run 根。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.artifacts import (
    DIAGNOSIS_JSON,
    EVENTS_JSONL,
    JUNIT_XML,
    PATCH_DIFF,
    REPORT_MD,
    STATE_FINAL,
    VALIDATION_JSON,
    ArtifactStore,
)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts", run_id="run_20261008_001")


def test_run_dir_layout(store: ArtifactStore) -> None:
    assert store.run_dir == (store.run_dir.parent.parent / "runs" / "run_20261008_001")
    assert store.run_dir.is_dir()


def test_attempt_dir_is_zero_padded(store: ArtifactStore) -> None:
    """两位补零是为了 `attempts/` 的字典序等于数字序——排序错了会把第 10 轮
    排到第 2 轮前面，而 Run Report 是按目录列轮次的。"""
    assert store.attempt_dir(1).name == "01"
    assert store.attempt_dir(12).name == "12"
    assert sorted(p.name for p in [store.attempt_dir(n) for n in (2, 10, 1)]) == [
        "01",
        "02",
        "10",
    ]


def test_attempt_dir_rejects_zero(store: ArtifactStore) -> None:
    with pytest.raises(ValueError):
        store.attempt_dir(0)


def test_documented_file_names_can_all_be_written(store: ArtifactStore) -> None:
    """把 02 §18.3 目录树里出现的文件名逐个写一遍。"""
    for name in (
        "config.yaml",
        STATE_FINAL,
        EVENTS_JSONL,
        "llm_calls.jsonl",
        "retrieval.json",
        "regression.json",
        "review.json",
        "report.json",
        REPORT_MD,
    ):
        assert store.write_text(name, "x").exists()
    for name in (
        PATCH_DIFF,
        VALIDATION_JSON,
        JUNIT_XML,
        "stdout.log",
        "stderr.log",
        DIAGNOSIS_JSON,
    ):
        assert store.write_text(store.attempt_relpath(1, name), "x").exists()


def test_write_and_read_text_roundtrip(store: ArtifactStore) -> None:
    store.write_text("report.md", "# 报告\n内容\n")
    assert store.read_text("report.md") == "# 报告\n内容\n"


def test_write_and_read_json_roundtrip(store: ArtifactStore) -> None:
    payload = {"run_id": "run1", "attempts": [{"iteration": 1}], "cost": 0.42}
    store.write_json("report.json", payload)
    assert store.read_json("report.json") == payload


def test_json_is_written_as_utf8_not_escaped(store: ArtifactStore) -> None:
    """``ensure_ascii=True`` 会把中文写成 \\uXXXX，报告会变得没法读。"""
    store.write_json("report.json", {"summary": "空列表访问下标"})
    assert "空列表访问下标" in store.read_text("report.json")


def test_write_json_redacts_when_asked(store: ArtifactStore) -> None:
    store.write_json("config.yaml", {"api_key": "sk-abcdefghijklmn"}, redact_values=True)
    assert "sk-abcdefghijklmn" not in store.read_text("config.yaml")


def test_write_json_does_not_redact_by_default(store: ArtifactStore) -> None:
    """patch 与 junit 里本来就没有密钥；默认不遍历是为了避免大对象上的无谓开销。

    这是刻意的取舍，所以要有测试把它钉住——否则有人会以为"默认就是脱敏的"。
    """
    store.write_json("retrieval.json", {"sha256": "a" * 64})
    assert "a" * 64 in store.read_text("retrieval.json")


def test_write_bytes_roundtrip(store: ArtifactStore) -> None:
    store.write_bytes("junit.xml", b"<testsuite/>")
    assert store.path_for("junit.xml").read_bytes() == b"<testsuite/>"


def test_exists_does_not_create_anything(store: ArtifactStore) -> None:
    """存在性检查不该有副作用，否则目录树会看起来像有产物。"""
    assert store.exists("nope/deep/file.json") is False
    assert not (store.run_dir / "nope").exists()


def test_missing_file_read_raises_without_creating_dirs(store: ArtifactStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.read_text("missing/deep.json")
    assert not (store.run_dir / "missing").exists()


@pytest.mark.parametrize(
    "relpath",
    [
        "../escape.json",
        "../../escape.json",
        "attempts/../../escape.json",
        "nested/../../../../etc/passwd",
    ],
)
def test_path_escape_is_rejected(store: ArtifactStore, relpath: str) -> None:
    with pytest.raises(ValueError):
        store.write_text(relpath, "x")


@pytest.mark.parametrize("run_id", ["../evil", "a/b", "a\\b", "", "."])
def test_illegal_run_id_is_rejected(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError):
        ArtifactStore(root=tmp_path, run_id=run_id)


def test_path_for_creates_parent_directories(store: ArtifactStore) -> None:
    target = store.path_for("attempts/03/deep/nested.json")
    assert target.parent.is_dir()


def test_relpath_is_posix_and_relative(store: ArtifactStore) -> None:
    """Report 里的路径要跨平台可读；Windows 的反斜杠写进去在别处打不开。"""
    target = store.write_text(store.attempt_relpath(2, PATCH_DIFF), "diff")
    assert store.relpath(target) == "attempts/02/patch.diff"


def test_relpath_rejects_paths_outside_run(store: ArtifactStore, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        store.relpath(outside)


def test_two_stores_for_same_run_share_directory(tmp_path: Path) -> None:
    """恢复运行时的新进程会用同一个 run_id 再建一次 store。"""
    first = ArtifactStore(root=tmp_path, run_id="run1")
    first.write_text("report.md", "old")
    second = ArtifactStore(root=tmp_path, run_id="run1")
    assert second.read_text("report.md") == "old"
    assert first.run_dir == second.run_dir
