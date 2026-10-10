import subprocess
import sys
from pathlib import Path

import pytest


def test_javsp_exe(tmp_path):
    exe = Path(__file__).resolve().parents[2] / "dist" / "JavSP-bin.exe"
    if sys.platform != "win32" or not exe.is_file():
        pytest.skip("需要 Windows 环境及 dist/JavSP-bin.exe 打包产物")

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    with (input_dir / "300MAAN-642.RIP.f4v").open("wb") as video:
        video.truncate(300 * 2**20)
    result = subprocess.run(
        [str(exe), "--auto-exit", "--input", str(input_dir), "--output", str(output_dir)],
        cwd=exe.parent,
        capture_output=True,
        encoding="utf-8",
        timeout=300,
    )
    result.check_returncode()
    files = list(output_dir.rglob("*.*"))
    assert any(i.name == "fanart.jpg" for i in files), "fanart not found"
    assert any(i.name == "poster.jpg" for i in files), "poster not found"
    assert any(i.suffix == ".f4v" for i in files), "video file not found"
    assert any(i.suffix == ".nfo" for i in files), "nfo file not found"
