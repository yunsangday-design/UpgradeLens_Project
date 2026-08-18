"""S3: deterministic ChangeSet + impact analysis (offline tests)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from upgradelens.change import (
    analyze_impact,
    collect_git_diff,
    extract_symbols,
    is_safe_path,
    module_imports,
    parse_unified_diff,
)
from upgradelens.change.models import ChangeLabel
from upgradelens.repository import scan_repository

_SAMPLE_DIFF = """\
diff --git a/old.py b/new.py
similarity index 90%
rename from old.py
rename to new.py
index 1111111..2222222 100644
--- a/old.py
+++ b/new.py
@@ -1,2 +1,2 @@
-old line
+new line

diff --git a/main.py b/main.py
index 3333333..4444444 100644
--- a/main.py
+++ b/main.py
@@ -1,3 +1,4 @@
 def f():
-    pass
+    return 1
+
+# added comment

diff --git a/added.py b/added.py
new file mode 100644
index 0000000..5555555
--- /dev/null
+++ b/added.py
@@ -0,0 +1,2 @@
+def g():
+    return 2

diff --git a/removed.py b/removed.py
deleted file mode 100644
index 6666666..0000000
--- a/removed.py
+++ /dev/null
@@ -1,1 +0,0 @@
-old content

diff --git a/img.png b/img.png
index 7777777..8888888 100644
Binary files a/img.png and b/img.png differ
"""


def test_parse_unified_diff_all_labels() -> None:
    cs = parse_unified_diff(_SAMPLE_DIFF)
    by_path = {f.path: f for f in cs.files}
    assert by_path["new.py"].label is ChangeLabel.RENAMED
    assert by_path["new.py"].old_path == "old.py"
    assert by_path["main.py"].label is ChangeLabel.MODIFIED
    assert by_path["main.py"].additions == 3
    assert by_path["main.py"].deletions == 1
    assert by_path["added.py"].label is ChangeLabel.ADDED
    assert by_path["removed.py"].label is ChangeLabel.DELETED
    assert by_path["img.png"].label is ChangeLabel.BINARY
    assert cs.stat.files_changed == 5
    assert cs.stat.additions == 6  # rename 1 + main 3 + added 2
    assert cs.stat.deletions == 3  # rename 1 + main 1 + removed 1


def test_is_safe_path_rejects_traversal() -> None:
    assert is_safe_path("src/foo.py")
    assert not is_safe_path("../etc/passwd")
    assert not is_safe_path("/abs/path.py")
    assert not is_safe_path("a/../../b")


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"], check=True
    )


def test_collect_git_diff_on_temp_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "main.py").write_text("def f():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    (repo / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "change"], check=True)

    cs = collect_git_diff(repo, "HEAD~1..HEAD")
    assert cs.files
    assert cs.files[0].path == "main.py"
    assert cs.files[0].label is ChangeLabel.MODIFIED
    assert cs.files[0].additions >= 1
    assert cs.files[0].deletions >= 1


def test_extract_symbols_and_imports() -> None:
    src = (
        "import os\n"
        "from pkg.mod import Thing\n"
        "\n"
        "class A:\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "def top():\n"
        "    return 2\n"
    )
    syms = extract_symbols("m.py", src)
    names = {s.name for s in syms}
    assert "A" in names
    assert "top" in names
    assert "method" in names
    imports = module_imports(src)
    assert "os" in imports
    assert "pkg.mod" in imports


def test_analyze_impact_one_hop(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "b.py").write_text("class B:\n    def m(self):\n        pass\n", encoding="utf-8")
    (pkg / "a.py").write_text(
        "from pkg.b import B\n\nclass A:\n    def use(self, b: B):\n        return b\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/pkg/b.py b/pkg/b.py\n"
        "index 111..222 100644\n"
        "--- a/pkg/b.py\n"
        "+++ b/pkg/b.py\n"
        "@@ -1,3 +1,3 @@\n"
        " class B:\n"
        "     def m(self):\n"
        "-        pass\n"
        "+        return 1\n"
    )
    cs = parse_unified_diff(diff)
    impact = analyze_impact(cs, tmp_path)
    direct_names = {s.name for s in impact.direct}
    impacted_names = {s.name for s in impact.impacted}
    assert "B" in direct_names
    # pkg.a imports pkg.b -> A is one hop away.
    assert "A" in impacted_names
    assert impact.labels.get("pkg/b.py") == "modified"


def test_scan_repository(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["pydantic>=2", "requests"]\n',
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "mod.py").write_text("def helper():\n    return 3\n", encoding="utf-8")

    profile = scan_repository(tmp_path)
    langs = {lang.language for lang in profile.languages}
    assert "python" in langs
    pyproject = next(m for m in profile.manifests if m.path.endswith("pyproject.toml"))
    assert pyproject.ecosystem == "pypi"
    assert "pydantic>=2" in pyproject.dependencies
    assert any(p.endswith("test_demo.py") for p in profile.tests.test_paths)
    assert profile.symbols  # at least the test function + helper
