"""Container entry point: existing Phase 1 contracts plus a real mini_auth CLI run."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from importlib.metadata import version
from pathlib import Path


def run(*args: str, env: dict[str, str] | None = None) -> str:
    print("COMMAND", json.dumps(args), flush=True)
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
        check=False,
    )
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", flush=True)
    print("EXIT", result.returncode, flush=True)
    if result.returncode:
        raise RuntimeError(f"Command exited {result.returncode}: {args!r}")
    return result.stdout


def main() -> None:
    packages = {
        name: version(name)
        for name in ("tree-sitter", "tree-sitter-python", "pytest", "codepilot-repair")
    }
    print("ENVIRONMENT", json.dumps({"python": sys.version, **packages}), flush=True)
    assert packages["tree-sitter"] == "0.25.2"
    assert packages["tree-sitter-python"] == "0.25.0"
    run("git", "--version")
    run("uv", "--version")

    with tempfile.TemporaryDirectory(prefix="codepilot-phase1-") as temporary:
        root = Path(temporary)
        junit = root / "contracts.xml"
        run(
            sys.executable,
            "-X",
            "faulthandler",
            "-m",
            "pytest",
            "-v",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(root / "pytest"),
            "--junitxml",
            str(junit),
            "tests/unit/repo/test_parser_subprocess.py",
            "tests/integration/test_index_golden.py",
            "tests/integration/test_cli_index.py",
            "tests/integration/test_incremental.py",
        )
        suites = ET.parse(junit).getroot().findall("testsuite")
        counts = {
            key: sum(int(suite.attrib.get(key, 0)) for suite in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
        assert counts == {"tests": 11, "failures": 0, "errors": 0, "skipped": 0}, counts
        print("CONTRACTS", json.dumps(counts), flush=True)

        fixture = root / "mini_auth"
        shutil.copytree("tests/fixtures/repos/mini_auth", fixture)
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "CodePilot Docker Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "CodePilot Docker Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        git = ("git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-C", str(fixture))
        run(*git, "init", "--initial-branch=main", env=git_env)
        run(*git, "add", "--all", env=git_env)
        run(*git, "commit", "--quiet", "-m", "Phase 1 fixture", env=git_env)
        commit = run(*git, "rev-parse", "HEAD", env=git_env).strip()

        database = root / "index.db"
        run(
            "codepilot",
            "index",
            str(fixture),
            "--base-commit",
            commit,
            "--database-url",
            f"sqlite:///{database}",
            env={
                **git_env,
                "CODEPILOT_CACHE_DIR": str(root / "cache"),
                "CODEPILOT_ARTIFACTS_DIR": str(root / "artifacts"),
            },
        )
        with sqlite3.connect(database) as connection:
            counts = {
                "Files": connection.execute("SELECT count(*) FROM files").fetchone()[0],
                "Symbols": connection.execute("SELECT count(*) FROM symbols").fetchone()[0],
                "Edges": connection.execute("SELECT count(*) FROM edges").fetchone()[0],
                "Tests": connection.execute(
                    "SELECT count(*) FROM symbols WHERE kind IN ('test_function', 'test_method')"
                ).fetchone()[0],
                "Parse errors": connection.execute(
                    "SELECT count(*) FROM files WHERE parse_error IS NOT NULL"
                ).fetchone()[0],
            }
        assert counts == {
            "Files": 14,
            "Symbols": 58,
            "Edges": 200,
            "Tests": 7,
            "Parse errors": 0,
        }, counts
        print("MINI_AUTH", json.dumps({"commit": commit, **counts}), flush=True)
    print("PHASE1_DOCKER_ACCEPTANCE_OK", flush=True)


if __name__ == "__main__":
    main()
