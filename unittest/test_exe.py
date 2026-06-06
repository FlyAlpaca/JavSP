import os
import subprocess
from glob import glob


def test_javsp_exe(tmp_path):
    cwd = os.getcwd()
    dist_dir = os.path.normpath(os.path.join(os.path.dirname(__file__) + "/../dist"))
    os.chdir(dist_dir)

    size = 300 * 2**20
    FILE = "300MAAN-642.RIP.f4v"
    try:
        os.system(f"fsutil file createnew {FILE} {size}")
        r = subprocess.run(
            f"JavSP-bin.exe --auto-exit --input . --output {tmp_path}".split(),
            capture_output=True,
            encoding="utf-8",
        )
        r.check_returncode()
        files = glob(os.path.join(str(tmp_path), "**/*.*"), recursive=True)
        assert any(i.endswith("fanart.jpg") for i in files), "fanart not found"
        assert any(i.endswith("poster.jpg") for i in files), "poster not found"
        assert any(i.endswith(".f4v") for i in files), "video file not found"
        assert any(i.endswith(".nfo") for i in files), "nfo file not found"
    finally:
        if os.path.exists(FILE):
            os.remove(FILE)
        os.chdir(cwd)
