"""Phase 18: per-instance image resolution must fail closed, never fall back."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codepilot.sandbox.junit import parse_junit_text
from codepilot.sandbox.status import classify_execution
from codepilot.sandbox.swebench_env import (
    DEFAULT_SPEC_SNAPSHOT,
    ImageInfo,
    InstanceImageOverride,
    SWEBenchEnvironmentAdapter,
    SWEBenchEnvironmentError,
    SWEBenchEnvironmentKind,
    SWEBenchInstanceImageResolver,
    instance_image_key,
)
from codepilot.schemas.enums import ExecutionStatus

FIXED_REF = "phase17/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072:phase17-numpy1264"
FIXED_ID = "sha256:" + "e" * 64
PARENT_ID = "sha256:" + "c" * 64
OTHER_ID = "sha256:" + "d" * 64


class StubBackend:
    def __init__(self) -> None:
        self.images: dict[str, ImageInfo] = {}
        self.pull_calls: list[str] = []

    def add(
        self, ref: str, image_id: str = OTHER_ID, *, tags: tuple[str, ...] | None = None
    ) -> None:
        self.images[ref] = ImageInfo(
            ref=ref,
            image_id=image_id,
            os="linux",
            architecture="amd64",
            repo_tags=tags or (ref,),
            labels={},
        )

    def daemon_ok(self) -> bool:
        return True

    def ostype(self) -> str:
        return "linux"

    def disk_free_gb(self, path: Path) -> float:
        return 120.0

    def inspect_image(self, ref: str) -> ImageInfo | None:
        return self.images.get(ref)

    def pull(self, ref: str) -> ImageInfo:
        self.pull_calls.append(ref)
        raise AssertionError(f"pull must not be attempted for {ref}")

    def list_images(self, *, labels: Mapping[str, str] | None = None) -> tuple[ImageInfo, ...]:
        return tuple(self.images.values())

    def build(self, **kwargs: Any) -> ImageInfo:
        raise AssertionError("build must not be attempted")

    def list_containers(self, *, labels: Mapping[str, str]) -> tuple[Any, ...]:
        return ()

    def remove_container(self, identifier: str) -> bool:
        return True


def _override(**changes: str) -> InstanceImageOverride:
    payload = {
        "instance_id": "pvlib__pvlib-python-1072",
        "image_ref": FIXED_REF,
        "image_id": FIXED_ID,
        "parent_image_ref": instance_image_key("pvlib__pvlib-python-1072"),
        "parent_image_id": PARENT_ID,
        "reason": "PyTables cp39 wheel needs the NumPy 1.x ABI",
    }
    payload.update(changes)
    return InstanceImageOverride.model_validate(payload)


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        instance_id="pvlib__pvlib-python-1072",
        repo="pvlib/pvlib-python",
        version="0.7",
        base_commit="04a523fafbd61bc2e49420963b84ed8e2bd1b3cf",
        environment_setup_commit="04a523fafbd61bc2e49420963b84ed8e2bd1b3cf",
        dataset=SimpleNamespace(revision="6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"),
    )


def _adapter(tmp_path: Path, backend: StubBackend, resolver: SWEBenchInstanceImageResolver) -> Any:
    return SWEBenchEnvironmentAdapter(
        docker=backend,
        spec_snapshot=DEFAULT_SPEC_SNAPSHOT,
        cache_dir=tmp_path,
        images=resolver,
    )


def test_resolver_defaults_to_official_image_and_scopes_override() -> None:
    resolver = SWEBenchInstanceImageResolver({"pvlib__pvlib-python-1072": _override()})

    fixed = resolver.resolve("pvlib__pvlib-python-1072")
    assert fixed.override is True
    assert fixed.image_ref == FIXED_REF
    assert fixed.image_id == FIXED_ID
    assert fixed.parent_image_ref == instance_image_key("pvlib__pvlib-python-1072")

    other = resolver.resolve("astropy__astropy-12907")
    assert other.override is False
    assert other.image_ref == instance_image_key("astropy__astropy-12907")
    assert other.image_id is None


def test_resolver_rejects_wrong_parent_and_incomplete_ids() -> None:
    with pytest.raises(SWEBenchEnvironmentError):
        SWEBenchInstanceImageResolver(
            {"pvlib__pvlib-python-1072": _override(parent_image_ref="swebench/other:latest")}
        )
    with pytest.raises(SWEBenchEnvironmentError):
        SWEBenchInstanceImageResolver(
            {"pvlib__pvlib-python-1072": _override(image_id="sha256:short")}
        )


def test_ensure_uses_override_and_verifies_full_image_id(tmp_path: Path) -> None:
    backend = StubBackend()
    backend.add(FIXED_REF, FIXED_ID)
    resolver = SWEBenchInstanceImageResolver({"pvlib__pvlib-python-1072": _override()})
    adapter = _adapter(tmp_path, backend, resolver)

    handle = adapter.ensure(adapter.identity(_task()))

    assert handle.image_key == FIXED_REF
    assert handle.image_id == FIXED_ID
    assert handle.image_override is True
    assert handle.expected_image_id == FIXED_ID
    assert backend.pull_calls == []


def test_ensure_fails_closed_when_override_missing_even_if_official_exists(
    tmp_path: Path,
) -> None:
    backend = StubBackend()
    backend.add(instance_image_key("pvlib__pvlib-python-1072"), PARENT_ID)
    resolver = SWEBenchInstanceImageResolver({"pvlib__pvlib-python-1072": _override()})
    adapter = _adapter(tmp_path, backend, resolver)

    with pytest.raises(SWEBenchEnvironmentError) as excinfo:
        adapter.ensure(adapter.identity(_task()))

    assert excinfo.value.kind == SWEBenchEnvironmentKind.ENV_ERROR
    assert "禁止" in str(excinfo.value)
    assert backend.pull_calls == []


def test_ensure_fails_closed_on_image_id_mismatch(tmp_path: Path) -> None:
    backend = StubBackend()
    backend.add(FIXED_REF, OTHER_ID)
    resolver = SWEBenchInstanceImageResolver({"pvlib__pvlib-python-1072": _override()})
    adapter = _adapter(tmp_path, backend, resolver)

    with pytest.raises(SWEBenchEnvironmentError) as excinfo:
        adapter.ensure(adapter.identity(_task()))

    assert "身份不匹配" in str(excinfo.value)
    assert backend.pull_calls == []


def test_preflight_reports_override_identity_mismatch(tmp_path: Path) -> None:
    backend = StubBackend()
    backend.add(FIXED_REF, OTHER_ID)
    resolver = SWEBenchInstanceImageResolver({"pvlib__pvlib-python-1072": _override()})
    adapter = _adapter(tmp_path, backend, resolver)

    report = adapter.preflight(required_instance="pvlib__pvlib-python-1072")

    assert report.image_override is True
    assert report.image_expected_id == FIXED_ID
    assert report.image_present is False
    assert any("身份不匹配" in problem for problem in report.problems)


def test_identity_records_execution_image_and_hash_tracks_override_id(
    tmp_path: Path,
) -> None:
    first = _adapter(
        tmp_path / "first",
        StubBackend(),
        SWEBenchInstanceImageResolver({"pvlib__pvlib-python-1072": _override()}),
    ).identity(_task())
    second = _adapter(
        tmp_path / "second",
        StubBackend(),
        SWEBenchInstanceImageResolver({"pvlib__pvlib-python-1072": _override(image_id=OTHER_ID)}),
    ).identity(_task())

    assert first.execution_image_key == FIXED_REF
    assert first.execution_image_id == FIXED_ID
    assert first.instance_image_key == instance_image_key("pvlib__pvlib-python-1072")
    assert first.identity_schema_version == 2
    assert first.environment_hash != second.environment_hash


def test_ensure_rejects_identity_bound_to_another_override_image(tmp_path: Path) -> None:
    identity = _adapter(
        tmp_path / "a",
        StubBackend(),
        SWEBenchInstanceImageResolver({"pvlib__pvlib-python-1072": _override()}),
    ).identity(_task())
    backend = StubBackend()
    backend.add(FIXED_REF, OTHER_ID)
    other_adapter = _adapter(
        tmp_path / "b",
        backend,
        SWEBenchInstanceImageResolver({"pvlib__pvlib-python-1072": _override(image_id=OTHER_ID)}),
    )

    with pytest.raises(SWEBenchEnvironmentError) as excinfo:
        other_adapter.ensure(identity)

    assert "execution_image_id" in str(excinfo.value)
    assert backend.pull_calls == []


def test_ensure_rejects_legacy_identity_schema_without_reusing_cache(tmp_path: Path) -> None:
    backend = StubBackend()
    backend.add(instance_image_key("pvlib__pvlib-python-1072"), PARENT_ID)
    adapter = _adapter(tmp_path, backend, SWEBenchInstanceImageResolver())
    identity = adapter.identity(_task()).model_copy(update={"identity_schema_version": 1})

    with pytest.raises(SWEBenchEnvironmentError) as excinfo:
        adapter.ensure(identity)

    assert "schema 版本" in str(excinfo.value)
    assert backend.pull_calls == []


def test_identity_without_override_keeps_official_image_behavior(tmp_path: Path) -> None:
    backend = StubBackend()
    official = instance_image_key("pvlib__pvlib-python-1072")
    backend.add(official, PARENT_ID)
    adapter = _adapter(tmp_path, backend, SWEBenchInstanceImageResolver())

    identity = adapter.identity(_task())
    handle = adapter.ensure(identity)

    assert identity.execution_image_key == official
    assert identity.execution_image_id is None
    assert identity.execution_image_override is False
    assert handle.image_key == official
    assert handle.image_id == PARENT_ID
    assert handle.image_override is False


def test_collection_error_is_not_an_assertion_failure() -> None:
    junit = parse_junit_text(
        '<testsuite tests="1" failures="1" errors="0">'
        '<testcase classname="t" name="fails"><failure message="boom"/></testcase>'
        "</testsuite>"
    )

    collection = classify_execution(exit_code=4, junit=None, diagnostic_text="ERROR collecting x")
    assertion = classify_execution(exit_code=1, junit=junit)
    environment = classify_execution(
        exit_code=1,
        junit=junit,
        diagnostic_text="ModuleNotFoundError: No module named 'tables'",
        workspace=Path("/testbed"),
    )

    assert collection.status is ExecutionStatus.COLLECTION_ERROR
    assert collection.error_reason == "PYTEST_COLLECTION_ERROR"
    assert assertion.status is ExecutionStatus.FAIL
    assert assertion.error_reason is None
    assert environment.status is ExecutionStatus.ENV_ERROR
