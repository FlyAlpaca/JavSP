"""配置加载与导入隔离，不依赖外部网络。"""

import subprocess
import sys

from javsp.config import load_config


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
sys.argv = ['javsp', '--config', '/missing/config.yml']
def forbidden(*args, **kwargs):
    raise AssertionError('import must not construct a network session')
requests.Session = forbidden
import javsp.config
import javsp.crawl.dispatcher
import javsp.network
import javsp.startup
'''], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
