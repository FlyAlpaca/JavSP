"""版本来源和三段/四段稳定镜像标签契约。"""

import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

version = importlib.import_module("javsp.__version__")


def test_installed_metadata_is_authoritative(monkeypatch):
    monkeypatch.setattr(version.meta, "version", lambda name: "2.0.0")
    assert version.get_version() == "2.0.0"


@pytest.mark.parametrize("tag", ["v2.0.0", "2.0.0"])
def test_git_tag_fallback(monkeypatch, tag):
    monkeypatch.setattr(version.meta, "version", lambda name: "0.0.0")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=tag + "\n"))
    assert version.get_version() == "2.0.0"


def test_frozen_version_file_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(version.meta, "version", lambda name: "0.0.0")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(version, "__file__", str(tmp_path / "__version__.py"))
    (tmp_path / "VERSION").write_text("2.0.0\n")
    assert version.get_version() == "2.0.0"


def test_release_workflow_matches_both_version_formats():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/docker-build.yml").read_text())
    steps = workflow["jobs"]["build-and-push"]["steps"]
    metadata_steps = [step for step in steps if step.get("uses", "").startswith("docker/metadata-action@")]
    assert len(metadata_steps) == 2
    for step in metadata_steps:
        tags = step["with"]["tags"]
        match = next(line for line in tags.splitlines() if line.startswith("type=match,"))
        pattern = match.split("pattern=", 1)[1].split(",group=", 1)[0]
        for tag, expected in (("v2.0.0", "2.0.0"), ("v1.10.2.1", "1.10.2.1")):
            assert re.fullmatch(pattern, tag).group(1) == expected
        assert not re.fullmatch(pattern, "v2.0.0-beta")
        assert "type=raw,value=latest,enable=${{ github.event_name == 'push' }}" in tags
        assert "type=raw,value=dev,enable=${{ github.event_name == 'workflow_dispatch' }}" in tags
        assert step["with"]["flavor"] == "latest=false"


@pytest.mark.parametrize("tag, accepted", [("v2.0.0", True), ("v1.10.2.1", True), ("v2.0.0-beta", False), ("v2.0", False)])
def test_release_shell_validates_stable_tag(tmp_path, tag, accepted):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/docker-build.yml").read_text())
    step = next(step for step in workflow["jobs"]["build-and-push"]["steps"] if step.get("id") == "version")
    script = step["run"].replace("${{ github.ref_type }}", "tag").replace("${{ github.ref_name }}", tag)
    result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                            env={**os.environ, "GITHUB_OUTPUT": str(tmp_path / "output")})
    assert (result.returncode == 0) == accepted
    if accepted:
        assert (tmp_path / "output").read_text().strip() == f"version={tag[1:]}"
