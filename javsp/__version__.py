"""版本信息模块：提供统一的版本获取机制，带多层降级策略"""
import importlib.metadata as meta
import re
from pathlib import Path

__all__ = ["__version__", "get_version"]


def get_version() -> str:
    """获取 JavSP 版本号，带降级链

    优先级：
    1. 已安装包的元数据（pip install / poetry install / uv sync 后生效）
    2. 从 pyproject.toml 读取声明的版本（直接源码运行时生效）
    3. 从 javsp/VERSION 文件读取（cx_freeze 打包产物中使用）
    4. 回退 0.0.0
    """
    # 1. 已安装的包元数据
    try:
        return meta.version("javsp")
    except meta.PackageNotFoundError:
        pass

    # 2. 从 pyproject.toml 读取
    try:
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        if pyproject.exists():
            content = pyproject.read_text(encoding="utf-8")
            match = re.search(
                r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE
            )
            if match:
                return match.group(1)
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