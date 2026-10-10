"""版本信息模块：提供统一的版本获取机制"""

import importlib.metadata as meta
from pathlib import Path

__all__ = ["__version__", "get_version"]


def get_version() -> str:
    """获取 JavSP 版本号

    优先级：
    1. 已安装包的元数据（Docker 镜像构建时写入）
    2. 从 git describe 获取最近 tag（本地源码开发时生效）
    3. 回退 0.0.0
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

    return "0.0.0"


__version__ = get_version()
