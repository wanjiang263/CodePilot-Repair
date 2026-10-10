from pathlib import Path

from codepilot.retrieval.query import build_query, tokenize

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/retrieval/issue.txt"


def test_issue_fields_and_traceback_order():
    query = build_query(
        FIXTURE.read_text(encoding="utf-8"), repo_files=["tests/test_login.py", "src/auth.py"]
    )
    assert [(f.file, f.line, f.function, f.in_repo) for f in query.parsed_frames] == [
        ("tests/test_login.py", 12, "test_expired_token", True),
        ("src/auth.py", 8, "getUserName", True),
        ("/opt/python/site-packages/vendor/auth.py", 2, "check", False),
    ]
    assert len(query.traceback_frames) == 3
    assert {"getUserName", "user_id", "ValueError"} <= set(query.identifiers)
    assert {"getusername", "get", "user", "name", "user_id", "id"} <= set(query.keywords)
    assert "invalid token [expired]" in query.error_messages
    assert "src/auth.py" in query.file_paths
    assert query.test_names == ["test_expired_token"]
    assert "the" not in query.keywords


def test_split_preserves_original_and_repeated_document_words():
    assert tokenize("getUserName user_id HTTPServer user user") == [
        "getusername",
        "get",
        "user",
        "name",
        "user_id",
        "user",
        "id",
        "httpserver",
        "http",
        "server",
        "user",
        "user",
    ]


def test_windows_frames_module_recursion_and_untrusted_paths(tmp_path):
    raw = f'File "{tmp_path / "a.py"}", line 1, in <module>'
    query = build_query(
        raw + "\n" + raw + '\nFile "../a.py", line 3, in f', repo_root=tmp_path, repo_files=["a.py"]
    )
    assert [f.in_repo for f in query.parsed_frames] == [True, True, False]
    assert query.parsed_frames[0].function == "<module>"
    assert len(query.parsed_frames) == 3
    external = build_query('File "C:\\external\\a.py", line 2, in f', repo_files=["a.py"])
    assert not external.parsed_frames[0].in_repo


def test_empty_and_unicode_query_are_deterministic():
    assert build_query("").keywords == []
    query = build_query('登录失败 "令牌过期" `auth.login` `auth.login`')
    assert query.identifiers == ["auth.login"]
    assert query.error_messages == ["令牌过期"]
    assert "登录失败" in query.keywords


def test_apostrophe_in_frame_path_and_empty_exception_message():
    query = build_query(
        'File "src/owner\'s auth.py", line 3, in f\nValueError:\nNext line is prose',
        repo_files=["src/owner's auth.py"],
    )
    assert query.parsed_frames[0].file == "src/owner's auth.py"
    assert query.parsed_frames[0].in_repo
    assert query.error_messages == ["ValueError:"]


def test_windows_traceback_case_and_boundary_are_normalized():
    from unittest.mock import Mock

    root = Mock(spec=Path)
    root.resolve.return_value.as_posix.return_value = "C:/Repo"
    query = build_query(
        'File "c:\\rEpO\\SRC\\a.py", line 2, in f\n'
        'File "C:/Repo-other/src/a.py", line 3, in f\n'
        'File "src/A.py", line 4, in f',
        repo_root=root,
        repo_files=["src/a.py"],
    )
    assert [(f.file, f.in_repo) for f in query.parsed_frames] == [
        ("src/a.py", True),
        ("C:/Repo-other/src/a.py", False),
        ("src/a.py", True),
    ]


def test_posix_traceback_paths_remain_case_sensitive():
    from unittest.mock import Mock

    root = Mock(spec=Path)
    root.resolve.return_value.as_posix.return_value = "/repo"
    query = build_query(
        'File "/Repo/src/a.py", line 1, in f\nFile "src/A.py", line 2, in f',
        repo_root=root,
        repo_files=["src/a.py"],
    )
    assert all(not frame.in_repo for frame in query.parsed_frames)
