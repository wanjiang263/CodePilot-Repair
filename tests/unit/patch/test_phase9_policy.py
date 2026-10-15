"""Trusted new-test policy, tool forgery rejection and delivery exclusion."""

import pytest

from codepilot.patch.diff import generate_git_diff
from codepilot.patch.validator import PatchValidator
from codepilot.tools.base import PathAccessError, WorkspacePathPolicy
from support.gitfixture import init_repo


@pytest.mark.parametrize(
    "path", ["tests/test_new.py", "test_new.py", "pkg/new_test.py", "TESTS/test_new.py"]
)
def test_default_deny_new_test(tmp_path, path):
    policy = WorkspacePathPolicy(tmp_path)
    with pytest.raises(PathAccessError):
        policy.resolve(path, must_exist=False, for_write=True)


@pytest.mark.parametrize(
    "body,passed",
    [
        ("def test_new():\n    assert 1 == 1\n", False),
        ("def test_new():\n    pass\n", False),
        ("import pytest\n@pytest.mark.skip\ndef test_new():\n    assert 2 == 2\n", False),
        ("from core import identity\ndef test_new():\n    assert identity(4) == 4\n", True),
    ],
)
def test_explicit_policy_and_ast(tmp_path, body, passed):
    repo = init_repo(tmp_path / "repo", {"core.py": "def identity(x):\n    return x\n"})
    policy = WorkspacePathPolicy(repo.path, allow_new_tests=True)
    policy.resolve("test_new.py", must_exist=False, for_write=True).write_text(
        body, encoding="utf-8"
    )
    validator = PatchValidator(policy, repo.head)
    assert validator.validate().passed is passed
    assert (
        "test_new.py"
        in generate_git_diff(
            repo.path, repo.head, policy=policy, delivery=True, new_tests_reviewed=True
        ).files
    )


def test_bypassed_tool_still_rejected_and_not_delivered(tmp_path):
    repo = init_repo(tmp_path / "repo", {"core.py": "value=1\n"})
    (repo.path / "test_new.py").write_text("def test_new():\n    assert True\n", encoding="utf-8")
    policy = WorkspacePathPolicy(repo.path)
    assert not PatchValidator(policy, repo.head).validate().passed
    diff = generate_git_diff(repo.path, repo.head, policy=policy, delivery=True)
    assert "test_new.py" not in diff.files
    assert "test_new.py" in diff.excluded_files
