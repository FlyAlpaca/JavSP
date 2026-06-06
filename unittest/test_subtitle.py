import os

import pytest

from javsp.datatype import Movie
from javsp.file import find_matching_subtitles

DEFAULT_SIZE = 512 * 2**20  # 512 MiB


def touch_file_size(path, size_bytes):
    with open(path, "wb") as f:
        f.seek(size_bytes - 1)
        f.write(b"\0")


# ===================== find_matching_subtitles =====================


@pytest.mark.parametrize(
    "names, sub_ext",
    [
        (("IPZ-380.mp4", "IPZ-380.srt"), ".srt"),
        (("IPZ-380.mp4", "IPZ-380.ass"), ".ass"),
    ],
)
def test_find_subtitle_exist(names, sub_ext, tmp_path):
    for name in names:
        touch_file_size(os.path.join(tmp_path, name), 1024)
    subtitles = find_matching_subtitles(os.path.join(tmp_path, names[0]))
    assert len(subtitles) == 1
    assert subtitles[0].endswith(sub_ext)


def test_find_subtitle_multiple_formats(tmp_path):
    touch_file_size(os.path.join(tmp_path, "IPZ-380.mp4"), 1024)
    touch_file_size(os.path.join(tmp_path, "IPZ-380.srt"), 1024)
    touch_file_size(os.path.join(tmp_path, "IPZ-380.ass"), 1024)
    subtitles = find_matching_subtitles(os.path.join(tmp_path, "IPZ-380.mp4"))
    assert len(subtitles) == 2
    basenames = {os.path.basename(s) for s in subtitles}
    assert basenames == {"IPZ-380.srt", "IPZ-380.ass"}


def test_find_subtitle_nonexist(tmp_path):
    touch_file_size(os.path.join(tmp_path, "IPZ-380.mp4"), 1024)
    subtitles = find_matching_subtitles(os.path.join(tmp_path, "IPZ-380.mp4"))
    assert len(subtitles) == 0


# ===================== Movie.rename_files 字幕移动 =====================


def _setup_scene(tmp_path, video, subtitle=None):
    """创建源目录和目标目录，返回 movie 对象"""
    src = tmp_path / "src"
    dst = tmp_path / "output"
    src.mkdir()
    dst.mkdir()
    touch_file_size(src / video, DEFAULT_SIZE)
    if subtitle:
        touch_file_size(src / subtitle, 1024)
    return src, dst


def test_rename_files_exact_match(tmp_path):
    src, dst = _setup_scene(tmp_path, "IPZ-380.mp4", "IPZ-380.srt")
    movie = Movie("IPZ-380")
    movie.files = [str(src / "IPZ-380.mp4")]
    movie.save_dir = str(dst)
    movie.basename = "IPZ-380"
    movie.rename_files()

    assert (dst / "IPZ-380.mp4").exists()
    assert (dst / "IPZ-380.srt").exists()
    assert not (src / "IPZ-380.srt").exists()


def test_rename_files_fuzzy_no_dash(tmp_path):
    """ipz380.srt 应通过番号 IPZ-380 匹配到视频"""
    src, dst = _setup_scene(tmp_path, "IPZ-380.mp4", "ipz380.srt")
    movie = Movie("IPZ-380")
    movie.files = [str(src / "IPZ-380.mp4")]
    movie.save_dir = str(dst)
    movie.basename = "IPZ-380"
    movie.rename_files()

    assert (dst / "IPZ-380.mp4").exists()
    # 字幕被重命名为目标 basename
    assert (dst / "IPZ-380.srt").exists()
    assert not (src / "ipz380.srt").exists()


def test_rename_files_fuzzy_zero_padding(tmp_path):
    """ipz00380.srt 应通过番号规范化（00380→380）匹配到视频"""
    src, dst = _setup_scene(tmp_path, "IPZ-380.mp4", "ipz00380.srt")
    movie = Movie("IPZ-380")
    movie.files = [str(src / "IPZ-380.mp4")]
    movie.save_dir = str(dst)
    movie.basename = "IPZ-380"
    movie.rename_files()

    assert (dst / "IPZ-380.mp4").exists()
    assert (dst / "IPZ-380.srt").exists()
    assert not (src / "ipz00380.srt").exists()


def test_rename_files_different_dvdid_not_match(tmp_path):
    """ABC-123.srt 不应匹配 IPZ-380 的视频"""
    src, dst = _setup_scene(tmp_path, "IPZ-380.mp4", "ABC-123.srt")
    movie = Movie("IPZ-380")
    movie.files = [str(src / "IPZ-380.mp4")]
    movie.save_dir = str(dst)
    movie.basename = "IPZ-380"
    movie.rename_files()

    assert (dst / "IPZ-380.mp4").exists()
    assert (src / "ABC-123.srt").exists()
    assert not (dst / "ABC-123.srt").exists()


def test_rename_files_ass_format(tmp_path):
    src, dst = _setup_scene(tmp_path, "IPZ-380.mp4", "IPZ-380.ass")
    movie = Movie("IPZ-380")
    movie.files = [str(src / "IPZ-380.mp4")]
    movie.save_dir = str(dst)
    movie.basename = "IPZ-380"
    movie.rename_files()

    assert (dst / "IPZ-380.mp4").exists()
    assert (dst / "IPZ-380.ass").exists()
    assert not (src / "IPZ-380.ass").exists()


def test_rename_files_subtitle_name_follows_video(tmp_path):
    """字幕文件名应跟随目标 basename（custom_name），而非原始文件名"""
    src, dst = _setup_scene(tmp_path, "IPZ-380.mp4", "IPZ-380.srt")
    movie = Movie("IPZ-380")
    movie.files = [str(src / "IPZ-380.mp4")]
    movie.save_dir = str(dst)
    movie.basename = "custom_name"
    movie.rename_files()

    assert (dst / "custom_name.mp4").exists()
    assert (dst / "custom_name.srt").exists()


def test_rename_files_multicd_video_single_subtitle(tmp_path):
    """单文件但名称含 CD1，视频被重命名为 basename，字幕跟随 basename"""
    src, dst = _setup_scene(tmp_path, "IPZ-380-CD1.mp4", "IPZ-380.srt")
    movie = Movie("IPZ-380")
    movie.files = [str(src / "IPZ-380-CD1.mp4")]
    movie.save_dir = str(dst)
    movie.basename = "IPZ-380"
    movie.rename_files()

    assert (dst / "IPZ-380.mp4").exists()
    assert (dst / "IPZ-380.srt").exists()


def test_rename_files_subtitle_already_in_dest(tmp_path):
    """目标已存在同名字幕文件时，应跳过不移动"""
    src, dst = _setup_scene(tmp_path, "IPZ-380.mp4", "IPZ-380.srt")
    touch_file_size(dst / "IPZ-380.srt", 1024)

    movie = Movie("IPZ-380")
    movie.files = [str(src / "IPZ-380.mp4")]
    movie.save_dir = str(dst)
    movie.basename = "IPZ-380"
    movie.rename_files()

    assert (dst / "IPZ-380.mp4").exists()
    assert (dst / "IPZ-380.srt").exists()
    # 目标已存在时源字幕文件不会被删除
    assert (src / "IPZ-380.srt").exists()
