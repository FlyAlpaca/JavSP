"""验证 wheel 携带程序运行时需要的资源。"""

import sys
from pathlib import Path
from zipfile import ZipFile


def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist")
    wheels = list(directory.glob("*.whl"))
    if not wheels:
        print(f"未找到 wheel: {directory}", file=sys.stderr)
        return 1
    required = {"config_default.yml"}
    required.update(str(path.as_posix()) for folder in ("data", "image") for path in Path(folder).iterdir() if path.is_file())
    for wheel in wheels:
        with ZipFile(wheel) as archive:
            missing = required - set(archive.namelist())
        if missing:
            print(f"{wheel} 缺少资源: {', '.join(sorted(missing))}", file=sys.stderr)
            return 1
        print(f"{wheel} 资源检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
