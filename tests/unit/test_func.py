import random

from javsp import func
from javsp.func import remove_trail_actor_in_title


def test_remove_trail_actor_in_title():
    run = remove_trail_actor_in_title
    delimiters = list("-xX &·,;　＆・，；")
    title1 = "东风夜放花千树，更吹落、星如雨。"
    title2 = "辛弃疾 " + title1
    names = ["辛弃疾", "牛顿", "爱因斯坦", "阿基米德", "伽利略"]

    def combine(items):
        sep = random.choice(delimiters)
        new_str = sep.join(items)
        return new_str

    # 定义测试用例
    assert title1 == run(combine([title1, "辛弃疾"]), names)
    assert title1 == run(combine([title1] + names), names)
    assert title1 == run(combine([title1, "辛弃疾"]), names)
    assert title2 == run(combine([title2] + names), names)


def test_noninteractive_scan_directory_never_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(func, "prompt", lambda *args: raise_prompt())
    movie_file = tmp_path / "movie.mp4"
    movie_file.touch()
    assert func.get_scan_dir(tmp_path, interactive=False) == str(tmp_path)
    assert func.get_scan_dir(movie_file, interactive=False) is None
    assert func.get_scan_dir(tmp_path / "missing", interactive=False) is None
    assert func.get_scan_dir(None, interactive=False) is None


def raise_prompt():
    raise AssertionError("非交互模式不应提示选目录")
