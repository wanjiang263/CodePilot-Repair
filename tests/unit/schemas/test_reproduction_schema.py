"""D-01：``ReproductionResult`` 契约的强类型与严格校验。

docs/05 给 D-01 的验收是"round-trip 测试"，但真正的价值在拒绝侧：尝试次数是
复现证据的分母，路径是最终 patch 的剔除依据。这两个字段一旦能被随手写错，
下游所有统计就都不可信了。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from codepilot.schemas.reproduction import (
    ENVIRONMENT_FAILURE_KINDS,
    MAX_REPRO_ATTEMPTS,
    MAX_REPRO_RETRIES,
    REPRO_TEST_DIRECTORY,
    ReproductionAttempt,
    ReproductionFailureKind,
    ReproductionResult,
    ReproductionStatus,
    is_reproduction_test_path,
    reproduction_node_id,
    validate_reproduction_test_path,
)

REPRO = f"{REPRO_TEST_DIRECTORY}/test_repro.py"


def _attempt(
    index: int,
    kind: ReproductionFailureKind = ReproductionFailureKind.INVALID_PASS,
    **extra: object,
) -> ReproductionAttempt:
    payload: dict[str, object] = {
        "index": index,
        "kind": kind,
        "detail": f"第 {index} 轮的真实证据",
    }
    payload.update(extra)
    return ReproductionAttempt.model_validate(payload)


def test_result_round_trips_through_json_exactly() -> None:
    result = ReproductionResult(
        status=ReproductionStatus.REPRODUCED,
        test_path=REPRO,
        fail_reason="AssertionError: 边界秒仍被判定为过期",
        attempts=2,
        kind=ReproductionFailureKind.VALID_ASSERTION,
        history=[
            _attempt(1, ReproductionFailureKind.INVALID_PASS),
            _attempt(
                2,
                ReproductionFailureKind.VALID_ASSERTION,
                test_path=REPRO,
                node_id=f"{REPRO}::test_expiry_boundary",
                failure_type="AssertionError",
                exit_code=1,
            ),
        ],
    )
    restored = ReproductionResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.attempts == 2 and restored.retries == 1
    assert restored.degraded is False
    assert [item.index for item in restored.history] == [1, 2]


def test_contract_fields_are_the_documented_four() -> None:
    fields = set(ReproductionResult.model_fields)
    assert {"status", "test_path", "fail_reason", "attempts"} <= fields
    # 追加的两个字段都带默认值，所以既有 RepairState.reproduction 读写不受影响。
    assert fields <= {"status", "test_path", "fail_reason", "attempts", "kind", "history"}
    assert ReproductionResult.model_config["extra"] == "forbid"


def test_unknown_status_and_extra_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ReproductionResult(status="reproduced_but_sure", test_path=REPRO)
    with pytest.raises(ValidationError):
        ReproductionResult.model_validate(
            {"status": "skipped", "gold_patch": "retrieval_gold/01.patch"}
        )


@pytest.mark.parametrize(
    "path",
    [
        "tests/_codepilot_repro/nested/test_repro.py",
        "/abs/tests/_codepilot_repro/test_repro.py",
        "C:\\tests\\_codepilot_repro\\test_repro.py",
        "../tests/_codepilot_repro/test_repro.py",
        "tests/_codepilot_repro/../test_repro.py",
        "tests/_CODEPILOT_REPRO/test_repro.py",
        "tests/_codepilot_repro_extra/test_repro.py",
        "TESTS/_codepilot_repro/test_repro.py",
        "tests/_codepilot_repro/conftest.py",
        "tests/_codepilot_repro/helper.py",
        "tests/test_repro.py",
        "src/_codepilot_repro/test_repro.py",
        "",
        "   ",
    ],
)
def test_invalid_reproduction_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        validate_reproduction_test_path(path)
    assert is_reproduction_test_path(path) is False
    with pytest.raises(ValidationError):
        ReproductionResult(
            status=ReproductionStatus.REPRODUCED,
            test_path=path,
            fail_reason="AssertionError",
            attempts=1,
            kind=ReproductionFailureKind.VALID_ASSERTION,
            history=[_attempt(1, ReproductionFailureKind.VALID_ASSERTION, test_path=path)],
        )


def test_valid_path_is_returned_unchanged() -> None:
    assert validate_reproduction_test_path(REPRO) == REPRO
    assert is_reproduction_test_path(REPRO) is True
    assert reproduction_node_id(REPRO, "test_boundary") == f"{REPRO}::test_boundary"


def test_node_id_rejects_a_malformed_test_name() -> None:
    with pytest.raises(ValueError):
        reproduction_node_id(REPRO, "not a name")


def test_attempts_must_match_the_history_it_claims_to_summarise() -> None:
    with pytest.raises(ValidationError, match="历史条目数"):
        ReproductionResult(
            status=ReproductionStatus.FAILED,
            fail_reason="三轮都没有复现",
            attempts=3,
            kind=ReproductionFailureKind.INVALID_PASS,
            history=[_attempt(1), _attempt(2)],
        )
    with pytest.raises(ValidationError, match="连续记录"):
        ReproductionResult(
            status=ReproductionStatus.FAILED,
            fail_reason="三轮都没有复现",
            attempts=2,
            kind=ReproductionFailureKind.INVALID_PASS,
            history=[_attempt(1), _attempt(3)],
        )


def test_attempts_cannot_be_fabricated_beyond_the_contract_ceiling() -> None:
    with pytest.raises(ValidationError):
        ReproductionResult(
            status=ReproductionStatus.FAILED,
            fail_reason="太多",
            attempts=MAX_REPRO_ATTEMPTS + 1,
        )
    for bad in ("2", 2.0, True, -1):
        with pytest.raises(ValidationError):
            ReproductionResult(status=ReproductionStatus.FAILED, attempts=bad)  # type: ignore[arg-type]


def test_retried_attempts_is_one_initial_try_plus_retries() -> None:
    assert MAX_REPRO_ATTEMPTS == MAX_REPRO_RETRIES + 1 == 4
    result = ReproductionResult(
        status=ReproductionStatus.FAILED,
        fail_reason="重试耗尽",
        attempts=MAX_REPRO_ATTEMPTS,
        kind=ReproductionFailureKind.INVALID_UNDECLARED_EXCEPTION,
        history=[_attempt(index) for index in range(1, MAX_REPRO_ATTEMPTS + 1)],
    )
    assert result.retries == MAX_REPRO_RETRIES


def test_reproduced_requires_failure_evidence_and_a_valid_kind() -> None:
    with pytest.raises(ValidationError, match="失败证据"):
        ReproductionResult(
            status=ReproductionStatus.REPRODUCED,
            test_path=REPRO,
            attempts=1,
            history=[_attempt(1, ReproductionFailureKind.VALID_ASSERTION, test_path=REPRO)],
        )
    with pytest.raises(ValidationError, match="有效复现类型"):
        ReproductionResult(
            status=ReproductionStatus.REPRODUCED,
            test_path=REPRO,
            fail_reason="AssertionError",
            attempts=1,
            kind=ReproductionFailureKind.INVALID_PASS,
            history=[_attempt(1)],
        )


def test_failed_and_skipped_cannot_claim_a_reproduction_file() -> None:
    with pytest.raises(ValidationError, match="不能声称拥有复现测试文件"):
        ReproductionResult(
            status=ReproductionStatus.FAILED,
            test_path=REPRO,
            fail_reason="没有复现",
            attempts=1,
            kind=ReproductionFailureKind.INVALID_PASS,
            history=[_attempt(1)],
        )
    with pytest.raises(ValidationError, match="没有尝试"):
        ReproductionResult(
            status=ReproductionStatus.SKIPPED,
            fail_reason="Issue 没有可验证行为",
            attempts=1,
            kind=ReproductionFailureKind.INVALID_PASS,
            history=[_attempt(1)],
        )
    skipped = ReproductionResult(
        status=ReproductionStatus.SKIPPED, fail_reason="Issue 没有可验证行为"
    )
    assert skipped.attempts == 0 and skipped.degraded is True


def test_failed_requires_real_evidence_and_a_category() -> None:
    with pytest.raises(ValidationError, match="实际失败证据"):
        ReproductionResult(
            status=ReproductionStatus.FAILED,
            attempts=1,
            history=[_attempt(1)],
        )
    with pytest.raises(ValidationError, match="失败结局类别"):
        ReproductionResult(
            status=ReproductionStatus.FAILED,
            fail_reason="没有复现",
            attempts=1,
            history=[_attempt(1)],
        )


@pytest.mark.parametrize("kind", sorted(ENVIRONMENT_FAILURE_KINDS, key=lambda item: item.value))
def test_environment_outcomes_cannot_be_disguised_as_reproduction_failures(
    kind: ReproductionFailureKind,
) -> None:
    env = ReproductionResult(
        status=ReproductionStatus.ENV_ERROR,
        fail_reason="环境镜像构建失败",
        attempts=1,
        kind=kind,
        history=[_attempt(1, kind)],
    )
    assert env.degraded is True
    with pytest.raises(ValidationError, match="伪装成复现失败"):
        ReproductionResult(
            status=ReproductionStatus.FAILED,
            fail_reason="环境镜像构建失败",
            attempts=1,
            kind=kind,
            history=[_attempt(1, kind)],
        )
    with pytest.raises(ValidationError, match="环境类结局"):
        ReproductionResult(
            status=ReproductionStatus.ENV_ERROR,
            fail_reason="测试通过了",
            attempts=1,
            kind=ReproductionFailureKind.INVALID_PASS,
            history=[_attempt(1)],
        )


def test_status_and_kind_enums_are_json_serialisable_strings() -> None:
    result = ReproductionResult(
        status=ReproductionStatus.FAILED,
        fail_reason="没有复现",
        attempts=1,
        kind=ReproductionFailureKind.INVALID_SYNTAX_ERROR,
        history=[_attempt(1, ReproductionFailureKind.INVALID_SYNTAX_ERROR)],
    )
    payload = result.model_dump(mode="json")
    assert payload["status"] == "failed"
    assert payload["kind"] == "invalid_syntax_error"
    assert payload["history"][0]["kind"] == "invalid_syntax_error"


def test_detail_is_a_required_bounded_field() -> None:
    with pytest.raises(ValidationError):
        ReproductionAttempt(index=1, kind=ReproductionFailureKind.INVALID_PASS, detail="")
    with pytest.raises(ValidationError):
        ReproductionAttempt(index=1, kind=ReproductionFailureKind.INVALID_PASS, detail="x" * 2001)
    with pytest.raises(ValidationError):
        ReproductionAttempt(
            index=0, kind=ReproductionFailureKind.INVALID_PASS, detail="index 从 1 开始"
        )


def test_all_failure_kinds_are_distinct_machine_readable_values() -> None:
    values = [kind.value for kind in ReproductionFailureKind]
    assert len(values) == len(set(values))
    assert "valid_assertion" in values and "invalid_import_error" in values
