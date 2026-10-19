"""E-16 官方 harness 适配器测试。

全部用例 **Mock 子进程**：不启动真实 ``swebench.harness.run_evaluation``、不连 Docker、
不下载数据集、不调 LLM。官方格式来自 ``tests/fixtures/swebench_harness/`` 里由
``swebench==4.1.0`` 真实生成的夹具（help/argparse/report/predictions）。

覆盖任务书 E-16 §10.7 的强制条目：

* 命令拼装与参数合法（含与真实 ``--help``/argparse 夹具交叉校验）；
* 进程成功 / 非零退出 / 超时；
* Docker 不可用归因；
* 非法 prediction、空 patch、部分完成；
* 报告缺失 / 损坏 / 身份不符；
* 旧缓存污染、不同 patch 不误复用；
* Gold（ORACLE_CONTROL）不进入推理与主 predictions；
* 官方结果与内部 M2 状态不一致（官方结论不被内部状态推翻）；
* Artifact 保存与恢复。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from codepilot.artifacts import ArtifactStore
from codepilot.eval.swebench_harness import (
    DATASET_NAME,
    DATASET_REVISION,
    HARNESS_VERSION,
    ORACLE_CONTROL_MODEL_NAME,
    HarnessCacheError,
    HarnessCacheIdentity,
    HarnessConfig,
    HarnessControlKind,
    HarnessDiscrepancy,
    HarnessError,
    HarnessErrorKind,
    HarnessExecutionResult,
    HarnessInputError,
    HarnessReport,
    HarnessRunDecision,
    LocalSubprocessRunner,
    OfficialHarnessRunner,
    OfficialVerdict,
    PlatformInfo,
    SubprocessOutcome,
    compare_with_internal_m2,
    default_harness_python,
    derive_run_id,
    oracle_control_run_id,
    patch_set_digest,
    run_oracle_control,
    write_oracle_predictions,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "swebench_harness"
MODEL = "codepilot/glm-4.5"

ID_RESOLVED = "astropy__astropy-12907"
ID_UNRESOLVED = "astropy__astropy-14182"
ID_EMPTY = "astropy__astropy-14365"
ID_ERROR = "astropy__astropy-14995"
ID_INCOMPLETE = "astropy__astropy-6938"
ALL_IDS = (ID_RESOLVED, ID_UNRESOLVED, ID_EMPTY, ID_ERROR, ID_INCOMPLETE)

PATCH = (
    "diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py\n"
    "--- a/astropy/modeling/separable.py\n"
    "+++ b/astropy/modeling/separable.py\n"
    "@@ -242,7 +242,7 @@ def _cstack(left, right):\n"
    "-            cright = np.zeros((left.n_outputs, right.n_outputs))\n"
    "+            cright = np.zeros((left.n_outputs, right.n_inputs))\n"
)


# ---------------------------------------------------------------------------
# 夹具与构造工具
# ---------------------------------------------------------------------------
def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_cli_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "official-cli-4.1.0.json").read_text(encoding="utf-8"))


def _load_report_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "official-report-4.1.0.json").read_text(encoding="utf-8"))


def _load_report_meta() -> dict[str, Any]:
    return json.loads((FIXTURES / "official-report-4.1.0.meta.json").read_text(encoding="utf-8"))


def _linux() -> PlatformInfo:
    return PlatformInfo(system="Linux", machine="x86_64", has_resource_module=True)


def _windows_native() -> PlatformInfo:
    return PlatformInfo(system="Windows", machine="AMD64", has_resource_module=False)


def _report_payload(
    *,
    total: int,
    resolved: tuple[str, ...] = (),
    unresolved: tuple[str, ...] = (),
    empty_patch: tuple[str, ...] = (),
    error: tuple[str, ...] = (),
    incomplete: tuple[str, ...] = (),
    submitted: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """按官方 schema_version=2 规则拼一份自洽报告。"""
    submitted_ids = (
        tuple(sorted(submitted))
        if submitted is not None
        else tuple(sorted({*resolved, *unresolved, *empty_patch, *error}))
    )
    completed = tuple(sorted({*resolved, *unresolved, *error}))
    return {
        "total_instances": total,
        "submitted_instances": len(submitted_ids),
        "completed_instances": len(completed),
        "resolved_instances": len(resolved),
        "unresolved_instances": len(unresolved),
        "empty_patch_instances": len(empty_patch),
        "error_instances": len(error),
        "completed_ids": list(completed),
        "incomplete_ids": list(incomplete),
        "empty_patch_ids": list(empty_patch),
        "submitted_ids": list(submitted_ids),
        "resolved_ids": list(resolved),
        "unresolved_ids": list(unresolved),
        "error_ids": list(error),
        "schema_version": 2,
    }


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    path.write_text(body + ("\n" if rows else ""), encoding="utf-8")
    return path


def _make_predictions(
    tmp_path: Path,
    patches: dict[str, str],
    *,
    model: str = MODEL,
    name: str = "predictions.jsonl",
) -> Path:
    rows = [
        {"instance_id": instance_id, "model_name_or_path": model, "model_patch": patch}
        for instance_id, patch in patches.items()
    ]
    return _write_jsonl(tmp_path / name, rows)  # type: ignore[arg-type]


def _patch_hashes(patches: dict[str, str]) -> dict[str, str]:
    return {instance_id: _sha(patch) for instance_id, patch in patches.items() if patch}


def _make_config(
    tmp_path: Path,
    predictions: Path,
    *,
    patches: dict[str, str] | None = None,
    run_id: str | None = None,
    instance_ids: tuple[str, ...] = (),
    report_dir: Path | None = None,
    **overrides: Any,
) -> HarnessConfig:
    hashes = _patch_hashes(patches or {})
    resolved_run_id = (
        run_id if run_id is not None else f"cp-{patch_set_digest(hashes)[:12]}-seed20261019"
    )
    kwargs: dict[str, Any] = {
        "predictions_path": predictions,
        "run_id": resolved_run_id,
        "report_dir": report_dir if report_dir is not None else tmp_path / "official-eval",
        "max_workers": 2,
        "timeout_seconds": 120,
        "dataset_name": DATASET_NAME,
        "split": "test",
        "instance_ids": instance_ids,
        "harness_python": None,
        "harness_version": HARNESS_VERSION,
        "model_name_or_path": MODEL,
    }
    kwargs.update(overrides)
    return HarnessConfig(**kwargs)


class FakeSubprocessRunner:
    """记录调用并可写夹具报告的假执行器。"""

    def __init__(
        self,
        outcome: SubprocessOutcome | None = None,
        *,
        on_run: Any = None,
    ) -> None:
        self.outcome = outcome if outcome is not None else SubprocessOutcome(returncode=0)
        self.on_run = on_run
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        argv: Any,
        *,
        cwd: Path,
        env: Any,
        timeout_seconds: int,
    ) -> SubprocessOutcome:
        self.calls.append(
            {
                "argv": tuple(argv),
                "cwd": cwd,
                "env": dict(env),
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.on_run is not None:
            self.on_run(cwd, dict(env))
        return self.outcome


def _write_report_on_run(config: HarnessConfig, payload: dict[str, Any]) -> Any:
    def _write(cwd: Path, _env: dict[str, str]) -> None:
        _write_json(cwd / config.expected_report_name, payload)

    return _write


def _run_with_report(
    tmp_path: Path,
    *,
    patches: dict[str, str],
    payload: dict[str, Any] | None,
    instance_ids: tuple[str, ...] = (),
    run_id: str | None = None,
    platform: PlatformInfo | None = None,
    artifacts: ArtifactStore | None = None,
    dataset_instances: tuple[str, ...] | None = None,
    on_run: Any = None,
    name: str = "predictions.jsonl",
    report_dir: Path | None = None,
) -> tuple[HarnessConfig, FakeSubprocessRunner, OfficialHarnessRunner, HarnessExecutionResult]:
    predictions = _make_predictions(tmp_path, patches, name=name)
    config = _make_config(
        tmp_path,
        predictions,
        patches=patches,
        run_id=run_id,
        instance_ids=instance_ids,
        report_dir=report_dir,
    )
    if on_run is None and payload is not None:
        on_run = _write_report_on_run(config, payload)
    fake = FakeSubprocessRunner(on_run=on_run)
    runner = OfficialHarnessRunner(
        config,
        runner=fake,
        platform=platform if platform is not None else _linux(),
        artifacts=artifacts,
        dataset_instances=dataset_instances,
    )
    return config, fake, runner, runner.run()


# ---------------------------------------------------------------------------
# 1. 命令拼装与官方 CLI 夹具交叉校验
# ---------------------------------------------------------------------------
def test_build_command_matches_real_swebench_4_1_0_cli(tmp_path: Path) -> None:
    cli = _load_cli_fixture()
    assert cli["provenance"]["harness_version"] == HARNESS_VERSION == "4.1.0"

    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(
        tmp_path,
        predictions,
        patches=patches,
        instance_ids=(ID_RESOLVED,),
        harness_python=tmp_path / "swebench-python.exe",
        max_workers=3,
        timeout_seconds=600,
        dataset_name="ORG/Custom",
    )
    command = OfficialHarnessRunner(
        config, runner=FakeSubprocessRunner(), platform=_linux()
    ).build_command(patch_hashes=_patch_hashes(patches))

    argv = command.argv
    assert argv[0] == str(tmp_path / "swebench-python.exe")
    assert argv[1:3] == ("-m", "swebench.harness.run_evaluation")
    assert argv[3:5] == ("-d", "ORG/Custom")
    assert argv[5:7] == ("-s", "test")
    assert argv[7:9] == ("-i", ID_RESOLVED)
    assert argv[9:11] == ("-p", str(predictions.resolve()))
    assert argv[11:13] == ("-id", config.run_id)
    assert argv[13:15] == ("--max_workers", "3")
    assert argv[15:17] == ("-t", "600")
    assert argv[17:19] == ("--report_dir", str((tmp_path / "official-eval").resolve()))

    # 每个选项都必须是 4.1.0 argparse 里真实存在的 flag（-m 是 Python 的）。
    real_flags = {flag for entry in cli["argparse"] for flag in entry["flags"]}
    used_flags = {
        token
        for token in argv
        if token.startswith("-") and token != "-m" and token not in {"-", "--"}
    }
    assert used_flags - real_flags == set(), f"未知官方参数: {used_flags - real_flags}"
    required = {entry["dest"] for entry in cli["argparse"] if entry["required"]}
    assert required == {"predictions_path", "run_id"}

    assert command.cwd == (tmp_path / "official-eval").resolve()
    assert command.expected_report.parent == command.cwd
    assert command.expected_report.name == f"codepilot__glm-4.5.{config.run_id}.json"
    assert command.env["PYTHONUTF8"] == "1"
    assert command.env["PYTHONIOENCODING"] == "utf-8"
    # env 只含显式注入变量，不能把父进程 PATH/API key 写进 artifact。
    assert "PATH" not in command.env and "OPENAI_API_KEY" not in command.env


def test_preview_command_works_before_predictions_exist(tmp_path: Path) -> None:
    """smoke --dry-run 的场景：还没写 predictions 也要能验证命令拼装。"""
    config = _make_config(
        tmp_path,
        tmp_path / "not-written-yet.jsonl",
        run_id="cp-000000000000-seed20261019",
        instance_ids=(ID_RESOLVED,),
        harness_python=tmp_path / "swebench-python.exe",
    )
    runner = OfficialHarnessRunner(
        config, runner=FakeSubprocessRunner(), platform=_windows_native()
    )
    command = runner.preview_command()
    assert command.argv[0] == str(tmp_path / "swebench-python.exe")
    assert "-m" in command.argv
    assert command.expected_report.name.endswith(".json")
    # preview 不做输入校验，但真实 run 一定会拒绝缺失的 predictions。
    with pytest.raises(HarnessInputError) as excinfo:
        runner.run()
    assert excinfo.value.kind is HarnessErrorKind.PREDICTIONS_MISSING
    with pytest.raises(HarnessInputError):
        runner.build_command(patch_hashes={})


def test_preview_and_build_command_agree_when_inputs_valid(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    runner = OfficialHarnessRunner(config, runner=FakeSubprocessRunner(), platform=_linux())
    assert (
        runner.preview_command().argv
        == runner.build_command(patch_hashes=_patch_hashes(patches)).argv
    )


def test_real_predictions_fixture_is_accepted(tmp_path: Path) -> None:
    """夹具里由官方格式写出的 jsonl 必须被本模块的严格校验接受。"""
    fixture = (FIXTURES / "official-predictions-4.1.0.jsonl").read_text(encoding="utf-8")
    path = tmp_path / "predictions.jsonl"
    path.write_text(fixture, encoding="utf-8")
    rows = [json.loads(line) for line in fixture.splitlines()]
    patches = {row["instance_id"]: row["model_patch"] for row in rows if row["model_patch"]}
    instance_ids = tuple(row["instance_id"] for row in rows)
    config = _make_config(tmp_path, path, patches=patches, instance_ids=instance_ids)
    runner = OfficialHarnessRunner(
        config, runner=FakeSubprocessRunner(), platform=_windows_native()
    )
    command = runner.build_command(patch_hashes=_patch_hashes(patches))
    assert command.argv[7] == "-i"
    assert command.argv[8] == instance_ids[0]
    assert command.argv[9] == instance_ids[1]
    assert command.argv[10] == "-p"


def test_report_filename_rule_matches_captured_official_report(tmp_path: Path) -> None:
    """夹具里官方真实产出的文件名必须能由配置复算出来。"""
    meta = _load_report_meta()
    provenance = meta["provenance"]
    predictions = _make_predictions(
        tmp_path,
        {ID_RESOLVED: PATCH},
        model=provenance["model_name_or_path"],
    )
    config = _make_config(
        tmp_path,
        predictions,
        run_id=provenance["run_id"],
        model_name_or_path=provenance["model_name_or_path"],
    )
    assert config.expected_report_name == provenance["report_filename"]
    assert config.expected_report_name == ("codepilot__glm-4.5.cp-0123456789ab-seed20261019.json")


def test_help_fixture_contains_all_flags_we_emit() -> None:
    help_text = (FIXTURES / "official-help-4.1.0.txt").read_text(encoding="utf-8")
    for flag in ("-d", "--dataset_name", "-s", "--split", "-i", "--instance_ids"):
        assert flag in help_text
    for flag in ("-p", "--predictions_path", "-id", "--run_id"):
        assert flag in help_text
    for flag in ("--max_workers", "-t", "--timeout", "--report_dir"):
        assert flag in help_text


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_workers": 0}, "max_workers"),
        ({"max_workers": True}, "整数"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": 5.5}, "整数"),
        ({"split": "train"}, "split"),
        ({"harness_version": "4.0.0"}, "swebench==4.1.0"),
        ({"model_name_or_path": "bad model/name"}, "model_name_or_path"),
        ({"run_id": "a/b"}, "run_id"),
        ({"dataset_revision": "not-a-revision"}, "dataset_revision"),
        ({"predictions_path": Path("x.txt")}, ".json"),
    ],
)
def test_config_rejects_illegal_parameters(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    predictions = _make_predictions(tmp_path, {ID_RESOLVED: PATCH}, name="predictions.jsonl")
    with pytest.raises(ValidationError, match=message):
        _make_config(tmp_path, predictions, patches={ID_RESOLVED: PATCH}, **overrides)


def test_config_rejects_duplicate_instance_ids(tmp_path: Path) -> None:
    predictions = _make_predictions(tmp_path, {ID_RESOLVED: PATCH})
    with pytest.raises(ValidationError, match="重复"):
        _make_config(
            tmp_path,
            predictions,
            instance_ids=(ID_RESOLVED, ID_RESOLVED),
        )


def test_build_command_rejects_patch_hash_mismatch(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    runner = OfficialHarnessRunner(config, runner=FakeSubprocessRunner(), platform=_linux())
    with pytest.raises(HarnessInputError) as excinfo:
        runner.build_command(patch_hashes={ID_RESOLVED: "0" * 64})
    assert excinfo.value.kind is HarnessErrorKind.PATCH_HASH_MISMATCH


def test_build_command_rejects_missing_predictions_file(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path,
        tmp_path / "missing.jsonl",
        run_id="cp-000000000000-seed",
    )
    runner = OfficialHarnessRunner(config, runner=FakeSubprocessRunner(), platform=_linux())
    with pytest.raises(HarnessInputError) as excinfo:
        runner.build_command(patch_hashes={})
    assert excinfo.value.kind is HarnessErrorKind.PREDICTIONS_MISSING


# ---------------------------------------------------------------------------
# 2. 非法 prediction / 空 patch / 实例归属
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([{"instance_id": ID_RESOLVED, "model_name_or_path": MODEL}], "字段必须"),
        (
            [
                {
                    "instance_id": ID_RESOLVED,
                    "model_name_or_path": MODEL,
                    "model_patch": PATCH,
                    "extra": 1,
                }
            ],
            "字段必须",
        ),
        (
            [
                {
                    "instance_id": ID_RESOLVED,
                    "model_name_or_path": "other/model",
                    "model_patch": PATCH,
                }
            ],
            "model_name_or_path",
        ),
        (
            [{"instance_id": "not-an-instance", "model_name_or_path": MODEL, "model_patch": PATCH}],
            "instance_id",
        ),
        (
            [
                {"instance_id": ID_RESOLVED, "model_name_or_path": MODEL, "model_patch": PATCH},
                {"instance_id": ID_RESOLVED, "model_name_or_path": MODEL, "model_patch": PATCH},
            ],
            "重复",
        ),
        ([], "为空"),
    ],
)
def test_predictions_reject_illegal_rows(
    tmp_path: Path, rows: list[dict[str, Any]], message: str
) -> None:
    path = _write_jsonl(tmp_path / "predictions.jsonl", rows)  # type: ignore[arg-type]
    config = _make_config(tmp_path, path, run_id="cp-000000000000-seed")
    runner = OfficialHarnessRunner(config, runner=FakeSubprocessRunner(), platform=_linux())
    with pytest.raises(HarnessInputError, match=message):
        runner.build_command(patch_hashes={})


def test_predictions_reject_blank_or_malformed_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    path.write_text('{"instance_id": "x"}\n\n', encoding="utf-8")
    config = _make_config(tmp_path, path, run_id="cp-000000000000-seed")
    runner = OfficialHarnessRunner(config, runner=FakeSubprocessRunner(), platform=_linux())
    with pytest.raises(HarnessInputError) as excinfo:
        runner.build_command(patch_hashes={})
    assert excinfo.value.kind is HarnessErrorKind.PREDICTIONS_INVALID

    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(HarnessInputError) as excinfo:
        runner.build_command(patch_hashes={})
    assert excinfo.value.kind is HarnessErrorKind.PREDICTIONS_INVALID


def test_predictions_must_be_within_requested_instance_ids(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH, ID_UNRESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, instance_ids=(ID_RESOLVED,))
    runner = OfficialHarnessRunner(config, runner=FakeSubprocessRunner(), platform=_linux())
    with pytest.raises(HarnessInputError) as excinfo:
        runner.build_command(patch_hashes=_patch_hashes(patches))
    assert excinfo.value.kind is HarnessErrorKind.DATASET_MISMATCH


def test_predictions_must_belong_to_dataset_universe(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    runner = OfficialHarnessRunner(
        config,
        runner=FakeSubprocessRunner(),
        platform=_linux(),
        dataset_instances=(ID_UNRESOLVED,),  # 当前数据集中没有 ID_RESOLVED
    )
    with pytest.raises(HarnessInputError) as excinfo:
        runner.build_command(patch_hashes=_patch_hashes(patches))
    assert excinfo.value.kind is HarnessErrorKind.DATASET_MISMATCH


def test_snapshot_pins_dataset_and_validates_manifest(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshot"
    snapshot = _write_jsonl(
        snapshot_dir / "test.jsonl",
        [{"instance_id": instance_id} for instance_id in ALL_IDS],  # type: ignore[arg-type]
    )
    _write_json(
        snapshot_dir / "manifest.json",
        {"dataset_name": DATASET_NAME, "revision": DATASET_REVISION},
    )
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(
        tmp_path,
        predictions,
        patches=patches,
        instance_ids=(ID_RESOLVED,),
        dataset_snapshot=snapshot,
    )
    command = OfficialHarnessRunner(
        config, runner=FakeSubprocessRunner(), platform=_linux()
    ).build_command(patch_hashes=_patch_hashes(patches))
    assert command.argv[3:5] == ("-d", str(snapshot.resolve()))

    # manifest 的 revision 与配置不同 → 在启动子进程前就拒绝。
    _write_json(
        snapshot_dir / "manifest.json",
        {"dataset_name": DATASET_NAME, "revision": "a" * 40},
    )
    with pytest.raises(HarnessInputError) as excinfo:
        OfficialHarnessRunner(config, runner=FakeSubprocessRunner(), platform=_linux())
    assert excinfo.value.kind is HarnessErrorKind.DATASET_MISMATCH


def test_snapshot_split_name_must_match_config(tmp_path: Path) -> None:
    snapshot = _write_jsonl(
        tmp_path / "snapshot" / "dev.jsonl",
        [{"instance_id": ID_RESOLVED}],  # type: ignore[arg-type]
    )
    predictions = _make_predictions(tmp_path, {ID_RESOLVED: PATCH})
    config = _make_config(
        tmp_path,
        predictions,
        patches={ID_RESOLVED: PATCH},
        instance_ids=(ID_RESOLVED,),
        dataset_snapshot=snapshot,
    )
    with pytest.raises(HarnessInputError) as excinfo:
        OfficialHarnessRunner(config, runner=FakeSubprocessRunner(), platform=_linux())
    assert excinfo.value.kind is HarnessErrorKind.DATASET_MISMATCH


# ---------------------------------------------------------------------------
# 3. 进程成功：真实报告夹具 + 逐实例结论
# ---------------------------------------------------------------------------
def test_run_success_parses_real_report_and_maps_verdicts(tmp_path: Path) -> None:
    payload = _load_report_fixture()
    patches = {
        ID_RESOLVED: PATCH,
        ID_UNRESOLVED: PATCH,
        ID_EMPTY: "",
        ID_ERROR: PATCH,
    }
    config, fake, _runner, result = _run_with_report(
        tmp_path,
        patches=patches,
        payload=payload,
        instance_ids=ALL_IDS,
    )

    assert len(fake.calls) == 1
    assert fake.calls[0]["timeout_seconds"] == 120
    assert result.decision is HarnessRunDecision.EXECUTED
    assert result.exit_code == 0
    assert result.error_kind is None
    assert result.reused is False
    assert result.report is not None
    assert (
        result.report_path == (tmp_path / "official-eval" / config.expected_report_name).resolve()
    )
    assert result.report_sha256 == result.report.source_sha256
    assert result.report.total_instances == 5
    assert result.report.resolved_rate == pytest.approx(1 / 5)

    assert result.verdict_for(ID_RESOLVED) is OfficialVerdict.RESOLVED
    assert result.verdict_for(ID_UNRESOLVED) is OfficialVerdict.UNRESOLVED
    assert result.verdict_for(ID_EMPTY) is OfficialVerdict.EMPTY_PATCH
    assert result.verdict_for(ID_ERROR) is OfficialVerdict.ERROR
    assert result.verdict_for(ID_INCOMPLETE) is OfficialVerdict.NOT_EVALUATED
    assert result.partial is True

    by_id = {item.instance_id: item for item in result.verdicts}
    assert by_id[ID_RESOLVED].resolved is True
    assert by_id[ID_UNRESOLVED].resolved is False
    # 空补丁没有官方 resolved/unresolved 判定：verdict 才是它的结论。
    assert by_id[ID_EMPTY].resolved is None
    assert by_id[ID_ERROR].resolved is None
    assert by_id[ID_INCOMPLETE].resolved is None
    assert by_id[ID_ERROR].error_kind is HarnessErrorKind.INSTANCE_ERROR

    # 官方聚合报告 + 身份 sidecar + codepilot 证据都落盘。
    report_dir = tmp_path / "official-eval"
    assert (report_dir / config.expected_report_name).is_file()
    sidecar = report_dir / (config.expected_report_name + ".identity.json")
    assert sidecar.is_file()
    evidence = report_dir / "_harness"
    for name in ("command.json", "identity.json", "report.json", "result.json", "stdout.log"):
        assert (evidence / name).is_file(), name
    saved = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    assert saved["decision"] == "EXECUTED"
    assert saved["verdicts"][0]["command"]["expected_report"].endswith(config.expected_report_name)


def test_run_detects_patch_apply_failure_from_per_instance_report(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH, ID_UNRESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(
        tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED, ID_UNRESOLVED)
    )
    payload = _report_payload(total=2, resolved=(ID_RESOLVED,), unresolved=(ID_UNRESOLVED,))
    applied_entry = json.loads(
        (FIXTURES / "official-instance-report-4.1.0.json").read_text(encoding="utf-8")
    )[ID_RESOLVED]

    def _on_run(cwd: Path, _env: dict[str, str]) -> None:
        _write_json(cwd / config.expected_report_name, payload)
        log_root = cwd / "logs" / "run_evaluation" / config.run_id / config.model_slug
        # 真实格式：patch 打上的实例 patch_successfully_applied=True。
        _write_json(log_root / ID_RESOLVED / "report.json", {ID_RESOLVED: applied_entry})
        # 聚合报告只写 unresolved；单实例报告才能区分"补丁没打上"。
        _write_json(
            log_root / ID_UNRESOLVED / "report.json",
            {
                ID_UNRESOLVED: {
                    **applied_entry,
                    "patch_successfully_applied": False,
                    "resolved": False,
                }
            },
        )

    fake = FakeSubprocessRunner(on_run=_on_run)
    result = OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()

    assert result.verdict_for(ID_RESOLVED) is OfficialVerdict.RESOLVED
    assert result.verdict_for(ID_UNRESOLVED) is OfficialVerdict.PATCH_APPLY_FAILED
    unresolved = next(item for item in result.verdicts if item.instance_id == ID_UNRESOLVED)
    # 非 RESOLVED/UNRESOLVED 的官方形态不带三态 bool（与 Owner F 的 SWEBenchEvaluation
    # 硬校验一致）；官方聚合口径见 report.unresolved_instances。
    assert unresolved.resolved is None
    assert result.report is not None and result.report.unresolved_instances == 1
    assert "patch_successfully_applied" in (unresolved.error_detail or "")


def test_run_returns_not_evaluated_for_unsubmitted_requested_instance(tmp_path: Path) -> None:
    payload = _report_payload(total=2, resolved=(ID_RESOLVED,), incomplete=(ID_UNRESOLVED,))
    # 只提交一条 prediction，但请求两条：另一条必须 NOT_EVALUATED 而不是 resolved/unresolved。
    _config2, fake2, _runner2, result2 = _run_with_report(
        tmp_path,
        patches={ID_RESOLVED: PATCH},
        payload=None,
        instance_ids=(ID_RESOLVED, ID_UNRESOLVED),
    )
    # 第一次给 payload=None，只是验证"报告缺失"；真正断言用下面的自定义报告。
    assert fake2.calls
    assert result2.error_kind is HarnessErrorKind.REPORT_MISSING

    predictions = _make_predictions(tmp_path, {ID_RESOLVED: PATCH}, name="predictions2.jsonl")
    config = _make_config(
        tmp_path,
        predictions,
        patches={ID_RESOLVED: PATCH},
        instance_ids=(ID_RESOLVED, ID_UNRESOLVED),
        report_dir=tmp_path / "eval2",
    )
    fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    result = OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()
    assert result.verdict_for(ID_RESOLVED) is OfficialVerdict.RESOLVED
    assert result.verdict_for(ID_UNRESOLVED) is OfficialVerdict.NOT_EVALUATED
    assert result.report is not None
    assert result.report.incomplete_ids == (ID_UNRESOLVED,)


# ---------------------------------------------------------------------------
# 4. 进程失败 / 超时 / Docker 不可用 / 报告缺失 / 损坏
# ---------------------------------------------------------------------------
def test_run_nonzero_exit_returns_process_failed(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    fake = FakeSubprocessRunner(
        outcome=SubprocessOutcome(
            returncode=2,
            stdout="Traceback (most recent call last): ...",
            stderr="ValueError: something exploded",
            duration_seconds=12.5,
        )
    )
    result = OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()

    assert result.exit_code == 2
    assert result.error_kind is HarnessErrorKind.PROCESS_FAILED
    assert "退出码 2" in (result.error_detail or "")
    assert result.verdict_for(ID_RESOLVED) is OfficialVerdict.MISSING
    assert result.verdicts[0].resolved is None
    assert not (
        tmp_path / "official-eval" / (config.expected_report_name + ".identity.json")
    ).exists()
    assert result.runtime_seconds == pytest.approx(12.5)
    assert "ValueError" in result.stderr_tail


def test_run_timeout_is_attributed_and_evidence_saved(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    fake = FakeSubprocessRunner(
        outcome=SubprocessOutcome(returncode=-1, timed_out=True, duration_seconds=120.0)
    )
    result = OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()

    assert result.timed_out is True
    assert result.error_kind is HarnessErrorKind.TIMEOUT
    assert "120s" in (result.error_detail or "")
    assert result.verdict_for(ID_RESOLVED) is OfficialVerdict.MISSING
    assert (tmp_path / "official-eval" / "_harness" / "result.json").is_file()
    assert not (
        tmp_path / "official-eval" / (config.expected_report_name + ".identity.json")
    ).exists()


def test_run_docker_unavailable_is_attributed(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    fake = FakeSubprocessRunner(
        outcome=SubprocessOutcome(
            returncode=1,
            stdout="",
            stderr=(
                "docker.errors.DockerException: Error while fetching server API version: "
                "('Connection aborted.', FileNotFoundError(2, 'No such file or directory'))"
            ),
        )
    )
    result = OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()
    assert result.error_kind is HarnessErrorKind.DOCKER_UNAVAILABLE
    assert "Docker" in (result.error_detail or "")


def test_run_zero_exit_without_report_is_report_missing(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    result = OfficialHarnessRunner(config, runner=FakeSubprocessRunner(), platform=_linux()).run()
    assert result.exit_code == 0
    assert result.error_kind is HarnessErrorKind.REPORT_MISSING
    assert result.report is None
    assert result.verdict_for(ID_RESOLVED) is OfficialVerdict.MISSING


def test_run_corrupted_report_is_not_interpreted(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    fake = FakeSubprocessRunner(
        on_run=lambda cwd, _env: (cwd / config.expected_report_name).write_text(
            "{not-json", encoding="utf-8"
        )
    )
    result = OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()
    assert result.error_kind is HarnessErrorKind.REPORT_CORRUPTED
    assert result.report is None
    assert result.verdict_for(ID_RESOLVED) is OfficialVerdict.MISSING
    assert not (
        tmp_path / "official-eval" / (config.expected_report_name + ".identity.json")
    ).exists()


def test_run_report_total_mismatch_is_rejected(tmp_path: Path) -> None:
    """报告 total_instances 与当前请求规模不符 → 报告身份不符，不能当结果。"""
    payload = _load_report_fixture()  # total_instances=5
    patches = {ID_RESOLVED: PATCH, ID_UNRESOLVED: PATCH, ID_EMPTY: "", ID_ERROR: PATCH}
    config = _make_config(
        tmp_path,
        _make_predictions(tmp_path, patches),
        patches=patches,
        instance_ids=(ID_RESOLVED, ID_UNRESOLVED, ID_EMPTY, ID_ERROR),  # 期望 total=4
    )
    fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    result = OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()
    assert result.error_kind is HarnessErrorKind.REPORT_IDENTITY_MISMATCH
    assert "total_instances=5" in (result.error_detail or "")
    assert result.report is None


# ---------------------------------------------------------------------------
# 5. Windows 原生 / dry-run / 缺少官方解释器
# ---------------------------------------------------------------------------
def test_windows_native_is_env_error_without_spawning(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    fake = FakeSubprocessRunner()
    result = OfficialHarnessRunner(config, runner=fake, platform=_windows_native()).run()

    assert fake.calls == []
    assert result.decision is HarnessRunDecision.PLATFORM_BLOCKED
    assert result.error_kind is HarnessErrorKind.ENV_ERROR
    detail = result.error_detail or ""
    assert "WSL2" in detail and "resource" in detail
    assert result.verdict_for(ID_RESOLVED) is OfficialVerdict.NOT_EVALUATED
    # 命令仍然可构造（dry-run/审计要求）。
    assert result.command.argv
    assert "-m" in result.command.argv


def test_dry_run_builds_command_without_executing(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH, ID_EMPTY: ""}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(
        tmp_path,
        predictions,
        patches=patches,
        instance_ids=(ID_RESOLVED, ID_EMPTY),
    )
    fake = FakeSubprocessRunner()
    result = OfficialHarnessRunner(config, runner=fake, platform=_linux()).run(dry_run=True)

    assert fake.calls == []
    assert result.decision is HarnessRunDecision.DRY_RUN
    assert result.exit_code is None
    assert result.error_kind is None
    assert result.report is None
    assert result.verdict_for(ID_RESOLVED) is OfficialVerdict.NOT_EVALUATED
    assert result.verdict_for(ID_EMPTY) is OfficialVerdict.EMPTY_PATCH


def test_missing_harness_python_is_resource_blocked(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(
        tmp_path,
        predictions,
        patches=patches,
        instance_ids=(ID_RESOLVED,),
        harness_python=tmp_path / "missing-python.exe",
    )
    fake = FakeSubprocessRunner()
    result = OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()
    assert fake.calls == []
    assert result.error_kind is HarnessErrorKind.RESOURCE_BLOCKED
    assert result.decision is HarnessRunDecision.PLATFORM_BLOCKED


def test_wsl_platform_prefixes_command(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(
        tmp_path,
        predictions,
        patches=patches,
        instance_ids=(ID_RESOLVED,),
        harness_python=Path("/home/ubuntu/.venv-swebench/bin/python"),
    )
    wsl = PlatformInfo(
        system="Windows",
        has_resource_module=False,
        use_wsl=True,
        wsl_executable=Path("C:/Windows/System32/wsl.exe"),
    )
    command = OfficialHarnessRunner(
        config, runner=FakeSubprocessRunner(), platform=wsl
    ).build_command(patch_hashes=_patch_hashes(patches))
    assert command.argv[0].endswith("wsl.exe")
    assert command.argv[1] == "-e"
    assert command.argv[2] == str(config.harness_python)
    assert command.argv[3:5] == ("-m", "swebench.harness.run_evaluation")


def test_platform_detect_reports_resource_availability() -> None:
    detected = PlatformInfo.detect()
    assert detected.system
    if detected.is_windows:
        assert detected.has_resource_module is False
        assert detected.official_execution_supported is False
    assert "system=" in detected.describe()


# ---------------------------------------------------------------------------
# 6. 缓存：复用 / 污染 / 不同 patch / 损坏 / 身份不符
# ---------------------------------------------------------------------------
def _cache_scenario(
    tmp_path: Path, *, run_id: str | None = None
) -> tuple[HarnessConfig, dict[str, str], dict[str, Any]]:
    patches = {ID_RESOLVED: PATCH}
    payload = _report_payload(total=1, resolved=(ID_RESOLVED,))
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(
        tmp_path,
        predictions,
        patches=patches,
        run_id=run_id,
        instance_ids=(ID_RESOLVED,),
    )
    return config, patches, payload


def test_cache_reuse_same_identity_skips_subprocess(tmp_path: Path) -> None:
    config, _patches, payload = _cache_scenario(tmp_path)
    first_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    first = OfficialHarnessRunner(config, runner=first_fake, platform=_linux()).run()
    assert first.decision is HarnessRunDecision.EXECUTED
    assert len(first_fake.calls) == 1

    second_fake = FakeSubprocessRunner()
    second = OfficialHarnessRunner(config.model_copy(), runner=second_fake, platform=_linux()).run()
    assert second_fake.calls == []
    assert second.decision is HarnessRunDecision.REUSED
    assert second.reused is True
    assert "身份一致" in (second.reused_reason or "")
    assert second.report is not None
    assert second.verdict_for(ID_RESOLVED) is OfficialVerdict.RESOLVED
    assert second.report_sha256 == first.report_sha256
    assert second.cache_identity is not None
    assert second.cache_identity.fingerprint == first.cache_identity.fingerprint  # type: ignore[union-attr]


def test_cache_rejects_different_patch_with_derived_run_id(tmp_path: Path) -> None:
    """不同补丁必须用新 run_id；沿用旧 run_id 时摘要在执行前就被拒绝。"""
    config, _patches, payload = _cache_scenario(tmp_path)
    first_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    OfficialHarnessRunner(config, runner=first_fake, platform=_linux()).run()

    changed_patches = {ID_RESOLVED: PATCH + "\n# changed\n"}
    changed_predictions = _make_predictions(tmp_path, changed_patches, name="changed.jsonl")
    stale_config = _make_config(
        tmp_path,
        changed_predictions,
        patches=changed_patches,
        run_id=config.run_id,  # 故意沿用旧 run_id
        instance_ids=(ID_RESOLVED,),
    )
    second_fake = FakeSubprocessRunner()
    with pytest.raises(HarnessCacheError) as excinfo:
        OfficialHarnessRunner(stale_config, runner=second_fake, platform=_linux()).run()
    assert excinfo.value.kind is HarnessErrorKind.CACHE_IDENTITY_MISMATCH
    assert "derive_run_id" in excinfo.value.detail
    assert second_fake.calls == []


def test_cache_rejects_sidecar_patch_mismatch_for_opaque_run_id(tmp_path: Path) -> None:
    config, _patches, payload = _cache_scenario(tmp_path, run_id="run-2026-10-19")
    first_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    OfficialHarnessRunner(config, runner=first_fake, platform=_linux()).run()

    changed_patches = {ID_RESOLVED: PATCH + "\n# changed\n"}
    changed_predictions = _make_predictions(tmp_path, changed_patches, name="changed.jsonl")
    stale_config = _make_config(
        tmp_path,
        changed_predictions,
        patches=changed_patches,
        run_id="run-2026-10-19",
        instance_ids=(ID_RESOLVED,),
    )
    second_fake = FakeSubprocessRunner()
    with pytest.raises(HarnessCacheError) as excinfo:
        OfficialHarnessRunner(stale_config, runner=second_fake, platform=_linux()).run()
    assert excinfo.value.kind is HarnessErrorKind.CACHE_IDENTITY_MISMATCH
    assert "patch_set_sha256" in excinfo.value.detail
    assert second_fake.calls == []


def test_different_patch_with_new_run_id_does_not_read_old_report(tmp_path: Path) -> None:
    old_patches = {ID_RESOLVED: PATCH}
    config, _patches, payload = _cache_scenario(tmp_path)
    first_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    OfficialHarnessRunner(config, runner=first_fake, platform=_linux()).run()

    new_patches = {ID_RESOLVED: PATCH + "\n# changed\n"}
    new_preds = _make_predictions(tmp_path, new_patches, name="changed.jsonl")
    new_config = _make_config(
        tmp_path,
        new_preds,
        patches=new_patches,
        instance_ids=(ID_RESOLVED,),
    )
    assert new_config.run_id != config.run_id
    new_payload = _report_payload(total=1, resolved=(ID_RESOLVED,))
    new_fake = FakeSubprocessRunner(on_run=_write_report_on_run(new_config, new_payload))
    second = OfficialHarnessRunner(new_config, runner=new_fake, platform=_linux()).run()
    assert second.decision is HarnessRunDecision.EXECUTED
    assert second.reused is False
    assert second.report is not None
    assert second.report.source_path is not None
    assert second.report.source_path.name == new_config.expected_report_name
    # 旧报告仍然独立存在，不被覆盖。
    assert (tmp_path / "official-eval" / config.expected_report_name).is_file()
    assert derive_run_id(_patch_hashes(new_patches), seed="s") != derive_run_id(
        _patch_hashes(old_patches), seed="s"
    )


def test_cache_report_without_identity_sidecar_is_rejected(tmp_path: Path) -> None:
    config, _patches, payload = _cache_scenario(tmp_path)
    fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()
    sidecar = tmp_path / "official-eval" / (config.expected_report_name + ".identity.json")
    sidecar.unlink()

    second_fake = FakeSubprocessRunner()
    with pytest.raises(HarnessCacheError) as excinfo:
        OfficialHarnessRunner(config.model_copy(), runner=second_fake, platform=_linux()).run()
    assert excinfo.value.kind is HarnessErrorKind.CACHE_UNVERIFIED
    assert second_fake.calls == []


def test_cache_corrupted_report_is_quarantined_and_rebuilt(tmp_path: Path) -> None:
    config, _patches, payload = _cache_scenario(tmp_path)
    first_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    OfficialHarnessRunner(config, runner=first_fake, platform=_linux()).run()

    report = tmp_path / "official-eval" / config.expected_report_name
    report.write_text("{broken", encoding="utf-8")
    rebuilt_payload = _report_payload(total=1, resolved=(ID_RESOLVED,))
    second_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, rebuilt_payload))
    second = OfficialHarnessRunner(config.model_copy(), runner=second_fake, platform=_linux()).run()

    assert second.decision is HarnessRunDecision.REBUILT
    assert second.reused is False
    assert len(second_fake.calls) == 1
    assert "损坏" in (second.reused_reason or "")
    quarantined = list(
        (tmp_path / "official-eval").glob(f"{config.expected_report_name}.corrupt-*")
    )
    assert len(quarantined) == 1
    assert second.verdict_for(ID_RESOLVED) is OfficialVerdict.RESOLVED


def test_cache_rejects_dataset_revision_change(tmp_path: Path) -> None:
    config, _patches, payload = _cache_scenario(tmp_path)
    first_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    OfficialHarnessRunner(config, runner=first_fake, platform=_linux()).run()

    other_revision = "a" * 40
    assert other_revision != config.dataset_revision
    changed_config = config.model_copy(update={"dataset_revision": other_revision})
    second_fake = FakeSubprocessRunner()
    with pytest.raises(HarnessCacheError) as excinfo:
        OfficialHarnessRunner(changed_config, runner=second_fake, platform=_linux()).run()
    assert excinfo.value.kind is HarnessErrorKind.CACHE_IDENTITY_MISMATCH
    assert "dataset_revision" in excinfo.value.detail
    assert second_fake.calls == []


def test_cache_rejects_old_cache_pollution_without_sidecar(tmp_path: Path) -> None:
    """报告目录里只有别人留下的旧报告、没有任何身份证据 → fail closed。"""
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    report_dir = tmp_path / "official-eval"
    _write_json(report_dir / config.expected_report_name, _load_report_fixture())
    fake = FakeSubprocessRunner()
    with pytest.raises(HarnessCacheError) as excinfo:
        OfficialHarnessRunner(config, runner=fake, platform=_linux()).run()
    assert excinfo.value.kind is HarnessErrorKind.CACHE_UNVERIFIED
    assert fake.calls == []


def test_cache_reuse_requires_same_image_identity(tmp_path: Path) -> None:
    """镜像身份不同（同补丁、同 run_id）也不允许复用。"""
    config, _patches, payload = _cache_scenario(tmp_path)
    first_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    OfficialHarnessRunner(
        config,
        runner=first_fake,
        platform=_linux(),
        image_ids={ID_RESOLVED: "sweb.eval.x86_64.astropy_1776_a"},
    ).run()

    second_fake = FakeSubprocessRunner()
    with pytest.raises(HarnessCacheError) as excinfo:
        OfficialHarnessRunner(
            config.model_copy(),
            runner=second_fake,
            platform=_linux(),
            image_ids={ID_RESOLVED: "sweb.eval.x86_64.astropy_1776_b"},
        ).run()
    assert excinfo.value.kind is HarnessErrorKind.CACHE_IDENTITY_MISMATCH
    assert "image_ids" in excinfo.value.detail
    assert second_fake.calls == []


def test_report_deleted_but_identity_sidecar_remains_reruns(tmp_path: Path) -> None:
    config, _patches, payload = _cache_scenario(tmp_path)
    first_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    OfficialHarnessRunner(config, runner=first_fake, platform=_linux()).run()
    report = tmp_path / "official-eval" / config.expected_report_name
    report.unlink()

    second_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    second = OfficialHarnessRunner(config.model_copy(), runner=second_fake, platform=_linux()).run()
    assert second.decision is HarnessRunDecision.EXECUTED
    assert second.reused is False
    assert len(second_fake.calls) == 1
    assert second.report is not None


def test_cached_official_result_can_be_read_on_windows_native(tmp_path: Path) -> None:
    """原生 Windows 跑不了 harness，但同身份缓存报告仍然可读（只读、不执行）。"""
    config, _patches, payload = _cache_scenario(tmp_path)
    first_fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    OfficialHarnessRunner(config, runner=first_fake, platform=_linux()).run()

    windows_fake = FakeSubprocessRunner()
    cached = OfficialHarnessRunner(
        config.model_copy(), runner=windows_fake, platform=_windows_native()
    ).run()
    assert windows_fake.calls == []
    assert cached.decision is HarnessRunDecision.REUSED
    assert cached.verdict_for(ID_RESOLVED) is OfficialVerdict.RESOLVED


# ---------------------------------------------------------------------------
# 7. 报告解析器（真实 schema_version=2 夹具 + 负控制）
# ---------------------------------------------------------------------------
def test_official_verdict_resolved_is_tristate() -> None:
    """只有官方 RESOLVED/UNRESOLVED 映射成 bool，其余官方形态保持 None。"""
    assert OfficialVerdict.RESOLVED.resolved_value is True
    assert OfficialVerdict.UNRESOLVED.resolved_value is False
    for verdict in (
        OfficialVerdict.PATCH_APPLY_FAILED,
        OfficialVerdict.EMPTY_PATCH,
        OfficialVerdict.ERROR,
        OfficialVerdict.MISSING,
        OfficialVerdict.NOT_EVALUATED,
    ):
        assert verdict.resolved_value is None, verdict
    assert OfficialVerdict.PATCH_APPLY_FAILED.is_evaluated is True
    assert OfficialVerdict.EMPTY_PATCH.is_evaluated is True
    assert OfficialVerdict.ERROR.is_evaluated is True
    assert OfficialVerdict.MISSING.is_evaluated is False
    assert OfficialVerdict.NOT_EVALUATED.is_evaluated is False


def test_parse_real_report_fixture() -> None:
    report = OfficialHarnessRunner.parse_report_file(FIXTURES / "official-report-4.1.0.json")
    assert isinstance(report, HarnessReport)
    assert report.schema_version == 2
    assert report.total_instances == 5
    assert report.submitted_instances == 4
    assert report.completed_instances == 3
    assert report.resolved_ids == (ID_RESOLVED,)
    assert report.unresolved_ids == (ID_UNRESOLVED,)
    assert report.empty_patch_ids == (ID_EMPTY,)
    assert report.error_ids == (ID_ERROR,)
    assert report.incomplete_ids == (ID_INCOMPLETE,)
    assert report.unstopped_instances == 0
    assert report.evaluation_coverage == pytest.approx(3 / 4)
    assert report.verdict_for(ID_RESOLVED) is OfficialVerdict.RESOLVED
    assert report.verdict_for(ID_EMPTY) is OfficialVerdict.EMPTY_PATCH
    assert report.verdict_for("astropy__astropy-00000") is OfficialVerdict.NOT_EVALUATED
    meta = _load_report_meta()
    assert report.source_sha256 == meta["provenance"]["report_sha256"]


def test_parse_report_without_optional_unstopped_fields(tmp_path: Path) -> None:
    """真实本地执行（client 非 None）时报告没有 unstopped_* 三键。"""
    payload = _load_report_fixture()
    for key in ("unstopped_instances", "unstopped_containers", "unremoved_images"):
        payload.pop(key)
    path = _write_json(tmp_path / "report.json", payload)
    report = OfficialHarnessRunner.parse_report_file(path)
    assert report.unstopped_instances is None
    assert report.unstopped_containers == ()
    assert report.unremoved_images == ()


def test_parse_report_allows_error_without_completed(tmp_path: Path) -> None:
    """官方 reporting.py 中"report.json 不存在"的实例只在 error_ids、不在 completed_ids。"""
    payload = _report_payload(
        total=2,
        resolved=(ID_RESOLVED,),
        submitted=(ID_RESOLVED, ID_UNRESOLVED),
    )
    payload["error_ids"] = [ID_UNRESOLVED]
    payload["error_instances"] = 1
    path = _write_json(tmp_path / "report.json", payload)
    report = OfficialHarnessRunner.parse_report_file(path)
    assert report.verdict_for(ID_UNRESOLVED) is OfficialVerdict.ERROR
    assert ID_UNRESOLVED not in report.completed_ids
    assert report.verdict_for(ID_RESOLVED) is OfficialVerdict.RESOLVED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"schema_version": 1}),
        lambda payload: payload.update({"resolved_instances": 99}),
        lambda payload: payload.update({"submitted_ids": []}),
        lambda payload: payload.update({"resolved_ids": [ID_UNRESOLVED]}),
        lambda payload: payload.pop("error_ids"),
        lambda payload: payload.update({"completed_instances": True}),
        lambda payload: payload.update({"resolved_ids": [ID_RESOLVED, ID_RESOLVED]}),
    ],
    ids=[
        "wrong-schema-version",
        "count-list-mismatch",
        "submitted-count-mismatch",
        "resolved-not-subset-completed",
        "missing-field",
        "bool-as-count",
        "duplicate-ids",
    ],
)
def test_parse_report_rejects_corrupted_payloads(tmp_path: Path, mutate: Any) -> None:
    payload = _load_report_fixture()
    mutate(payload)
    path = _write_json(tmp_path / "report.json", payload)
    with pytest.raises(HarnessError) as excinfo:
        OfficialHarnessRunner.parse_report_file(path)
    assert excinfo.value.kind is HarnessErrorKind.REPORT_CORRUPTED


def test_parse_report_missing_file_is_missing() -> None:
    with pytest.raises(HarnessError) as excinfo:
        OfficialHarnessRunner.parse_report_file(FIXTURES / "does-not-exist.json")
    assert excinfo.value.kind is HarnessErrorKind.REPORT_MISSING


# ---------------------------------------------------------------------------
# 8. 官方结论 vs 内部 M2 状态
# ---------------------------------------------------------------------------
def test_internal_m2_never_overrides_official_unresolved(tmp_path: Path) -> None:
    payload = _report_payload(total=1, unresolved=(ID_RESOLVED,))
    _config, _fake, _runner, result = _run_with_report(
        tmp_path,
        patches={ID_RESOLVED: PATCH},
        payload=payload,
        instance_ids=(ID_RESOLVED,),
        name="m2.jsonl",
    )
    verdict = result.verdicts[0]
    assert verdict.resolved is False
    assert (
        compare_with_internal_m2(verdict, m2_verified=True)
        is HarnessDiscrepancy.OFFICIAL_DENIED_INTERNAL_VERIFIED
    )
    # 关键负控制：比较函数不修改官方结论，官方 unresolved 仍然是 False。
    assert verdict.resolved is False
    assert result.report is not None and result.report.resolved_instances == 0


def test_internal_m2_discrepancy_classification(tmp_path: Path) -> None:
    resolved = _run_with_report(
        tmp_path,
        patches={ID_RESOLVED: PATCH},
        payload=_report_payload(total=1, resolved=(ID_RESOLVED,)),
        instance_ids=(ID_RESOLVED,),
        name="resolved.jsonl",
    )[3].verdicts[0]
    assert compare_with_internal_m2(resolved, m2_verified=False) is (
        HarnessDiscrepancy.OFFICIAL_RESOLVED_INTERNAL_NOT_VERIFIED
    )
    assert compare_with_internal_m2(resolved, m2_verified=None) is (
        HarnessDiscrepancy.INTERNAL_UNKNOWN
    )
    assert compare_with_internal_m2(resolved, m2_verified=True) is HarnessDiscrepancy.NONE

    not_evaluated = _run_with_report(
        tmp_path,
        patches={ID_RESOLVED: PATCH},
        payload=None,
        instance_ids=(ID_RESOLVED,),
        platform=_windows_native(),
        name="blocked.jsonl",
        report_dir=tmp_path / "blocked-eval",
    )[3].verdicts[0]
    assert not_evaluated.resolved is None
    assert compare_with_internal_m2(not_evaluated, m2_verified=True) is (
        HarnessDiscrepancy.OFFICIAL_NOT_EVALUATED
    )


# ---------------------------------------------------------------------------
# 9. ORACLE_CONTROL（gold 只在评测侧）
# ---------------------------------------------------------------------------
def test_oracle_control_never_enters_primary_predictions(tmp_path: Path) -> None:
    primary_patches = {ID_RESOLVED: PATCH, ID_UNRESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, primary_patches)
    primary_before = predictions.read_bytes()
    config = _make_config(
        tmp_path,
        predictions,
        patches=primary_patches,
        instance_ids=(ID_RESOLVED, ID_UNRESOLVED),
    )
    gold = {
        ID_RESOLVED: "diff --git a/gold.py b/gold.py\n--- a/gold.py\n+++ b/gold.py\n",
        ID_UNRESOLVED: "diff --git a/gold2.py b/gold2.py\n--- a/gold2.py\n+++ b/gold2.py\n",
    }
    oracle_path = tmp_path / "oracle-predictions.jsonl"
    oracle_run_id = oracle_control_run_id(gold)
    oracle_payload = _report_payload(total=2, resolved=(ID_RESOLVED, ID_UNRESOLVED))
    oracle_config = config.model_copy(
        update={
            "predictions_path": oracle_path,
            "run_id": oracle_run_id,
            "model_name_or_path": ORACLE_CONTROL_MODEL_NAME,
            "instance_ids": tuple(sorted(gold)),
        }
    )
    fake = FakeSubprocessRunner(on_run=_write_report_on_run(oracle_config, oracle_payload))
    result = run_oracle_control(
        config,
        gold,
        oracle_predictions_path=oracle_path,
        runner=fake,
        platform=_linux(),
    )

    assert fake.calls, "ORACLE_CONTROL 必须真的调用官方 harness 才会记录证据"
    assert result.control_kind is HarnessControlKind.ORACLE_CONTROL
    assert result.included_in_resolved_rate is False
    assert result.counts_toward_resolved_rate is False
    assert result.run_id.startswith("cp-oracle-")
    assert result.report is not None
    assert result.report.resolved_instances == 2
    assert result.report_path is not None
    assert result.report_path.name.startswith(ORACLE_CONTROL_MODEL_NAME + ".")

    # 主 predictions 原封不动，gold patch 没有混进去。
    assert predictions.read_bytes() == primary_before
    primary_text = predictions.read_text(encoding="utf-8")
    for gold_patch in gold.values():
        assert gold_patch not in primary_text
    assert gold_patch_marker(gold) not in primary_text

    # oracle 文件独立且模型名是保留的控制名。
    oracle_rows = [
        json.loads(line) for line in oracle_path.read_text(encoding="utf-8").splitlines()
    ]
    assert {row["model_name_or_path"] for row in oracle_rows} == {ORACLE_CONTROL_MODEL_NAME}
    assert oracle_path.resolve() != predictions.resolve()


def gold_patch_marker(gold: dict[str, str]) -> str:
    return next(iter(gold.values())).splitlines()[0]


def test_oracle_control_rejects_primary_predictions_path(tmp_path: Path) -> None:
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    with pytest.raises(HarnessInputError):
        run_oracle_control(
            config,
            {ID_RESOLVED: PATCH},
            oracle_predictions_path=predictions,
            runner=FakeSubprocessRunner(),
            platform=_linux(),
        )


def test_write_oracle_predictions_rejects_empty_patch(tmp_path: Path) -> None:
    with pytest.raises(HarnessInputError):
        write_oracle_predictions({ID_RESOLVED: ""}, tmp_path / "oracle.jsonl")
    with pytest.raises(HarnessInputError):
        write_oracle_predictions({}, tmp_path / "oracle.jsonl")


# ---------------------------------------------------------------------------
# 10. Artifact 保存与恢复
# ---------------------------------------------------------------------------
def test_artifact_save_and_restore(tmp_path: Path) -> None:
    payload = _report_payload(total=1, resolved=(ID_RESOLVED,))
    patches = {ID_RESOLVED: PATCH}
    predictions = _make_predictions(tmp_path, patches)
    config = _make_config(tmp_path, predictions, patches=patches, instance_ids=(ID_RESOLVED,))
    store = ArtifactStore(root=tmp_path / "artifacts", run_id=config.run_id)
    fake = FakeSubprocessRunner(on_run=_write_report_on_run(config, payload))
    runner = OfficialHarnessRunner(config, runner=fake, platform=_linux(), artifacts=store)
    result = runner.run()

    evidence_rel = "swebench_harness"
    for name in (
        "command.json",
        "identity.json",
        "stdout.log",
        "stderr.log",
        "report.json",
        "result.json",
    ):
        assert store.exists(f"{evidence_rel}/{name}"), name
    assert result.evidence_dir == store.path_for(f"{evidence_rel}/result.json").parent
    assert store.read_json(f"{evidence_rel}/command.json")["expected_report"].endswith(
        config.expected_report_name
    )

    restored = OfficialHarnessRunner.load_saved_result(store)
    assert restored is not None
    assert restored.model_dump() == result.model_dump()
    assert restored.verdict_for(ID_RESOLVED) is OfficialVerdict.RESOLVED

    other_store = ArtifactStore(root=tmp_path / "artifacts", run_id="another-run")
    assert OfficialHarnessRunner.load_saved_result(other_store) is None


def test_artifact_restore_rejects_corrupted_result(tmp_path: Path) -> None:
    store = ArtifactStore(root=tmp_path / "artifacts", run_id="run-1")
    store.write_text("swebench_harness/result.json", "{not-json")
    with pytest.raises(HarnessError) as excinfo:
        OfficialHarnessRunner.load_saved_result(store)
    assert excinfo.value.kind is HarnessErrorKind.INTERNAL_INCONSISTENCY


# ---------------------------------------------------------------------------
# 11. 缓存身份与补丁摘要（纯函数）
# ---------------------------------------------------------------------------
def test_identity_fingerprint_is_canonical_and_reports_mismatch() -> None:
    first = HarnessCacheIdentity(
        run_id="run-1",
        patch_hashes=((ID_RESOLVED, "a" * 64), (ID_UNRESOLVED, "b" * 64)),
        model_name_or_path=MODEL,
        dataset_name=DATASET_NAME,
        dataset_revision=DATASET_REVISION,
        split="test",
        harness_version=HARNESS_VERSION,
        image_ids=((ID_RESOLVED, "sweb.eval.x86_64.x"),),
    )
    reordered = HarnessCacheIdentity(
        run_id="run-1",
        patch_hashes=((ID_UNRESOLVED, "b" * 64), (ID_RESOLVED, "a" * 64)),
        model_name_or_path=MODEL,
        dataset_name=DATASET_NAME,
        dataset_revision=DATASET_REVISION,
        split="test",
        harness_version=HARNESS_VERSION,
        image_ids=((ID_RESOLVED, "sweb.eval.x86_64.x"),),
    )
    assert first.fingerprint == reordered.fingerprint
    assert first.mismatches(reordered) == ()
    assert first.patch_hash_for(ID_RESOLVED) == "a" * 64
    assert first.image_identity_for(ID_RESOLVED) == "sweb.eval.x86_64.x"

    changed = first.model_copy(update={"dataset_revision": "b" * 40, "run_id": "run-2"})
    reasons = first.mismatches(changed)
    assert any("dataset_revision" in reason for reason in reasons)
    assert any("run_id" in reason for reason in reasons)
    assert first.patch_set_sha256 == patch_set_digest(dict(first.patch_hashes))


def test_derive_run_id_depends_on_patch_set() -> None:
    first = derive_run_id({ID_RESOLVED: "a" * 64}, seed="20261019")
    second = derive_run_id({ID_RESOLVED: "c" * 64}, seed="20261019")
    assert first.startswith("cp-") and len(first.split("-")[1]) == 12
    assert first != second
    assert oracle_control_run_id({ID_RESOLVED: PATCH}).startswith("cp-oracle-")


# ---------------------------------------------------------------------------
# 12. 本地执行器（不真跑官方 harness，只验证命令/超时接口）
# ---------------------------------------------------------------------------
def test_local_runner_merges_env_and_reports_missing_executable(tmp_path: Path) -> None:
    runner = LocalSubprocessRunner()
    command = ["cmd", "/c", "echo hi"] if os.name == "nt" else ["echo", "hi"]
    outcome = runner.run(
        command,
        cwd=tmp_path,
        env={"PYTHONUTF8": "1"},
        timeout_seconds=10,
    )
    assert outcome.returncode == 0
    assert outcome.timed_out is False
    assert "hi" in outcome.stdout

    with pytest.raises(HarnessError) as excinfo:
        runner.run(
            [str(tmp_path / "definitely-not-a-real-executable")],
            cwd=tmp_path,
            env={},
            timeout_seconds=5,
        )
    assert excinfo.value.kind is HarnessErrorKind.PROCESS_FAILED


def test_default_harness_python_pointer() -> None:
    found = default_harness_python()
    if found is not None:
        assert found.is_file()
        assert ".venv-swebench" in str(found)
