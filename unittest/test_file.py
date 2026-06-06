import os

import pytest

from javsp.file import scan_movies

DEFAULT_SIZE = 512 * 2**20  # 512 MiB


def touch_file_size(path: str, size_bytes: int):
    with open(path, "wb") as f:
        f.seek(size_bytes - 1)
        f.write(b"\0")


@pytest.fixture
def prepare_files(files, tmp_path):
    """按照指定的文件列表创建对应的文件"""
    if not isinstance(files, dict):
        files = {i: DEFAULT_SIZE for i in files}
    for name, size in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        touch_file_size(str(path), size)
    return str(tmp_path)


# 根文件夹下的单个影片文件
@pytest.mark.parametrize("files", [("ABC-123.mp4",)])
def test_single_movie(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "ABC-123"
    assert len(movies[0].files) == 1
    basenames = [os.path.basename(i) for i in movies[0].files]
    assert basenames[0] == "ABC-123.mp4"


# 多个分片以数字排序: 012
@pytest.mark.parametrize("files", [("ABC-123-0.mp4", "ABC-123-1.mp4", "ABC-123- 2.mp4")])
def test_scan_movies__012(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "ABC-123"
    assert len(movies[0].files) == 3
    basenames = [os.path.basename(i) for i in movies[0].files]
    assert basenames[0] == "ABC-123-0.mp4"
    assert basenames[1] == "ABC-123-1.mp4"
    assert basenames[2] == "ABC-123- 2.mp4"


# 多个分片以数字排序: 123
@pytest.mark.parametrize("files", [("ABC-123.1.mp4", "ABC-123. 2.mp4", "ABC-123.3.mp4")])
def test_scan_movies__123(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "ABC-123"
    assert len(movies[0].files) == 3
    basenames = [os.path.basename(i) for i in movies[0].files]
    assert basenames[0] == "ABC-123.1.mp4"
    assert basenames[1] == "ABC-123. 2.mp4"
    assert basenames[2] == "ABC-123.3.mp4"


# 多个分片以字母排序
@pytest.mark.parametrize("files", [("ABC-123-A.mp4", "ABC-123-B.mp4", "ABC-123- C .mp4")])
def test_scan_movies__abc(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "ABC-123"
    assert len(movies[0].files) == 3
    basenames = [os.path.basename(i) for i in movies[0].files]
    assert basenames[0] == "ABC-123-A.mp4"
    assert basenames[1] == "ABC-123-B.mp4"
    assert basenames[2] == "ABC-123- C .mp4"


# 多个分片以.CDx编号
@pytest.mark.parametrize("files", [("ABC-123.CD1.mp4", "ABC-123.CD2 .mp4", "ABC-123.CD3.mp4")])
def test_scan_movies__cdx(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "ABC-123"
    assert len(movies[0].files) == 3
    basenames = [os.path.basename(i) for i in movies[0].files]
    assert basenames[0] == "ABC-123.CD1.mp4"
    assert basenames[1] == "ABC-123.CD2 .mp4"
    assert basenames[2] == "ABC-123.CD3.mp4"


@pytest.mark.parametrize("files", [("abc123cd1.mp4", "abc123cd2.mp4")])
def test_scan_movies__cdx_without_delimeter(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "ABC-123"
    assert len(movies[0].files) == 2
    basenames = [os.path.basename(i) for i in movies[0].files]
    assert basenames[0] == "abc123cd1.mp4"
    assert basenames[1] == "abc123cd2.mp4"


# 文件夹以番号命名，分片位于文件夹内且无番号信息
@pytest.mark.parametrize("files", [("ABC-123/CD1.mp4", "ABC-123/CD2 .mp4", "ABC-123/CD3.mp4")])
def test_scan_movies__from_folder(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "ABC-123"
    assert len(movies[0].files) == 3
    basenames = [os.path.basename(i) for i in movies[0].files]
    assert basenames[0] == "CD1.mp4"
    assert basenames[1] == "CD2 .mp4"
    assert basenames[2] == "CD3.mp4"


# 分片以多位数字编号
@pytest.mark.parametrize("files", [("ABC-123.01.mp4", "ABC-123.02.mp4", "ABC-123.03.mp4")])
def test_scan_movies__0x123(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "ABC-123"
    assert len(movies[0].files) == 3
    basenames = [os.path.basename(i) for i in movies[0].files]
    assert basenames[0] == "ABC-123.01.mp4"
    assert basenames[1] == "ABC-123.02.mp4"
    assert basenames[2] == "ABC-123.03.mp4"


# 无效: 没有可以匹配到番号的文件
@pytest.mark.parametrize("files", [("什么也没有.mp4",)])
def test_scan_movies__nothing(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 0


# 无效: 在CWD下没有可以匹配到番号的文件
@pytest.mark.parametrize("files", [("什么也没有.mp4",)])
def test_scan_movies__nothing_in_cwd(prepare_files):
    cwd = os.getcwd()
    os.chdir(prepare_files)
    try:
        movies = scan_movies(".")
    finally:
        os.chdir(cwd)
    assert len(movies) == 0


# 无效：多个分片命名杂乱
@pytest.mark.parametrize("files", [("ABC-123-1.mp4", "ABC-123-第2部分.mp4", "ABC-123-3.mp4")])
def test_scan_movies__strange_names(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 0


# 无效：同一影片的分片和非分片混合
@pytest.mark.parametrize("files", [("ABC-123.mp4", "ABC-123-1.mp4", "ABC-123-2.mp4")])
def test_scan_movies__mix_slices(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 0


# 无效：多个分片位于不同文件夹
@pytest.mark.parametrize("files", [("ABC-123.CD1.mp4", "sub/ABC-123.CD2.mp4", "ABC-123.CD3.mp4")])
def test_scan_movies__wrong_structure(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 0


# 无效：分片的起始编号不合法
@pytest.mark.parametrize("files", [("ABC-123.CD2.mp4", "ABC-123.CD3.mp4", "ABC-123.CD4.mp4")])
def test_scan_movies__wrong_initial_id(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 0


# 无效：分片的编号不连续
@pytest.mark.parametrize("files", [("ABC-123.CD1.mp4", "ABC-123.CD3.mp4", "ABC-123.CD4.mp4")])
def test_scan_movies__not_consecutive(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 0


# 无效：分片的编号重复
@pytest.mark.parametrize("files", [("ABC-123-1.mp4", "ABC-123-1 .mp4", "ABC-123-3.mp4")])
def test_scan_movies__duplicate_index(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 0


# 混合有效和无效数据
@pytest.mark.parametrize(
    "files",
    [("DEF-456/movie.mp4", "ABC-123.1.mp4", "sub/ABC-123.2.mp4", "ABC-123.3.mp4")],
)
def test_scan_movies__mix_data(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "DEF-456"
    assert len(movies[0].files) == 1
    basenames = [os.path.basename(i) for i in movies[0].files]
    assert basenames[0] == "movie.mp4"


# 文件夹以番号命名，文件夹内同时有带番号的影片和广告
@pytest.mark.parametrize(
    "files",
    [
        {
            "ABC-123/ABC-123.mp4": DEFAULT_SIZE,
            "ABC-123/广告1.mp4": 1024,
            "ABC-123/广告2.mp4": 243269631,
        }
    ],
)
def test_scan_movies__1_video_with_ad(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 1
    assert movies[0].dvdid == "ABC-123"
    assert len(movies[0].files) == 1


# 文件夹内同时有多部带番号的影片和广告
@pytest.mark.parametrize(
    "files",
    [
        {
            "ABC-123.mp4": DEFAULT_SIZE,
            "DEF-456.mp4": DEFAULT_SIZE,
            "广告1.mp4": 1024,
            "广告2.mp4": 243269631,
        }
    ],
)
def test_scan_movies__n_video_with_ad(prepare_files):
    movies = scan_movies(prepare_files)
    assert len(movies) == 2
    dvdids = {m.dvdid for m in movies}
    assert dvdids == {"ABC-123", "DEF-456"}
    assert all(len(i.files) == 1 for i in movies)
