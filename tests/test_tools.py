# tests/test_tools.py
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

STUB_CONFIG = {
    "tools": {
        "allowed_paths": [],  # overridden per test
        "code_timeout": 2,
    }
}

# --- web_search ---

def test_web_search_returns_formatted_string():
    with patch("jarvis.tools.web_search.DDGS") as mock_ddgs:
        mock_ddgs.return_value.text.return_value = [
            {"title": "Python", "body": "A programming language.", "href": "https://python.org"}
        ]
        from jarvis.tools.web_search import search
        result = search("what is Python")
        assert isinstance(result, str)
        assert "Python" in result

def test_web_search_returns_no_results_message():
    with patch("jarvis.tools.web_search.DDGS") as mock_ddgs:
        mock_ddgs.return_value.text.return_value = []
        from jarvis.tools.web_search import search
        result = search("xyzzy nothing happens")
        assert result == "No results found."

# --- code_exec ---

def test_code_exec_returns_stdout():
    with patch("jarvis.tools.code_exec._load_config", return_value=STUB_CONFIG):
        from jarvis.tools.code_exec import run_python
        result = run_python("print('hello jarvis')")
        assert "hello jarvis" in result

def test_code_exec_returns_stderr_on_error():
    with patch("jarvis.tools.code_exec._load_config", return_value=STUB_CONFIG):
        from jarvis.tools.code_exec import run_python
        result = run_python("raise ValueError('test error')")
        assert "ValueError" in result or "error" in result.lower()

def test_code_exec_times_out():
    with patch("jarvis.tools.code_exec._load_config", return_value=STUB_CONFIG):
        from jarvis.tools.code_exec import run_python
        result = run_python("import time; time.sleep(999)")
        assert "timeout" in result.lower() or "timed out" in result.lower()

# --- file_ops ---

def test_file_write_and_read(tmp_path):
    cfg = {"tools": {"allowed_paths": [str(tmp_path)], "code_timeout": 10}}
    with patch("jarvis.tools.file_ops._load_config", return_value=cfg):
        from jarvis.tools.file_ops import read_file, write_file
        write_file(str(tmp_path / "test.txt"), "hello")
        content = read_file(str(tmp_path / "test.txt"))
        assert content == "hello"

def test_file_read_blocked_outside_allowed(tmp_path):
    cfg = {"tools": {"allowed_paths": [str(tmp_path)], "code_timeout": 10}}
    outside = "C:/Windows/System32/config/SAM" if sys.platform == "win32" else "/etc/passwd"
    with patch("jarvis.tools.file_ops._load_config", return_value=cfg):
        from jarvis.tools.file_ops import read_file
        with pytest.raises(PermissionError):
            read_file(outside)

def test_file_ops_expand_user_home(tmp_path):
    """'~' in a tool path must resolve like the '~' entries in allowed_paths."""
    cfg = {"tools": {"allowed_paths": ["~/jarvis_test_dir"], "code_timeout": 10}}
    with patch("jarvis.tools.file_ops._load_config", return_value=cfg), \
         patch.dict(os.environ, {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}):
        (tmp_path / "jarvis_test_dir").mkdir()
        from jarvis.tools.file_ops import write_file, read_file, list_files
        write_file("~/jarvis_test_dir/note.txt", "hi")
        assert read_file("~/jarvis_test_dir/note.txt") == "hi"
        assert "note.txt" in list_files("~/jarvis_test_dir")

# --- router ---

def test_router_dispatch_unknown_tool():
    from jarvis.tools.router import dispatch
    result = dispatch("nonexistent_tool", {})
    assert "Unknown tool" in result

def test_router_dispatch_known_tool():
    with patch("jarvis.tools.router.search") as mock_search:
        mock_search.return_value = "Search result"
        from jarvis.tools.router import dispatch
        result = dispatch("web_search", {"query": "test"})
        assert result == "Search result"
