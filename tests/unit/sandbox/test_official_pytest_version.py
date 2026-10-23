"""The pytest workspace mount hides setuptools_scm's generated version file."""

from __future__ import annotations

from codepilot.sandbox.official import _pytest_command


def test_pytest_command_bootstraps_generated_version_file() -> None:
    command = _pytest_command(
        "pytest-dev__pytest-11148", "/opt/miniconda3/envs/testbed/bin/python -m pytest -q"
    )

    assert command.endswith("/opt/miniconda3/envs/testbed/bin/python -m pytest -q")
    assert "if [ ! -f /testbed/src/_pytest/_version.py ]" in command
    assert "python setup.py --version" in command
    assert "__version_tuple__ = version_tuple = (8, 0, 0)" in command


def test_non_pytest_command_is_unchanged() -> None:
    command = "python -m pytest -q tests/test_core.py"

    assert _pytest_command("scikit-learn__scikit-learn-12471", command) == command
