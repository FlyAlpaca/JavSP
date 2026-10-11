"""配置文件失败应保留上下文，不能静默回退默认值。"""

import pytest

from javsp.config import ConfigLoadError, _user_config_source


def test_empty_config_uses_defaults(tmp_path):
    path = tmp_path / 'config.yml'
    path.write_text('', encoding='utf-8')
    assert _user_config_source(path) is None


@pytest.mark.parametrize('content', ['scanner: [', '- scalar', 'false'])
def test_invalid_config_stops(tmp_path, content):
    path = tmp_path / 'config.yml'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(ConfigLoadError):
        _user_config_source(path)


def test_missing_explicit_config_preserves_cause(tmp_path):
    with pytest.raises(ConfigLoadError) as error:
        _user_config_source(tmp_path / 'missing.yml')
    assert isinstance(error.value.__cause__, FileNotFoundError)
