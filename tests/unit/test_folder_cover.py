"""离线验证文件夹封面的尺寸、内容和自定义命名兼容性。"""

from types import SimpleNamespace

import pytest
from PIL import Image

from javsp import processor


@pytest.fixture
def poster_config(monkeypatch):
    cover = SimpleNamespace(add_label=False, crop=SimpleNamespace(on_id_pattern=[], engine=None))
    monkeypatch.setattr(processor, "Cfg", lambda: SimpleNamespace(summarizer=SimpleNamespace(cover=cover)))
    return cover


def movie_for(tmp_path, size=(1000, 800), mode="RGB"):
    fanart = tmp_path / "fanart.png"
    Image.new(mode, size, "blue").save(fanart)
    return SimpleNamespace(
        save_dir=str(tmp_path),
        fanart_file=str(fanart),
        poster_file=str(tmp_path / "poster.png"),
        info=SimpleNamespace(uncensored=False, label="ABC"),
        data_src="test",
        hard_sub=False,
        uncensored=False,
    )


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "P"])
@pytest.mark.parametrize("size", [(1000, 800), (60, 80), (200, 1000)])
def test_folder_cover_is_jpeg_and_keeps_aspect_ratio(tmp_path, poster_config, size, mode):
    movie = movie_for(tmp_path, size, mode)
    original = (tmp_path / "fanart.png").read_bytes()

    processor.process_poster(movie)

    with Image.open(movie.poster_file) as poster, Image.open(tmp_path / "folder.jpg") as folder:
        assert folder.format == "JPEG"
        assert folder.mode == "RGB"
        assert folder.width <= min(300, poster.width)
        assert folder.height <= min(450, poster.height)
        assert abs(folder.width / folder.height - poster.width / poster.height) < 0.01
        if poster.width <= 300 and poster.height <= 450:
            assert folder.size == poster.size
        else:
            assert folder.width == 300 or folder.height == 450
    assert (tmp_path / "fanart.png").read_bytes() == original


def test_folder_cover_uses_cropped_poster_with_labels(tmp_path, monkeypatch, poster_config):
    movie = movie_for(tmp_path)
    # 左半边属于封底，默认裁剪应使用右半边的蓝色封面。
    with Image.open(movie.fanart_file) as fanart:
        fanart.paste("red", (0, 0, 400, 800))
        fanart.save(movie.fanart_file)
    poster_config.add_label = True
    movie.hard_sub = True
    movie.uncensored = True
    monkeypatch.setattr(processor, "SUBTITLE_MARK_FILE", Image.new("RGBA", (150, 150), "green"))
    monkeypatch.setattr(processor, "UNCENSORED_MARK_FILE", Image.new("RGBA", (150, 150), "yellow"))

    processor.process_poster(movie)

    with Image.open(tmp_path / "folder.jpg") as folder:
        assert folder.getpixel((20, 20))[2] > 240
        assert folder.getpixel((folder.width - 20, folder.height - 20))[1] > 100
        red, green, blue = folder.getpixel((20, folder.height - 20))
        assert red > 240 and green > 240 and blue < 20


@pytest.mark.parametrize("destination", ["poster_file", "fanart_file"])
def test_custom_folder_filename_preserves_full_size_image(tmp_path, poster_config, destination):
    movie = movie_for(tmp_path)
    setattr(movie, destination, str(tmp_path / "folder.jpg"))
    if destination == "fanart_file":
        Image.new("RGB", (1000, 800), "blue").save(movie.fanart_file)

    processor.process_poster(movie)

    with Image.open(tmp_path / "folder.jpg") as folder:
        assert folder.height == 800
        assert folder.width > 300
