"""启动检查顺序与版本提醒的离线行为。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from javsp import startup


def test_startup_prepares_sources_before_model(tmp_path, monkeypatch):
    calls = []
    cfg = SimpleNamespace(
        scanner=SimpleNamespace(input_directory=tmp_path),
        r18dev=SimpleNamespace(enabled=True),
        crawler=SimpleNamespace(online_enabled=False),
        _r18dev_db_path=tmp_path / "r18dev.sqlite",
    )
    monkeypatch.setattr(startup, "check_for_updates", lambda: calls.append("version"))
    monkeypatch.setattr(startup, "load_actress_aliases", lambda config: calls.append("aliases"))
    monkeypatch.setattr(startup, "prepare_database", lambda path, online: calls.append("database") or path)
    monkeypatch.setattr(startup, "import_crawlers", lambda config: calls.append("crawlers"))

    assert startup.prepare_startup(cfg) == str(tmp_path)
    assert calls == ["version", "aliases", "database", "crawlers"]


def test_missing_scan_directory_stops_before_network(tmp_path, monkeypatch):
    cfg = SimpleNamespace(scanner=SimpleNamespace(input_directory=tmp_path / "missing"))
    monkeypatch.setattr(startup, "check_for_updates", lambda: pytest.fail("不应联网检查版本"))
    with pytest.raises(startup.StartupError, match="扫描目录"):
        startup.prepare_startup(cfg)


def test_mapping_failure_stops_before_data_sources(tmp_path, monkeypatch):
    cfg = SimpleNamespace(scanner=SimpleNamespace(input_directory=tmp_path))
    monkeypatch.setattr(startup, "check_for_updates", lambda: None)

    def missing(_cfg):
        raise startup.ActorMappingError("无有效缓存")

    monkeypatch.setattr(startup, "load_actress_aliases", missing)
    with pytest.raises(startup.StartupError, match="无有效缓存"):
        startup.prepare_startup(cfg)


def test_model_starts_after_preparation_and_closes(monkeypatch):
    calls = []
    cfg = SimpleNamespace(translator=object())
    monkeypatch.setattr(startup, "prepare_startup", lambda config: calls.append("resources") or "/media")

    @contextmanager
    def translator(_config):
        calls.append("model started")
        try:
            yield object()
        finally:
            calls.append("model closed")

    monkeypatch.setattr(startup, "LocalTranslator", translator)
    with startup.startup_context(cfg) as (root, _):
        assert root == "/media"
        calls.append("scan")
    assert calls == ["resources", "model started", "scan", "model closed"]


class ReleaseResponse:
    def __init__(self, tag):
        self.tag = tag

    def raise_for_status(self):
        pass

    def json(self):
        return {"tag_name": self.tag}


def test_update_notice_and_offline_fallback(monkeypatch, caplog):
    monkeypatch.setattr(startup, "get_version", lambda: "2.0.0.dev3")
    session = SimpleNamespace(get=lambda url, timeout: ReleaseResponse("v2.0.0"))
    startup.check_for_updates(session=session)
    assert "发现 JavSP 新版本 v2.0.0" in caplog.text

    caplog.clear()

    def unavailable(*_args, **_kwargs):
        raise startup.requests.ConnectionError("offline")

    startup.check_for_updates(session=SimpleNamespace(get=unavailable))
    assert "版本更新检查失败，继续使用当前版本" in caplog.text


def test_unknown_local_version_skips_network(monkeypatch):
    monkeypatch.setattr(startup, "get_version", lambda: "0.0.0")
    startup.check_for_updates(session=SimpleNamespace(get=lambda *_args, **_kwargs: pytest.fail("不应联网")))
