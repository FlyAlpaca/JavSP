"""版本信息模块：提供统一的版本获取机制"""

import importlib.metadata as meta
from pathlib import Path

__all__ = ["__version__", "get_version"]


def get_version() -> str:
    """获取 JavSP 版本号

    优先级：
    1. 已安装包的元数据（pip install / uv sync 后生效）
    2. 从 git describe 获取最近 tag（本地源码开发时生效）
    3. 从 javsp/VERSION 文件读取（cx_freeze 打包产物中使用）
    4. 回退 0.0.0
    """
    # 1. 已安装的包元数据
    try:
        v = meta.version("javsp")
        if v != "0.0.0":
            return v
    except meta.PackageNotFoundError:
        pass

    # 2. 从 git describe 获取版本（本地源码开发时生效）
    try:
        import subprocess

        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).parent.parent,
        )
        if result.returncode == 0:
            tag = result.stdout.strip()
            if tag.startswith("v"):
                return tag[1:]
            return tag
    except Exception:
        pass

    # 3. 从同目录下的 VERSION 文件读取（cx_freeze 打包时写入，见 CI 流程）
    try:
        version_file = Path(__file__).parent / "VERSION"
        if version_file.exists():
            return version_file.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    return "0.0.0"


__version__ = get_version()
