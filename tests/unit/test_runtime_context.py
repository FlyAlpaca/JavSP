"""配置加载与站点运行状态隔离，不依赖外部网络。"""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from javsp.config import load_config
from javsp.web import context as context_module
from javsp.web.context import SiteContext


def test_configuration_precedence_and_independent_loads(tmp_path, monkeypatch):
    path = tmp_path / 'config.yml'
    path.write_text('network:\n  retry: 1\n', encoding='utf-8')
    monkeypatch.setenv('JAVSP_NETWORK__RETRY', '2')
    first = load_config(['--config', str(path), '--onetwork.retry', '3'])
    second = load_config(['--config', str(path)])
    assert first.network.retry == 3
    assert second.network.retry == 2
    assert first is not second
    monkeypatch.delenv('JAVSP_NETWORK__RETRY')
    assert load_config(['--config', str(path)]).network.retry == 1


def test_import_does_not_load_configuration_or_create_sessions():
    result = subprocess.run(
        [sys.executable, '-c', '''
import sys
import requests
import curl_cffi.requests
sys.argv = ['javsp', '--config', '/missing/config.yml']
def forbidden(*args, **kwargs):
    raise AssertionError('import must not construct a network session')
requests.Session = forbidden
curl_cffi.requests.Session = forbidden
import javsp.config
import javsp.crawl.dispatcher
from pathlib import Path
import importlib
for module in Path('javsp/web').glob('*.py'):
    importlib.import_module('javsp.web.' + module.stem)
'''], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_site_context_isolation_and_cleanup(monkeypatch):
    clients = []

    class Client:
        def __init__(self, network, **kwargs):
            self.network = network
            self.headers, self.cookies = {}, {}
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(context_module, 'Request', Client)
    cfg = load_config([])
    other = cfg.model_copy(update={'network': cfg.network.model_copy(update={'retry': 7})})
    with SiteContext(cfg, 'javdb') as first, SiteContext(other, 'javdb') as second:
        first.request.cookies['login'] = 'first'
        first.cookies_pool = [{'cookies': {}}]
        first.base_url = 'https://changed.test'
        assert second.request.cookies == {}
        assert second.cookies_pool is None
        assert second.base_url != first.base_url
        assert first.request.network is cfg.network
        assert second.request.network is other.network
        previous = first.request
        first.reset_request()
        assert previous.closed
        assert 'Accept-Language' in first.request.headers
    assert all(client.closed for client in clients)


def test_context_closes_on_parser_error(monkeypatch):
    client = SimpleNamespace(headers={}, cookies={}, close=lambda: closed.append(True))
    closed = []
    monkeypatch.setattr(context_module, 'Request', lambda *args, **kw: client)
    with pytest.raises(ValueError):
        with SiteContext(load_config([]), 'javbus'):
            raise ValueError('parser failed')
    assert closed == [True]
