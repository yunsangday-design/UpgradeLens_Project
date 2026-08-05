from __future__ import annotations

from pathlib import Path

from upgradelens.platform import read_text_utf8, to_posix_rel_path


def test_posix_rel_path_uses_forward_slash(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "src" / "models.py"
    target.parent.mkdir(parents=True)

    assert to_posix_rel_path(repo, target) == "src/models.py"


def test_posix_rel_path_handles_chinese_and_space_names(tmp_path: Path) -> None:
    repo = tmp_path / "仓库 root"
    repo.mkdir()
    target = repo / "源" / "my module.py"
    target.parent.mkdir(parents=True)

    assert to_posix_rel_path(repo, target) == "源/my module.py"


def test_read_text_accepts_lf(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("line1\nline2\n", encoding="utf-8")
    assert read_text_utf8(path).splitlines() == ["line1", "line2"]


def test_read_text_accepts_crlf(tmp_path: Path) -> None:
    # Write raw bytes so the on-disk content is exactly CRLF regardless of the
    # platform's text-mode newline translation.
    path = tmp_path / "b.txt"
    path.write_bytes(b"line1\r\nline2\r\n")
    assert read_text_utf8(path).splitlines() == ["line1", "line2"]


def test_read_text_accepts_utf8_non_ascii(tmp_path: Path) -> None:
    path = tmp_path / "c.txt"
    path.write_text("# 注释：升级目标 pydantic\n", encoding="utf-8")
    assert "升级目标" in read_text_utf8(path)
