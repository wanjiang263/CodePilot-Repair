from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from codepilot.sandbox.env_spec import (
    DEFAULT_TEST_COMMAND,
    EnvSpec,
    EnvSpecError,
    EnvSpecInferenceError,
    from_swebench_spec,
    infer_user_env_spec,
    load_env_spec,
    resolve_env_spec,
)


def test_env_spec_json_round_trip_and_canonical_form() -> None:
    spec = EnvSpec(install=("python -m pip install -e .",), env={"Z": "2", "A": "1"})
    assert EnvSpec.model_validate_json(spec.model_dump_json()) == spec
    assert (
        spec.canonical_json()
        == EnvSpec(
            install=("python -m pip install -e .",), env={"A": "1", "Z": "2"}
        ).canonical_json()
    )
    assert "--junitxml=/out/junit.xml" in spec.test_command
    assert json.loads(spec.canonical_json())["install"] == ["python -m pip install -e ."]


@pytest.mark.parametrize(
    "payload",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": 3601},
        {"timeout_seconds": True},
        {"timeout_seconds": "300"},
        {"timeout_seconds": 12.5},
        {"python_version": "3.9"},
        {"install": "pip install ."},
        {"install": [""]},
        {"test_command": "pytest"},
        {"env": {"API_KEY": "secret"}},
        {"unsupported": True},
    ],
)
def test_env_spec_rejects_invalid_or_unsupported_content(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EnvSpec.model_validate(payload)


def test_synthetic_env_yaml_source(tmp_path: Path) -> None:
    path = tmp_path / "env.yaml"
    path.write_text(
        "python_version: '3.12'\ninstall:\n  - python -m pip install -e .\n",
        encoding="utf-8",
    )
    assert load_env_spec(path).install == ("python -m pip install -e .",)
    assert resolve_env_spec(tmp_path) == load_env_spec(path)


def test_invalid_yaml_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "env.yaml"
    path.write_text("install: [", encoding="utf-8")
    with pytest.raises(EnvSpecError, match="无法读取"):
        load_env_spec(path)


def test_swebench_official_shape_is_adapted_without_dataset_download() -> None:
    spec = from_swebench_spec(
        {
            "python": "3.11",
            "packages": "requirements.txt",
            "install": "python -m pip install -e .",
            "pre_install": ["apt-get update"],
            "test_cmd": "python -m pytest -q",
        }
    )
    assert spec.python_version == "3.11"
    assert spec.install == (
        "python -m pip install -r requirements.txt",
        "python -m pip install -e .",
    )
    assert spec.test_command.endswith("--junitxml=/out/junit.xml")


def test_swebench_conda_shape_is_explicitly_unsupported() -> None:
    with pytest.raises(EnvSpecError, match="conda"):
        from_swebench_spec({"packages": "environment.yml"})


@pytest.mark.parametrize(
    "payload",
    [
        {"pre_install": {"command": "echo unsafe"}},
        {"install": ["pip install .", 1]},
        {"packages": ["pytest", 1]},
        {"test_cmd": None},
        {"test_cmd": ""},
        {"timeout_seconds": True},
        {"timeout_seconds": 1.9},
        {"timeout_seconds": "600"},
    ],
)
def test_swebench_malformed_commands_fail_loudly(payload: dict[str, object]) -> None:
    with pytest.raises(EnvSpecError):
        from_swebench_spec(payload)


def test_user_repo_inference_prefers_declared_test_extra(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1'\n[project.optional-dependencies]\ntest=['pytest']\n",
        encoding="utf-8",
    )
    spec = infer_user_env_spec(tmp_path)
    assert spec.install == ("python -m pip install -e .[test]",)
    assert spec.test_command == DEFAULT_TEST_COMMAND


def test_user_repo_inference_supports_requirements_and_setup(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    assert infer_user_env_spec(tmp_path).install == (
        "python -m pip install -r requirements.txt",
        "python -m pip install -e .",
    )


def test_user_repo_inference_never_silently_guesses(tmp_path: Path) -> None:
    with pytest.raises(EnvSpecInferenceError, match=r"env\.yaml"):
        infer_user_env_spec(tmp_path)


def test_user_repo_inference_rejects_malformed_project_table(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("project = 'invalid'\n", encoding="utf-8")
    with pytest.raises(EnvSpecInferenceError, match="project"):
        infer_user_env_spec(tmp_path)
