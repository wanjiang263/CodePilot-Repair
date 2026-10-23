"""P1: ``--image-overrides`` must reach SmokeConfig and one resolver per run."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import swebench_smoke as smoke  # noqa: E402

from codepilot.sandbox.swebench_env import (  # noqa: E402
    SWEBenchEnvironmentError,
    instance_image_key,
)

INSTANCE = "pvlib__pvlib-python-1072"
FIXED_REF = "phase17/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072:phase17-numpy1264"
FIXED_ID = "sha256:" + "e" * 64
PARENT_ID = "sha256:" + "c" * 64


def _override_file(tmp_path: Path, *, schema_version: int = 1, image_id: str = FIXED_ID) -> Path:
    payload = {
        "schema_version": schema_version,
        "overrides": [
            {
                "instance_id": INSTANCE,
                "image_ref": FIXED_REF,
                "image_id": image_id,
                "parent_image_ref": instance_image_key(INSTANCE),
                "parent_image_id": PARENT_ID,
                "reason": "pinned NumPy ABI for the instance image",
            }
        ],
    }
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _config(tmp_path: Path, overrides: Path, *, dry_run: bool = True) -> Any:
    return smoke.SmokeConfig(
        instance_id=INSTANCE,
        artifact_root=tmp_path / "artifacts",
        image_overrides=overrides,
        dry_run=dry_run,
    )


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        instance_id=INSTANCE,
        repo="pvlib/pvlib-python",
        version="0.7",
        base_commit="04a523fafbd61bc2e49420963b84ed8e2bd1b3cf",
        environment_setup_commit="04a523fafbd61bc2e49420963b84ed8e2bd1b3cf",
        dataset=SimpleNamespace(revision=smoke.DATASET_REVISION),
    )


def test_cli_image_overrides_reaches_config_and_config_payload(tmp_path: Path) -> None:
    overrides = _override_file(tmp_path)
    args = smoke._parse_args(
        ["--instance-id", INSTANCE, "--image-overrides", str(overrides), "--dry-run"]
    )

    config = smoke._config_from_args(args)

    assert config.image_overrides == overrides
    assert (
        config.config_payload()["image_overrides_sha256"]
        == hashlib.sha256(overrides.read_bytes()).hexdigest()
    )


def test_config_rejects_missing_override_file(tmp_path: Path) -> None:
    with pytest.raises(smoke.SmokeError):
        _config(tmp_path, tmp_path / "missing.json")


def test_override_schema_error_fails_closed_before_any_run(tmp_path: Path) -> None:
    config = _config(tmp_path, _override_file(tmp_path, schema_version=99))

    with pytest.raises(SWEBenchEnvironmentError):
        smoke._image_resolver(config)


def test_dry_run_adapter_and_sandbox_share_one_resolved_override(tmp_path: Path) -> None:
    config = _config(tmp_path, _override_file(tmp_path))
    images = smoke._image_resolver(config)

    adapter = smoke._default_environment_adapter(config, images=images)
    identity = adapter.identity(_task())
    sandbox = smoke._default_sandbox_factory(config, images=images, client=SimpleNamespace())(
        object()
    )

    assert identity.execution_image_key == FIXED_REF
    assert identity.execution_image_id == FIXED_ID
    assert sandbox.images is images
    assert sandbox.images.resolve(INSTANCE).image_ref == identity.execution_image_key
    assert sandbox.images.resolve(INSTANCE).image_id == identity.execution_image_id


def test_environment_digest_changes_when_override_image_id_changes(tmp_path: Path) -> None:
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _config(first_dir, _override_file(first_dir))
    second = _config(second_dir, _override_file(second_dir, image_id="sha256:" + "d" * 64))

    def digest(config: Any) -> str:
        adapter = smoke._default_environment_adapter(config, images=smoke._image_resolver(config))
        payload = smoke._jsonable(adapter.identity(_task()))
        return smoke._environment_identity_digest(payload) or ""

    assert digest(first) != digest(second)


def test_execution_image_consistency_guard_rejects_sandbox_mismatch() -> None:
    environment = {
        "execution_image_key": FIXED_REF,
        "execution_image_id": FIXED_ID,
    }
    matching = SimpleNamespace(last_image={"image_ref": FIXED_REF, "image_id": FIXED_ID})
    smoke._assert_execution_image_matches(environment, [matching])

    mismatched = SimpleNamespace(
        last_image={"image_ref": FIXED_REF, "image_id": "sha256:" + "d" * 64}
    )
    with pytest.raises(smoke.SmokeError):
        smoke._assert_execution_image_matches(environment, [mismatched])
