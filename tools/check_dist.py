"""验证 wheel/sdist 的运行资源，并拒绝携带用户数据和恢复材料。"""

import sys
import tarfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent


def forbidden(name):
    path = PurePosixPath(name)
    return (
        any(part.startswith('.javsp-') or part == '__pycache__' for part in path.parts)
        or path.name in {'config.yml', 'recovery.json', 'actor-mapping.xml', 'actor-mapping.json'}
        or any(suffix in path.name for suffix in ('.sqlite', '.gguf'))
        or path.suffix in {'.part', '.log', '.pyc'}
    )


def check_archive(artifact):
    if artifact.suffix == '.whl':
        with ZipFile(artifact) as archive:
            names = set(archive.namelist())
    else:
        with tarfile.open(artifact) as archive:
            names = {name.partition('/')[2] for name in archive.getnames()}
    required = {'config_default.yml'}
    required.update(
        str(path.relative_to(ROOT).as_posix())
        for folder in ('data', 'image') for path in (ROOT / folder).iterdir() if path.is_file()
    )
    missing = required - names
    prohibited = sorted(name for name in names if forbidden(name))
    if missing or prohibited:
        raise ValueError(f'{artifact}: 缺少资源 {sorted(missing)}; 禁止携带 {prohibited}')


def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('dist')
    artifacts = sorted([*directory.glob('*.whl'), *directory.glob('*.tar.gz')])
    if not any(path.suffix == '.whl' for path in artifacts) or not any(path.name.endswith('.tar.gz') for path in artifacts):
        print(f'需要本次构建的 wheel 和 sdist: {directory}', file=sys.stderr)
        return 1
    for artifact in artifacts:
        try:
            check_archive(artifact)
        except (ValueError, OSError, tarfile.TarError) as error:
            print(error, file=sys.stderr)
            return 1
        print(f'{artifact.name} 资源与数据边界检查通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
