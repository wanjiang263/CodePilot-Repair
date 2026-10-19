"""E-15：官方 predictions.jsonl 写入器、状态机与 Patch 安全校验测试。

全部离线；不调用真实 LLM、不构建 Docker 镜像。补丁都是最小但结构真实的 git diff。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from codepilot.eval.swebench_prediction import (
    PatchValidationError,
    PredictionEntry,
    PredictionError,
    PredictionIntegrityError,
    PredictionManifest,
    PredictionRecord,
    PredictionStatus,
    PredictionWriter,
    normalize_patch,
    patch_files,
    patch_sha256,
)
from codepilot.eval.swebench_task import DATASET_NAME, DATASET_REVISION, SWEBenchDatasetRef

MODEL = "codepilot/glm-fixture"
RUN_ID = "cp-run-fixture"
INSTANCE = "django__django-10914"
OTHER_INSTANCE = "sqlfluff__sqlfluff-1625"


def _dataset(*, revision: str = DATASET_REVISION, split: str = "test") -> SWEBenchDatasetRef:
    return SWEBenchDatasetRef(name=DATASET_NAME, revision=revision, split=split)


def _modify_diff(*, path: str = "src/calc.py") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " value = 1\n"
        "-result = value + 1\n"
        "+result = value + 2\n"
    )


def _new_file_diff(*, path: str = "docs/new.txt") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        "+hello\n"
    )


def _delete_file_diff(*, path: str = "src/old.py") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        "index 1234567..0000000\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-bye\n"
    )


def _writer(
    path: Path,
    *,
    model: str = MODEL,
    run_id: str = RUN_ID,
    dataset: SWEBenchDatasetRef | None = None,
    allow_existing: bool = True,
    include_empty: bool = False,
) -> PredictionWriter:
    return PredictionWriter(
        path,
        model_name_or_path=model,
        run_id=run_id,
        dataset=dataset or _dataset(),
        allow_existing=allow_existing,
        include_empty=include_empty,
    )


def _entry(
    *,
    instance_id: str = INSTANCE,
    patch: str | None = None,
    status: PredictionStatus = PredictionStatus.PATCH_READY,
    model: str = MODEL,
    run_id: str = RUN_ID,
) -> PredictionEntry:
    text = _modify_diff() if patch is None else patch
    if status is PredictionStatus.PATCH_READY:
        return PredictionEntry.patch_ready(
            instance_id=instance_id,
            run_id=run_id,
            model_name_or_path=model,
            patch=text,
        )
    return PredictionEntry.not_ready(
        instance_id=instance_id,
        run_id=run_id,
        model_name_or_path=model,
        status=status,
        patch=text if status is PredictionStatus.INVALID_PATCH else "",
    )


# ---------------------------------------------------------------------------
# 官方三字段格式
# ---------------------------------------------------------------------------


def test_prediction_record_has_exactly_three_official_fields() -> None:
    record = PredictionRecord(
        instance_id=INSTANCE, model_name_or_path=MODEL, model_patch=_modify_diff()
    )
    assert set(record.model_dump()) == {
        "instance_id",
        "model_name_or_path",
        "model_patch",
    }
    with pytest.raises(ValidationError):
        PredictionRecord.model_validate(
            {
                "instance_id": INSTANCE,
                "model_name_or_path": MODEL,
                "model_patch": _modify_diff(),
                "resolved": True,
            }
        )
    with pytest.raises(ValidationError):
        PredictionRecord(instance_id="not-an-instance", model_name_or_path=MODEL, model_patch="")


def test_write_patch_ready_jsonl_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    writer.add(_entry(instance_id=OTHER_INSTANCE))
    writer.add(_entry())
    manifest = writer.write()

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 2
    ids = []
    for line in lines:
        payload: Any = json.loads(line)
        assert set(payload) == {"instance_id", "model_name_or_path", "model_patch"}
        assert payload["model_name_or_path"] == MODEL
        ids.append(payload["instance_id"])
    # 顺序稳定：instance_id 升序，与 add 顺序无关。
    assert ids == sorted(ids)

    records = PredictionWriter.load(path)
    assert [record.instance_id for record in records] == ids
    assert manifest.written == 2
    assert manifest.skipped == 0
    assert manifest.predictions_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest.predictions_bytes == len(path.read_bytes())
    assert manifest.predictions_file == path.name
    assert (tmp_path / "predictions.manifest.json").is_file()
    assert manifest.status_counts[PredictionStatus.PATCH_READY.value] == 2


def test_empty_patch_not_written_but_recorded_in_manifest(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    writer.add(_entry())
    writer.add(
        PredictionEntry.not_ready(
            instance_id=OTHER_INSTANCE,
            run_id=RUN_ID,
            model_name_or_path=MODEL,
            status=PredictionStatus.EMPTY_PATCH,
        )
    )
    manifest = writer.write()

    assert [record.instance_id for record in PredictionWriter.load(path)] == [INSTANCE]
    assert manifest.written == 1
    assert manifest.skipped == 1
    assert manifest.status_counts["EMPTY_PATCH"] == 1
    empty = next(entry for entry in manifest.entries if entry.instance_id == OTHER_INSTANCE)
    assert empty.status is PredictionStatus.EMPTY_PATCH
    assert empty.written is False


def test_include_empty_writes_blank_model_patch(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path, include_empty=True)
    writer.add(
        PredictionEntry.not_ready(
            instance_id=INSTANCE,
            run_id=RUN_ID,
            model_name_or_path=MODEL,
            status=PredictionStatus.EMPTY_PATCH,
        )
    )
    manifest = writer.write()
    assert manifest.include_empty is True
    records = PredictionWriter.load(path)
    assert records[0].model_patch == ""
    writer.verify_integrity()


def test_write_include_empty_override_persists(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)  # 构造时 include_empty=False
    writer.add(
        PredictionEntry.not_ready(
            instance_id=INSTANCE,
            run_id=RUN_ID,
            model_name_or_path=MODEL,
            status=PredictionStatus.EMPTY_PATCH,
        )
    )
    first = writer.write(include_empty=True)
    assert first.include_empty is True
    assert len(PredictionWriter.load(path)) == 1
    # 后续不带参数的 write() 不得把空 patch 行又悄悄删掉。
    second = writer.write()
    assert second.include_empty is True
    assert len(PredictionWriter.load(path)) == 1


def test_write_empty_prediction_set(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    manifest = _writer(path).write()
    assert path.read_bytes() == b""
    assert manifest.written == 0 and manifest.skipped == 0
    assert PredictionWriter.load(path) == []


# ---------------------------------------------------------------------------
# 状态机 / entry 不变量
# ---------------------------------------------------------------------------


def test_status_invariants_are_enforced() -> None:
    with pytest.raises(ValidationError, match="PATCH_READY"):
        PredictionEntry(
            instance_id=INSTANCE,
            status=PredictionStatus.PATCH_READY,
            patch="",
            run_id=RUN_ID,
            model_name_or_path=MODEL,
        )
    with pytest.raises(ValidationError, match="EMPTY_PATCH"):
        PredictionEntry(
            instance_id=INSTANCE,
            status=PredictionStatus.EMPTY_PATCH,
            patch="diff",
            run_id=RUN_ID,
            model_name_or_path=MODEL,
        )
    with pytest.raises(ValidationError, match="不应携带 patch"):
        PredictionEntry(
            instance_id=INSTANCE,
            status=PredictionStatus.ENV_ERROR,
            patch="diff",
            run_id=RUN_ID,
            model_name_or_path=MODEL,
        )
    with pytest.raises(ValidationError, match="patch_sha256"):
        PredictionEntry(
            instance_id=INSTANCE,
            status=PredictionStatus.PATCH_READY,
            patch=_modify_diff(),
            run_id=RUN_ID,
            model_name_or_path=MODEL,
            patch_sha256="0" * 64,
        )
    with pytest.raises(ValueError, match="patch_ready"):
        PredictionEntry.not_ready(
            instance_id=INSTANCE,
            run_id=RUN_ID,
            model_name_or_path=MODEL,
            status=PredictionStatus.PATCH_READY,
        )


def test_patch_ready_entry_fills_hash_automatically() -> None:
    entry = _entry()
    assert entry.patch_sha256 == patch_sha256(entry.patch)
    assert entry.status is PredictionStatus.PATCH_READY
    assert entry.status.writable and entry.status.records_patch


def test_failure_statuses_are_manifest_only(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    statuses = [
        PredictionStatus.MODEL_OUTPUT_INVALID,
        PredictionStatus.WORKFLOW_STOPPED,
        PredictionStatus.ENV_ERROR,
        PredictionStatus.BUDGET_EXHAUSTED,
    ]
    for offset, status in enumerate(statuses):
        writer.add(
            PredictionEntry.not_ready(
                instance_id=f"django__django-{11000 + offset}",
                run_id=RUN_ID,
                model_name_or_path=MODEL,
                status=status,
                error_kind=status.value.lower(),
            )
        )
    manifest = writer.write()
    assert path.read_bytes() == b""
    for status in statuses:
        assert manifest.status_counts[status.value] == 1
    assert all(entry.written is False for entry in manifest.entries)


def test_invalid_patch_records_raw_hash_but_is_not_written(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    invalid_patch = "this is not a diff"
    writer.add(
        PredictionEntry.not_ready(
            instance_id=INSTANCE,
            run_id=RUN_ID,
            model_name_or_path=MODEL,
            status=PredictionStatus.INVALID_PATCH,
            patch=invalid_patch,
        )
    )
    manifest = writer.write()
    assert path.read_bytes() == b""
    entry = manifest.entries[0]
    assert entry.status is PredictionStatus.INVALID_PATCH
    assert entry.written is False
    assert entry.patch_sha256 == patch_sha256(invalid_patch)


# ---------------------------------------------------------------------------
# Patch 规范化 / 安全校验
# ---------------------------------------------------------------------------


def test_normalization_preserves_whitespace_and_normalizes_newlines() -> None:
    path = "src/calc.py"
    crlf = (
        f"diff --git a/{path} b/{path}\r\n"
        f"--- a/{path}\r\n"
        f"+++ b/{path}\r\n"
        "@@ -1,2 +1,2 @@\r\n"
        " value = 1\r\n"
        "-\told = 1   \r\n"
        "+\tnew = 2   \r\n"
    )
    normalized = normalize_patch(crlf)
    assert "\r" not in normalized
    assert normalized.endswith("\n") and not normalized.endswith("\n\n")
    assert "-\told = 1   \n" in normalized
    assert "+\tnew = 2   \n" in normalized


def test_multifile_add_and_delete_are_supported() -> None:
    patch = _modify_diff() + _new_file_diff() + _delete_file_diff()
    normalized = normalize_patch(patch)
    assert patch_files(normalized) == ("src/calc.py", "docs/new.txt", "src/old.py")
    assert patch_sha256(normalize_patch(patch)) == patch_sha256(normalized)


def test_no_newline_marker_is_preserved() -> None:
    """`\\ No newline at end of file` 是 apply 语义的一部分，不能被重排/丢掉。"""
    path = "src/calc.py"
    patch = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "\\ No newline at end of file\n"
    )
    normalized = normalize_patch(patch)
    assert normalized.count("\\ No newline at end of file") == 2
    assert patch_files(normalized) == (path,)
    # 多文件场景：第一个文件带标记时，后续文件仍能被正确切分。
    multi = patch + _new_file_diff()
    normalized_multi = normalize_patch(multi)
    assert "\\ No newline at end of file\n" in normalized_multi
    assert patch_files(normalized_multi) == (path, "docs/new.txt")


def test_multiple_hunks_in_same_file_are_preserved() -> None:
    path = "src/calc.py"
    patch = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        "@@ -10,2 +10,2 @@\n"
        " c\n"
        "-d\n"
        "+D\n"
    )
    normalized = normalize_patch(patch)
    assert normalized.count("@@ ") == 2
    assert patch_files(normalized) == (path,)


def test_hunk_content_starting_with_marker_prefix_is_not_a_new_file() -> None:
    """删除 SQL/Lua 注释行 `-- a/comment` 时 diff 行是 `--- a/comment`；
    不能靠前缀把它误判成新文件标记（行数计数才可靠）。"""
    path = "src/query.sql"
    patch = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        "--- a/comment\n"
        "+-- b/comment\n"
        " keep\n"
    )
    normalized = normalize_patch(patch)
    assert patch_files(normalized) == (path,)
    assert "--- a/comment" in normalized


def test_mode_only_patch_is_supported() -> None:
    """纯 chmod 补丁（无 hunk）在 git apply 中合法，不应记 INVALID_PATCH。"""
    path = "scripts/run.sh"
    patch = f"diff --git a/{path} b/{path}\nold mode 100644\nnew mode 100755\n"
    assert normalize_patch(patch) == patch
    assert patch_files(patch) == (path,)


def test_rename_is_rejected_but_truncated_hunk_is_preserved() -> None:
    rename = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    with pytest.raises(PatchValidationError, match=r"重命名|前缀"):
        normalize_patch(rename)
    # 截断 hunk 结构上不完整，但写入器不替官方 harness 判 apply 成败：
    # 保留原文，官方评测会给出 PATCH_APPLY_FAILED。
    truncated = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,3 +1,3 @@\n"
        " only-one-line\n"
    )
    assert normalize_patch(truncated) == truncated
    assert patch_files(truncated) == ("src/calc.py",)


@pytest.mark.parametrize(
    "bad_patch",
    [
        "diff --git a//etc/passwd b//etc/passwd\n--- a//etc/passwd\n+++ b//etc/passwd\n"
        "@@ -1 +1 @@\n-a\n+b\n",
        "diff --git a/../secret.py b/../secret.py\n--- a/../secret.py\n+++ b/../secret.py\n"
        "@@ -1 +1 @@\n-a\n+b\n",
        "diff --git a/.git/config b/.git/config\n--- a/.git/config\n+++ b/.git/config\n"
        "@@ -1 +1 @@\n-a\n+b\n",
        "diff --git a/eval/run.py b/eval/run.py\n--- a/eval/run.py\n+++ b/eval/run.py\n"
        "@@ -1 +1 @@\n-a\n+b\n",
        "diff --git a/src\\calc.py b/src\\calc.py\n--- a/src\\calc.py\n+++ b/src\\calc.py\n"
        "@@ -1 +1 @@\n-a\n+b\n",
        "diff --git a/C:/tmp/x.py b/C:/tmp/x.py\n--- a/C:/tmp/x.py\n+++ b/C:/tmp/x.py\n"
        "@@ -1 +1 @@\n-a\n+b\n",
    ],
)
def test_unsafe_or_absolute_paths_are_rejected(bad_patch: str) -> None:
    with pytest.raises(PatchValidationError):
        normalize_patch(bad_patch)


@pytest.mark.parametrize(
    "not_a_diff",
    [
        "",
        "   ",
        "print('hello')\n",
        "diff --git a/foo.py b/foo.py\n",
    ],
)
def test_non_diff_inputs_are_rejected(not_a_diff: str) -> None:
    with pytest.raises(PatchValidationError):
        normalize_patch(not_a_diff)


def test_headerless_unified_diff_is_supported() -> None:
    """模型常直接输出 ``--- a/f`` / ``+++ b/f``；git apply 接受，写入器也应接受。"""
    headerless = "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1 +1 @@\n-old\n+new\n"
    normalized = normalize_patch(headerless)
    assert patch_files(normalized) == ("src/calc.py",)
    assert "diff --git" not in normalized
    with pytest.raises(PatchValidationError, match="diff --git"):
        # 多文件但没有 diff --git 头时边界不可靠，拒绝而不是猜。
        normalize_patch(headerless + "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(PatchValidationError):
        normalize_patch("--- a/../evil.py\n+++ b/../evil.py\n@@ -1 +1 @@\n-a\n+b\n")


def test_reproduction_tests_are_excluded_from_delivered_patch() -> None:
    repro = _modify_diff(path="tests/_codepilot_repro/test_bug.py")
    patch = repro + _modify_diff()
    normalized = normalize_patch(patch)
    assert patch_files(normalized) == ("src/calc.py",)
    with pytest.raises(PatchValidationError, match="_codepilot_repro"):
        normalize_patch(repro)


def test_add_rejects_unsafe_patch(tmp_path: Path) -> None:
    writer = _writer(tmp_path / "predictions.jsonl")
    with pytest.raises(PredictionError, match="安全校验"):
        writer.add(_entry(patch=_modify_diff(path="/etc/passwd")))


# ---------------------------------------------------------------------------
# 去重 / 顺序 / 覆盖
# ---------------------------------------------------------------------------


def test_duplicate_instance_last_write_wins(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    first = _modify_diff().replace("value + 2", "value + 3")
    writer.add(_entry(patch=first))
    writer.add(_entry())
    manifest = writer.write()
    assert manifest.overwrites == 1
    records = PredictionWriter.load(path)
    assert len(records) == 1
    assert records[0].model_patch == normalize_patch(_modify_diff())


# ---------------------------------------------------------------------------
# 原子写 / 恢复 / 完整性
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_old_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    writer.add(_entry())
    writer.write()
    original = path.read_bytes()

    def fail_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("replace failed")

    writer.add(_entry(instance_id=OTHER_INSTANCE))
    monkeypatch.setattr("codepilot.eval.swebench_prediction.os.replace", fail_replace)
    with pytest.raises(PredictionError, match="原子写失败"):
        writer.write()
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_resume_keeps_identity_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    first = _writer(path)
    first.add(_entry())
    first.add(_entry(instance_id=OTHER_INSTANCE))
    first.write()

    resumed = _writer(path)
    assert {entry.instance_id for entry in resumed.entries} == {INSTANCE, OTHER_INSTANCE}
    resumed.add(
        PredictionEntry.not_ready(
            instance_id="django__django-11000",
            run_id=RUN_ID,
            model_name_or_path=MODEL,
            status=PredictionStatus.ENV_ERROR,
        )
    )
    manifest = resumed.write()
    assert manifest.written == 2
    assert manifest.skipped == 1
    assert manifest.status_counts["ENV_ERROR"] == 1
    resumed.verify_integrity()
    assert len(PredictionWriter.load(path)) == 2


def test_resume_preserves_invalid_patch_hash(tmp_path: Path) -> None:
    """断点恢复只从 manifest 拿得到被拒补丁的 hash（原文在 artifact 里），不能丢。"""
    path = tmp_path / "predictions.jsonl"
    raw = "this is not a diff"
    writer = _writer(path)
    writer.add(
        PredictionEntry.not_ready(
            instance_id=INSTANCE,
            run_id=RUN_ID,
            model_name_or_path=MODEL,
            status=PredictionStatus.INVALID_PATCH,
            patch=raw,
        )
    )
    writer.write()
    resumed = _writer(path)
    manifest = resumed.write()
    entry = manifest.entries[0]
    assert entry.status is PredictionStatus.INVALID_PATCH
    assert entry.patch_sha256 == patch_sha256(raw)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"run_id": "another-run"}, "run_id"),
        ({"model": "other/model"}, "model"),
        ({"dataset": _dataset(revision="c" * 40)}, "revision"),
    ],
)
def test_resume_rejects_changed_identity(
    tmp_path: Path, kwargs: dict[str, Any], message: str
) -> None:
    path = tmp_path / "predictions.jsonl"
    _writer(path).write()
    with pytest.raises(PredictionIntegrityError, match=message):
        _writer(path, **kwargs)


def test_resume_rejects_incomplete_files(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    _writer(path).write()
    (tmp_path / "predictions.manifest.json").unlink()
    with pytest.raises(PredictionIntegrityError, match="不完整"):
        _writer(path)


def test_resume_rejects_include_empty_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    _writer(path, include_empty=True).write()
    with pytest.raises(PredictionIntegrityError, match="include_empty"):
        _writer(path)


def test_allow_existing_false_rejects_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    _writer(path).write()
    with pytest.raises(PredictionError, match="allow_existing=False"):
        _writer(path, allow_existing=False)


def test_verify_integrity_detects_tampered_predictions(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    writer.add(_entry())
    writer.write()
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(PredictionIntegrityError, match="sha256"):
        writer.verify_integrity()


def test_verify_integrity_detects_manifest_status_tamper(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    writer.add(_entry())
    writer.write()
    manifest_path = tmp_path / "predictions.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["entries"][0]["status"] = PredictionStatus.ENV_ERROR.value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PredictionIntegrityError, match="不应写进"):
        writer.verify_integrity()


def test_verify_integrity_detects_missing_predictions(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    writer.write()
    path.unlink()
    with pytest.raises(PredictionIntegrityError, match="不存在"):
        writer.verify_integrity()


def test_write_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = _writer(path)
    writer.add(_entry())
    first = writer.write()
    first_bytes = path.read_bytes()
    second = writer.write()
    assert path.read_bytes() == first_bytes
    assert first.model_dump() == second.model_dump()


def test_manifest_binds_run_model_and_dataset(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    manifest = _writer(path).write()
    assert manifest.run_id == RUN_ID
    assert manifest.model_name_or_path == MODEL
    assert manifest.dataset_name == DATASET_NAME
    assert manifest.dataset_revision == DATASET_REVISION
    assert manifest.harness_version == "4.1.0"
    assert PredictionManifest.model_validate(manifest.model_dump()) == manifest


# ---------------------------------------------------------------------------
# load 的严格性
# ---------------------------------------------------------------------------


def test_load_rejects_extra_keys_and_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    record = {
        "instance_id": INSTANCE,
        "model_name_or_path": MODEL,
        "model_patch": normalize_patch(_modify_diff()),
    }
    path.write_text(json.dumps({**record, "resolved": True}) + "\n", encoding="utf-8")
    with pytest.raises(PredictionIntegrityError, match="字段无效"):
        PredictionWriter.load(path)
    path.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(PredictionIntegrityError, match="合法 JSON"):
        PredictionWriter.load(path)
    # 空文件（等价于"没有任何预测"）是合法的；但已有记录中间出现空行是损坏。
    path.write_text("\n", encoding="utf-8")
    assert PredictionWriter.load(path) == []
    path.write_text(json.dumps(record) + "\n\n", encoding="utf-8")
    with pytest.raises(PredictionIntegrityError, match="空行"):
        PredictionWriter.load(path)


def test_load_rejects_duplicate_instance_ids(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    record = {
        "instance_id": INSTANCE,
        "model_name_or_path": MODEL,
        "model_patch": normalize_patch(_modify_diff()),
    }
    line = json.dumps(record)
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")
    with pytest.raises(PredictionIntegrityError, match="重复"):
        PredictionWriter.load(path)


def test_writer_rejects_mismatched_entry_identity(tmp_path: Path) -> None:
    writer = _writer(tmp_path / "predictions.jsonl")
    with pytest.raises(PredictionError, match="run_id"):
        writer.add(_entry(run_id="other-run"))
    with pytest.raises(PredictionError, match="model_name_or_path"):
        writer.add(_entry(model="other/model"))
