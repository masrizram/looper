"""Proof tests for multi-file artifact extraction (ADR-009).

``single_file`` mode could only ever emit a toy. Package mode parses
attacker-influenced text into filesystem paths, so the tests below are mostly
about what the parser *refuses*.
"""

from __future__ import annotations

import pytest

from looper.artifact import (
    ArtifactFile,
    is_safe_relative_path,
    parse_multifile,
    primary_module,
    verify_python_files,
)

SAMPLE = """Here is the package.

### FILE: src/app/models.py
```python
class User:
    name: str
```

### FILE: src/app/service.py
```python
def greet(user):
    return f"hi {user.name}"
```

### FILE: README.md
```markdown
# App
```
"""


def test_parses_every_marked_file():
    files = parse_multifile(SAMPLE, max_files=25)
    assert [f.path for f in files] == [
        "src/app/models.py",
        "src/app/service.py",
        "README.md",
    ]
    assert "class User" in files[0].content
    assert "```" not in files[0].content


def test_no_markers_means_not_package_output():
    assert parse_multifile("just some prose\n```python\nx=1\n```", max_files=5) == []


def test_empty_text_is_handled():
    assert parse_multifile("", max_files=5) == []
    assert parse_multifile(None, max_files=5) == []  # type: ignore[arg-type]


def test_unfenced_body_is_still_captured():
    files = parse_multifile("### FILE: a.py\nx = 1\n", max_files=5)
    assert files[0].content == "x = 1"


def test_marker_variants_are_accepted():
    text = "## file: a.py\n```\nx=1\n```\n#### File: b.py\n```\ny=2\n```\n"
    assert [f.path for f in parse_multifile(text, max_files=5)] == ["a.py", "b.py"]


def test_empty_body_is_skipped():
    files = parse_multifile("### FILE: a.py\n\n### FILE: b.py\ny = 2\n", max_files=5)
    assert [f.path for f in files] == ["b.py"]


def test_backslash_paths_are_normalised():
    files = parse_multifile("### FILE: src\\app\\x.py\nx=1\n", max_files=5)
    assert files[0].path == "src/app/x.py"


def test_backticked_path_is_cleaned():
    files = parse_multifile("### FILE: `a.py`\nx=1\n", max_files=5)
    assert files[0].path == "a.py"


def test_file_count_is_capped(caplog):
    text = "".join(f"### FILE: f{i}.py\nx = {i}\n" for i in range(10))
    with caplog.at_level("WARNING"):
        files = parse_multifile(text, max_files=3)
    assert len(files) == 3
    assert "dropping the rest" in caplog.text


# -- path safety ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "a/../../b.py",
        "/etc/passwd",
        "\\windows\\system32\\x.py",
        "C:/Windows/x.py",
        "c:relative.py",
        "",
        "dir/",
        "payload.sh",
        "binary.exe",
        "x.py\x00.txt",
    ],
)
def test_unsafe_paths_are_rejected(path: str):
    assert is_safe_relative_path(path) is False


@pytest.mark.parametrize(
    "path",
    ["a.py", "src/app/models.py", "README.md", "pyproject.toml", "conf/x.yaml"],
)
def test_safe_paths_are_accepted(path: str):
    assert is_safe_relative_path(path) is True


def test_unsafe_marker_paths_are_dropped_not_written(caplog):
    text = "### FILE: ../evil.py\nx=1\n### FILE: ok.py\ny=2\n"
    with caplog.at_level("WARNING"):
        files = parse_multifile(text, max_files=5)
    assert [f.path for f in files] == ["ok.py"]
    assert "unsafe artifact path" in caplog.text


# -- syntax verification -------------------------------------------------


def test_verify_requires_at_least_one_python_file():
    ok, note = verify_python_files([ArtifactFile("README.md", "# hi")])
    assert ok is False
    assert "no Python files" in note


def test_verify_rejects_broken_python():
    files = [ArtifactFile("a.py", "def broken(:\n    pass\n")]
    ok, note = verify_python_files(files)
    assert ok is False
    assert "a.py has a syntax error" in note


def test_verify_accepts_valid_package():
    files = parse_multifile(SAMPLE, max_files=25)
    ok, note = verify_python_files(files)
    assert ok is True, note


def test_verify_empty_list_fails_closed():
    ok, _ = verify_python_files([])
    assert ok is False


# -- reviewer view -------------------------------------------------------


def test_primary_module_concatenates_every_python_file():
    """Reviewer/security agents must see all modules, not just the first."""
    blob = primary_module(parse_multifile(SAMPLE, max_files=25))
    assert "class User" in blob
    assert "def greet" in blob
    assert "# ==== src/app/service.py ====" in blob
    # Markdown is not code and must not be audited as such.
    assert "# App" not in blob
