from codepilot.context.tokens import TokenCounter
from codepilot.failure.compressor import compress_failures
from codepilot.failure.schemas import ParsedFailure
from codepilot.failure.signature import failure_signature, normalize
from codepilot.schemas.common import TraceFrame


def test_signature_stability_negative_controls():
    a = ParsedFailure(
        test_id="a", exception_type="ValueError", message="object 0xabc at /tmp/tmpABC/a: 123"
    )
    b = a.model_copy(update={"message": "object 0xdef at C:\\Temp\\tmpXYZ\\a: 987"})
    assert failure_signature(a) == failure_signature(b)
    assert failure_signature(a) != failure_signature(
        a.model_copy(update={"exception_type": "TypeError"})
    )
    assert normalize(normalize(a.message)) == normalize(a.message)
    assert normalize("missing 'user'") != normalize("missing 'session'")
    assert normalize("C:\\repo\\file.py") == normalize("/repo/file.py")


def test_dedup_budget_identity_and_purity():
    failures = [
        ParsedFailure(
            test_id=f"test_{n}",
            exception_type="ValueError",
            message="bad " + "汉字🙂" * 3000,
            artifact_ref="run/junit.xml",
        )
        for n in range(3)
    ]
    before = [f.model_dump_json() for f in failures]
    result = compress_failures(failures, target_tests={"test_2"})
    assert result.total_failures == 3
    assert result.groups[0].sample.test_id == "test_2"
    assert result.groups[0].other_test_ids == ["test_0", "test_1"]
    assert result.groups[0].truncated_characters > 0
    assert result.context.used <= result.context.budget
    assert before == [f.model_dump_json() for f in failures]
    assert result == compress_failures(failures, target_tests={"test_2"})
    tiny = compress_failures(failures, budget=1)
    assert tiny.context.used == 0
    assert tiny.all_test_ids == ["test_0", "test_1", "test_2"]
    assert len(tiny.context.omitted) == 1
    assert compress_failures([]).total_failures == 0


def test_priority_is_strict_and_independent_of_input_order():
    failures = [
        ParsedFailure(test_id=name, message=name, artifact_ref=f"run/{name}.xml")
        for name in ("other", "regression", "reproduction", "target")
    ]
    options = {
        "target_tests": {"target"},
        "reproduction_tests": {"reproduction"},
        "regression_tests": {"regression"},
    }
    result = compress_failures(failures, **options)
    assert [g.sample.test_id for g in result.groups] == [
        "target",
        "reproduction",
        "regression",
        "other",
    ]
    first = compress_failures([failures[-1]], **options)
    limited = compress_failures(failures, budget=first.context.used, **options)
    assert len(limited.context.included) == 1
    assert len(limited.context.omitted) == 3
    assert limited == compress_failures(
        list(reversed(failures)), budget=first.context.used, **options
    )
    assert limited.context.used == TokenCounter().charged(limited.context.text)
    assert limited.context.exact_count is False
    assert limited.context.count_note


def test_frames_artifacts_redaction_and_location_negative_control():
    frames = [
        TraceFrame(file=file, line=1, function="run", in_repo=inside, code="api_key=abcdefghijk")
        for file, inside in (
            ("a.py", True),
            ("/lib/one.py", False),
            ("b.py", True),
            ("/lib/two.py", False),
        )
    ]
    f = ParsedFailure(
        test_id="test_x",
        frames=frames,
        deepest_repo_frame=frames[2],
        message="password=abcdefghijk",
        artifact_ref="run/junit.xml",
    )
    group = compress_failures([f]).groups[0]
    assert [frame.file for frame in group.sample.frames] == ["a.py", "b.py", "/lib/two.py"]
    assert group.omitted_frames == 1 and group.artifact_refs == ["run/junit.xml"]
    assert "abcdefghijk" not in group.model_dump_json()
    different = frames[2].model_copy(update={"function": "other"})
    other = f.model_copy(update={"frames": [different], "deepest_repo_frame": different})
    assert failure_signature(f) != failure_signature(other)
    assert failure_signature(f) != failure_signature(ParsedFailure(test_id="x", message=f.message))


def test_unicode_many_failures_and_normalization_boundary():
    text = "路径🙂Пример café e\u0301 " * 3000
    failures = [
        ParsedFailure(
            test_id=f"test_{i}", message=f"{chr(65 + i)} {text}", artifact_ref="run/junit.xml"
        )
        for i in range(80)
    ]
    result = compress_failures(failures, budget=4000)
    assert result.context.used <= 4000 and result.context.omitted
    assert len(result.all_test_ids) == 80
    assert all(len(g.sample.message) <= 2000 for g in result.groups)
    assert normalize("count 123") == normalize(
        "count 456"
    )  # intentional frozen-algorithm collision
    assert normalize("missing user") != normalize("missing session")
    assert normalize("'" + "x" * 90 + "'") == normalize('"' + "y" * 90 + '"')
    for message in (text, "0xFFF 55 /tmp/tmpABC/a", "C:\\Temp\\rand123\\x", "x\n  y\t z"):
        assert normalize(normalize(message)) == normalize(message)
