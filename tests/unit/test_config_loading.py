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


@pytest.mark.parametrize('source', ['file', 'environment', 'cli'])
def test_removed_options_are_ignored_with_safe_migration_notice(tmp_path, monkeypatch, caplog, source):
    from javsp.config import load_config

    path = tmp_path / 'config.yml'
    path.write_text('{}', encoding='utf-8')
    argv = ['-c', str(path)]
    removed = {
        'crawler.online_enabled': 'true',
        'crawler.normalize_actress_name': 'false',
        'crawler.required_keys': 'private-value',
        'crawler.respect_site_avid': 'false',
        'r18dev.enabled': 'false',
        'network.proxy_free': 'private-value',
        'concurrency.crawlers': 'invalid-value',
    }
    if source == 'file':
        path.write_text(
            'crawler:\n  online_enabled: true\n  normalize_actress_name: false\n'
            '  required_keys: private-value\n  respect_site_avid: false\n'
            'r18dev:\n  enabled: false\nnetwork:\n  proxy_free: private-value\n'
            'concurrency:\n  crawlers: invalid-value\n', encoding='utf-8',
        )
    elif source == 'environment':
        for key, value in removed.items():
            monkeypatch.setenv('JAVSP_' + key.replace('.', '__').upper(), value)
    else:
        for key, value in removed.items():
            argv.extend(['--o' + key, value])
    cfg = load_config(argv)
    assert not hasattr(cfg, 'crawler')
    assert not hasattr(cfg.r18dev, 'enabled')
    assert not hasattr(cfg.network, 'proxy_free')
    assert not hasattr(cfg.concurrency, 'crawlers')
    assert cfg.r18dev.required_keys == ['cover', 'title']
    assert cfg.r18dev.normalize_actress_name and cfg.r18dev.respect_source_id
    for key in ('crawler', 'r18dev.enabled', 'network.proxy_free', 'concurrency.crawlers'):
        assert key in caplog.text
    assert 'private-value' not in caplog.text and 'invalid-value' not in caplog.text
    assert 'r18dev.respect_source_id' in caplog.text


def test_local_rule_precedence_and_independent_loads(tmp_path, monkeypatch):
    from javsp.config import load_config

    path = tmp_path / 'config.yml'
    path.write_text(
        'r18dev:\n  required_keys: [title]\n  normalize_actress_name: false\n  respect_source_id: false\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('JAVSP_R18DEV__RESPECT_SOURCE_ID', 'true')
    first = load_config(['-c', str(path), '--or18dev.respect_source_id', 'false'])
    second = load_config(['-c', str(path)])
    assert not first.r18dev.respect_source_id and second.r18dev.respect_source_id
    assert first.r18dev.required_keys == ['title'] and not first.r18dev.normalize_actress_name
    monkeypatch.delenv('JAVSP_R18DEV__RESPECT_SOURCE_ID')
    assert not load_config(['-c', str(path)]).r18dev.respect_source_id
    assert first is not second


@pytest.mark.parametrize('key,value', [('required_keys', '[not_a_field]'), ('normalize_actress_name', 'invalid'),
                                       ('respect_source_id', 'invalid')])
def test_invalid_local_rules_are_rejected(tmp_path, key, value):
    from javsp.config import load_config

    path = tmp_path / 'config.yml'
    path.write_text(f'r18dev:\n  {key}: {value}\n', encoding='utf-8')
    with pytest.raises(ValueError):
        load_config(['-c', str(path)])
