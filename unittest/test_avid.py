import os

import pytest

from javsp.avid import get_cid, get_id

file_dir = os.path.dirname(__file__)


@pytest.fixture
def prepare_files(files, tmp_path):
    """按照指定的文件列表创建对应的文件"""
    for i in files:
        path = tmp_path / i
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(path))


def test_fc2():
    assert "FC2-123456" == get_id("(2017) [FC2-123456] 【個人撮影】")
    assert "FC2-123456" == get_id("fc2-ppv-123456-1.delogo.mp4")
    assert "FC2-123456" == get_id("FC2-PPV-123456.mp4")
    assert "FC2-123456" == get_id("FC2PPV-123456 Yuukiy")
    assert "FC2-1234567" == get_id("fc2-ppv_1234567-2.mp4")


def test_normal():
    assert "" == get_id("Yuukiy")
    assert "ABC-12" == get_id("ABC-12_01.mkv")
    assert "ABC-123" == get_id("Sky Angel Vol.6 月丘うさぎ(ABC-123).avi")
    assert "ABCD-123" == get_id("ABCD-123.mp4")


def test_cid_valid():
    assert "ab012st" == get_cid("ab012st")
    assert "ab012st" == get_cid("ab012st.mp4")
    assert "123_0456" == get_cid("123_0456.mp4")
    assert "123abc00045" == get_cid("123abc00045.mp4")
    assert "403abcd56789" == get_cid("403abcd56789_1")
    assert "h_001abc00001" == get_cid("h_001abc00001.mp4")
    assert "1234wvr00001rp" == get_cid("1234wvr00001rp.mp4")
    assert "402abc_hello000089" == get_cid("402abc_hello000089.mp4")
    assert "h_826zizd021" == get_cid("h_826zizd021.mp4")
    assert "403abcd56789" == get_cid("403abcd56789cd1.mp4")


def test_from_file():
    # 用来控制是否将转换结果覆盖原文件（便于检查所有失败的条目）
    write_back = False
    rewrite_lines = []

    datafile = os.path.join(file_dir, "testdata_avid.txt")
    with open(datafile, encoding="utf-8") as f:
        lines = f.readlines()
        for line_no, line in enumerate(lines, start=1):
            items = line.strip("\r\n").split("\t")
            if len(items) == 2:
                (filename, avid), ignore = items, False
            else:
                filename, avid, ignore = items
            guess_id = get_id(filename)
            if write_back:
                rewrite_lines.append(f"{filename}\t{guess_id}\n")
                continue
            if guess_id != avid:
                if ignore:
                    print(f"Ignored: {guess_id} != {avid}\t'{filename}'")
                else:
                    assert guess_id == avid.upper(), f"AV ID not match at line {line_no}"
    if write_back:
        with open(datafile, "w", encoding="utf-8") as f:
            f.writelines(rewrite_lines)


def test_cid_invalid():
    assert "" == get_cid("hasUpperletter.mp4")
    assert "" == get_cid("存在非ASCII字符.mp4")
    assert "" == get_cid("has-dash.mp4")
    assert "" == get_cid("403_abcd56789_fgh")
    assert "" == get_cid("many_parts1234-12.mp4")
    assert "" == get_cid("abc12.mp4")
    assert "" == get_cid("ab012st/仅文件夹名称为cid.mp4")
    assert "" == get_cid("123_0456st.mp4")


@pytest.mark.parametrize("files", [("Unknown.mp4",)])
def test_by_folder_name1(prepare_files):
    assert "" == get_id("Unknown.mp4")


@pytest.mark.parametrize("files", [("FC2-123456/Unknown.mp4",)])
def test_by_folder_name2(prepare_files):
    assert "FC2-123456" == get_id("FC2-123456/Unknown.mp4")


@pytest.mark.parametrize("files", [("ABC-123/CDF-456.mp4",)])
def test_by_folder_name3(prepare_files):
    assert "CDF-456" == get_id("ABC-123/CDF-456.mp4")
