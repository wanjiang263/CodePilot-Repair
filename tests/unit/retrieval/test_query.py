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
