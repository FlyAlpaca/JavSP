"""旧模块路径与按职责分层后的实现保持同一模块身份。"""

from importlib import import_module

import pytest


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("avid", "scan.avid"),
        ("file", "scan.file"),
        ("dispatcher", "crawl.dispatcher"),
        ("r18dev", "crawl.r18dev"),
        ("summarizer", "metadata.summarizer"),
        ("image", "publish.image"),
        ("nfo", "publish.nfo"),
        ("processor", "publish.processor"),
        ("output", "publish.output"),
        ("models", "workflow.models"),
        ("pipeline", "workflow.pipeline"),
    ],
)
def test_legacy_module_is_canonical_module(legacy, canonical, monkeypatch):
    new_module = import_module(f"javsp.{canonical}")
    old_module = import_module(f"javsp.{legacy}")
    assert old_module is new_module

    marker = object()
    monkeypatch.setattr(old_module, "_layout_test_marker", marker, raising=False)
    assert getattr(new_module, "_layout_test_marker") is marker
