"""文件移动失败时整组回滚，保留原始内容。"""

import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from javsp import datatype
from javsp.config import load_config
from javsp.datatype import Movie
from javsp.publish import output


@pytest.fixture
def scene(tmp_path, monkeypatch):
    cfg = SimpleNamespace(scanner=load_config().scanner, summarizer=SimpleNamespace(match_subtitles=True))
    monkeypatch.setattr(datatype, "load_config", lambda: cfg)
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    movie = Movie("ABC-123")
    movie.files = [str(src / f"ABC-123-CD{i}.mp4") for i in (1, 2)]
    for i, file in enumerate(movie.files):
        Path(file).write_bytes(bytes([i]))
    movie.save_dir, movie.basename = str(dst), "renamed"
    return movie, src, dst


@pytest.mark.parametrize("hardlink", [False, True])
def test_conflict_checked_before_first_part(scene, hardlink):
    movie, src, dst = scene
    (dst / "renamed-CD2.mp4").write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        movie.rename_files(hardlink)
    assert (src / "ABC-123-CD1.mp4").read_bytes() == b"\x00"
    assert not (dst / "renamed-CD1.mp4").exists()
    assert (dst / "renamed-CD2.mp4").read_bytes() == b"existing"


@pytest.mark.parametrize("hardlink", [False, True])
def test_partial_copy_failure_rolls_back(scene, monkeypatch, hardlink):
    movie, src, dst = scene
    real_rename = output._rename_new
    real_copy = output.shutil.copy2

    def rename(source, target):
        if str(source) in movie.files:
            raise OSError(errno.EXDEV, "cross device")
        return real_rename(source, target)

    def copy(source, target):
        if str(source) == movie.files[1]:
            Path(target).write_bytes(b"partial")
            raise OSError("disk full")
        return real_copy(source, target)

    monkeypatch.setattr(output, "_rename_new", rename)
    monkeypatch.setattr(output.shutil, "copy2", copy)
    if hardlink:
        def no_links(*args):
            raise OSError("unsupported")
        monkeypatch.setattr(output.os, "link", no_links)
    with pytest.raises(OSError, match="disk full"):
        movie.rename_files(hardlink)
    assert (src / "ABC-123-CD1.mp4").read_bytes() == b"\x00"
    assert (src / "ABC-123-CD2.mp4").read_bytes() == b"\x01"
    assert list(dst.iterdir()) == []


def test_subtitle_failure_rolls_back_video(scene, monkeypatch):
    movie, src, dst = scene
    (src / "ABC-123.srt").write_bytes(b"subtitle")
    real_move = output._rename_new

    def move(source, target):
        if str(source).endswith(".srt"):
            raise OSError("subtitle failure")
        return real_move(source, target)

    monkeypatch.setattr(output, "_rename_new", move)
    with pytest.raises(OSError, match="subtitle failure"):
        movie.rename_files()
    assert list(dst.iterdir()) == []
    assert len(list(src.iterdir())) == 3


def test_rollback_failure_reports_paths_and_keeps_recoverable_file(scene, monkeypatch):
    movie, src, dst = scene
    real_move = output._rename_new

    def move(source, target):
        if str(source) == movie.files[1] or str(source) == str(dst / "renamed-CD1.mp4"):
            raise OSError("permission denied")
        return real_move(source, target)

    monkeypatch.setattr(output, "_rename_new", move)
    with pytest.raises(RuntimeError, match="回滚不完整") as exc:
        movie.rename_files()
    assert str(dst / "renamed-CD1.mp4") in str(exc.value)
    assert str(src / "ABC-123-CD1.mp4") in str(exc.value)
    assert (dst / "renamed-CD1.mp4").read_bytes() == b"\x00"
    assert (src / "ABC-123-CD2.mp4").read_bytes() == b"\x01"
