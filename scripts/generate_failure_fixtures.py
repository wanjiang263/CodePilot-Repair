"""Explicit controlled pytest corpus generator. Executes only source literals below."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

CASES = {
    "syntax": "def test_broken(:\n    pass\n",
    "import": "def test_import():\n    from pkg import absent\n",
    "attribute": "def test_attribute():\n    object().absent\n",
    "type": "def test_type():\n    len(3)\n",
    "key": "def test_key():\n    {}['missing']\n",
    "assertion": "def test_assertion():\n    assert 1 == 2\n",
    "timeout": "import time\n\ndef test_timeout():\n    time.sleep(5)\n",
    "collection": "raise RuntimeError('controlled collection failure')\n",
    "fixture_setup": "import pytest\n\n@pytest.fixture\ndef resource():\n    raise ValueError('controlled setup failure')\n\ndef test_resource(resource):\n    pass\n",
    "fixture_teardown": "import pytest\n\n@pytest.fixture\ndef resource():\n    yield 1\n    raise ValueError('controlled teardown failure')\n\ndef test_resource(resource):\n    assert resource == 1\n",
    "did_not_raise": "import pytest\n\ndef test_raises():\n    with pytest.raises(ValueError):\n        pass\n",
    "dependency": "def test_dependency():\n    import codepilot_absent_dependency_7f98\n",
    "multiple": "import pytest\n\n@pytest.mark.parametrize('value', [1, 2])\ndef test_many(value):\n    assert value == 0\n",
    "chain": "def test_chain():\n    try:\n        raise KeyError('origin')\n    except KeyError as exc:\n        raise ValueError('outer') from exc\n",
    "volatile_a": "def test_volatile():\n    raise ValueError('object 0xabcdef at /tmp/tmpABC/item: 123')\n",
    "volatile_b": "def test_volatile():\n    raise ValueError('object 0xfedcba at /tmp/tmpXYZ/item: 456')\n",
    "unknown": "def test_unknown():\n    raise RuntimeError('opaque controlled failure')\n",
}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / "tests/fixtures/failures"
    provenance = []
    for name, code in CASES.items():
        source = destination / "sources" / name
        source.mkdir(parents=True, exist_ok=True)
        (source / "test_case.py").write_text(code, encoding="utf-8")
        (source / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        if name == "import":
            (source / "pkg.py").write_text("existing = 1\n", encoding="utf-8")
        raw = root / "artifacts/phase7-fixtures" / f"{name}.xml"
        raw.parent.mkdir(parents=True, exist_ok=True)
        args = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "pytest_timeout",
            "-p",
            "no:cacheprovider",
            "-c",
            "pytest.ini",
            "--tb=long",
            "--junitxml",
            str(raw),
            "test_case.py",
        ]
        if name == "timeout":
            # pytest-timeout's thread mode exits without XML on Windows: record that honestly.
            args.extend(["--timeout=1"])
        completed = subprocess.run(
            args,
            cwd=source,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode not in (1, 2):
            raise RuntimeError(f"unexpected controlled exit: {name} {completed.returncode}")
        if raw.exists():
            value = raw.read_text(encoding="utf-8")
            for original in (str(source), source.as_posix()):
                value = value.replace(original, "/fixture")
            for prefix in (
                str(root),
                root.as_posix(),
                sys.base_prefix,
                sys.base_prefix.replace("\\", "/"),
            ):
                value = value.replace(prefix, "[runtime]")
            xml = ElementTree.fromstring(value)
            for element in xml.iter():
                for key in ("time", "timestamp", "hostname"):
                    element.attrib.pop(key, None)
            (destination / f"{name}.xml").write_text(
                ElementTree.tostring(xml, encoding="unicode"), encoding="utf-8"
            )
        else:
            if name != "timeout" or "Timeout" not in completed.stdout:
                raise RuntimeError(f"missing junit: {name}")
            # Real subprocess evidence, deliberately not labelled DockerSandbox output.
            from codepilot.schemas.enums import ExecutionStatus
            from codepilot.schemas.execution import ExecutionResult

            result = ExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                exit_code=completed.returncode,
                error_reason="PYTEST_TIMEOUT_THREAD_EXIT",
                artifact_dir="tests/fixtures/failures/timeout.execution.json",
                diagnostics={
                    "execution_backend": "controlled_local_pytest",
                    "junit_written": False,
                    "timeout_seconds": 1,
                },
            )
            (destination / "timeout.execution.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
        provenance.append(
            {
                "sample": name,
                "source": f"sources/{name}/test_case.py",
                "command": [
                    "python",
                    *args[1 : args.index("--junitxml")],
                    "--junitxml",
                    "<artifact>",
                    "test_case.py",
                    *(["--timeout=1"] if name == "timeout" else []),
                ],
                "exit_code": completed.returncode,
                "backend": "controlled_local_pytest",
                "pytest_version": pytest.__version__,
            }
        )
    (destination / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
