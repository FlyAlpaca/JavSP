"""包检查同时验证必需资源和禁止携带的数据。"""

from zipfile import ZipFile

import pytest

from tools.check_dist import ROOT, check_archive


def test_distribution_rejects_database(tmp_path):
    wheel = tmp_path / 'test.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('config_default.yml', '')
        for folder in ('image',):
            for path in (ROOT / folder).iterdir():
                if path.is_file():
                    archive.writestr(path.relative_to(ROOT).as_posix(), '')
        archive.writestr('r18dev.sqlite', 'user data')
    with pytest.raises(ValueError, match='r18dev.sqlite'):
        check_archive(wheel)


def test_distribution_requires_bundled_actor_resources(tmp_path):
    wheel = tmp_path / 'test.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('config_default.yml', '')
        for path in (ROOT / 'image').iterdir():
            if path.is_file():
                archive.writestr(path.relative_to(ROOT).as_posix(), '')
    with pytest.raises(ValueError, match='kanji-variants.tsv'):
        check_archive(wheel)
