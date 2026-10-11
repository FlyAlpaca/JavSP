"""源子目录清理的转移证据、保护边界、失败与取消恢复。"""

import os
from pathlib import Path

import pytest

from javsp.cli import print_summary, summarize_batch
from javsp.config import load_config
from javsp.datatype import Movie, MovieInfo
from javsp.publish import source_cleanup, transaction
from javsp.publish.source_cleanup import SourceCleanup
from javsp.workflow.models import BatchResult, JobResult, OutputResult, ScanResult, Stage


@pytest.fixture
def scene(tmp_path):
    root = tmp_path / "src"
    folder = root / "group"
    folder.mkdir(parents=True)
    video = folder / "ABC-123.mp4"
    video.write_bytes(b"video")
    (folder / "old.nfo").write_bytes(b"old nfo")
    (folder / "unmatched.srt").write_bytes(b"subtitle")
    (folder / "notes.txt").write_bytes(b"notes")
    cfg = load_config()
    cfg = cfg.model_copy(update={
        "scanner": cfg.scanner.model_copy(update={"minimum_size": 0, "skip_nfo_dir": False}),
        "summarizer": cfg.summarizer.model_copy(update={
            "move_files": True, "path": cfg.summarizer.path.model_copy(update={"hard_link": False}),
        }),
    })
    movie = Movie("ABC-123")
    movie.files = [str(video)]
    movie.info = MovieInfo("ABC-123", title="title")
    return cfg, root, folder, movie


def committed_batch(movie, destination):
    destination.mkdir(parents=True, exist_ok=True)
    moved = []
    for file in movie.files:
        target = destination / Path(file).name
        Path(file).rename(target)
        moved.append((file, str(target)))
    movie.save_dir = str(destination)
    movie.files = [target for _, target in moved]
    return BatchResult(1, jobs=[JobResult(movie, OutputResult(movie, moved))])


def test_removes_residual_files_and_nested_directories_but_keeps_root(scene, tmp_path):
    cfg, root, folder, movie = scene
    nested = folder / "disc"
    nested.mkdir()
    video = Path(movie.files[0])
    video.rename(nested / video.name)
    movie.files = [str(nested / video.name)]
    (nested / "poster.jpg").write_bytes(b"image")
    unrelated = root / "unrelated"
    unrelated.mkdir()
    (unrelated / "notes.txt").write_bytes(b"keep")
    cleanup = SourceCleanup(root, cfg, ScanResult(movies=[movie]))
    batch = committed_batch(movie, tmp_path / "out")
    cleanup.run(batch)
    assert not folder.exists()
    assert root.is_dir() and (unrelated / "notes.txt").read_bytes() == b"keep"
    assert Path(movie.files[0]).read_bytes() == b"video"
    assert batch.exit_code == 0 and not batch.warnings
    assert not list(root.glob(".javsp-*"))


@pytest.mark.parametrize("blocker", ["existing", "skipped", "new", "removed"])
def test_preserves_directory_without_transfer_evidence_for_every_original_video(scene, tmp_path, blocker):
    cfg, root, folder, movie = scene
    extra = folder / "unknown.mp4"
    scan = ScanResult(movies=[movie])
    if blocker != "new":
        extra.write_bytes(b"unprocessed")
    if blocker == "skipped":
        scan.skipped.append(str(extra))
    cleanup = SourceCleanup(root, cfg, scan)
    batch = committed_batch(movie, tmp_path / "out")
    if blocker == "new":
        extra.write_bytes(b"new video")
    elif blocker == "removed":
        extra.unlink()
    cleanup.run(batch)
    assert folder.is_dir() and (folder / "notes.txt").read_bytes() == b"notes"
    assert not batch.warnings and batch.exit_code == 0


@pytest.mark.parametrize("protection", ["skipped_subtitle", "ignored", "recovery_dir", "recovery_file", "symlink", "mount"])
def test_preserves_protected_content(scene, tmp_path, monkeypatch, protection):
    cfg, root, folder, movie = scene
    protected = folder / "protected"
    if protection in ("ignored", "recovery_dir", "mount"):
        protected = folder / {"ignored": "#不要扫描", "recovery_dir": ".javsp-evidence", "mount": "mounted"}[protection]
        protected.mkdir()
        (protected / "evidence.txt").write_bytes(b"keep")
    elif protection == "recovery_file":
        protected = folder / "recovery.json"
        protected.write_bytes(b"keep")
    elif protection == "symlink":
        external = tmp_path / "external"
        external.mkdir()
        (external / "keep.txt").write_bytes(b"keep")
        protected.symlink_to(external, target_is_directory=True)
    if protection == "mount":
        is_mount = Path.is_mount
        monkeypatch.setattr(Path, "is_mount", lambda path: path == protected or is_mount(path))
    cleanup = SourceCleanup(root, cfg, ScanResult(movies=[movie]))
    batch = committed_batch(movie, tmp_path / "out")
    if protection == "skipped_subtitle":
        batch.jobs[0].output.skipped_files.append(str(folder / "unmatched.srt"))
    cleanup.run(batch)
    assert folder.is_dir() and (folder / "notes.txt").exists()
    if protection == "symlink":
        assert (external / "keep.txt").read_bytes() == b"keep"
    assert batch.exit_code == 0


def test_preserves_output_inside_source_directory(scene):
    cfg, root, folder, movie = scene
    cleanup = SourceCleanup(root, cfg, ScanResult(movies=[movie]))
    batch = committed_batch(movie, folder / "result")
    cleanup.run(batch)
    assert folder.is_dir() and (folder / "notes.txt").exists()
    assert Path(movie.files[0]).read_bytes() == b"video"


@pytest.mark.parametrize("mode", ["no_move", "hardlink", "aborted"])
def test_disabled_or_aborted_batch_does_not_remove_residuals(scene, tmp_path, mode):
    cfg, root, folder, movie = scene
    if mode == "no_move":
        cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"move_files": False})})
    elif mode == "hardlink":
        paths = cfg.summarizer.path.model_copy(update={"hard_link": True})
        cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"path": paths})})
    cleanup = SourceCleanup(root, cfg, ScanResult(movies=[movie]))
    batch = committed_batch(movie, tmp_path / "out")
    batch.aborted = mode == "aborted"
    cleanup.run(batch)
    assert folder.is_dir() and (folder / "notes.txt").exists()


@pytest.mark.parametrize("change", ["new_file", "changed_residual", "missing_target", "changed_target", "replaced_directory"])
def test_changed_files_or_directories_prevent_cleanup(scene, tmp_path, change):
    cfg, root, folder, movie = scene
    cleanup = SourceCleanup(root, cfg, ScanResult(movies=[movie]))
    batch = committed_batch(movie, tmp_path / "out")
    if change == "new_file":
        (folder / "new.txt").write_bytes(b"keep new")
    elif change == "changed_residual":
        (folder / "notes.txt").write_bytes(b"changed")
    elif change == "missing_target":
        Path(movie.files[0]).unlink()
    elif change == "changed_target":
        Path(movie.files[0]).write_bytes(b"bad")
    else:
        folder.rename(root / "original")
        folder.mkdir()
        (folder / "notes.txt").write_bytes(b"replacement")
    cleanup.run(batch)
    assert folder.is_dir() and (folder / "notes.txt").exists()


@pytest.mark.parametrize("failure", ["staging", "cancel", "rollback", "new_video"])
def test_staging_failure_restores_residuals_and_stops_on_incomplete_recovery(scene, tmp_path, monkeypatch, failure):
    cfg, root, folder, movie = scene
    cleanup = SourceCleanup(root, cfg, ScanResult(movies=[movie]))
    batch = committed_batch(movie, tmp_path / "out")
    originals = {path: path.read_bytes() for path in folder.iterdir()}
    move = transaction.FileTransaction.move
    calls = []

    def fail(tx, operation):
        move(tx, operation)
        calls.append(operation)
        if len(calls) == 1:
            if failure == "cancel":
                raise KeyboardInterrupt("cancel cleanup")
            if failure == "rollback":
                Path(operation.source).write_bytes(b"external replacement")
                raise OSError("stage failed")
            if failure == "new_video":
                (folder / "new.mp4").write_bytes(b"new video")
            else:
                raise OSError("stage failed")

    monkeypatch.setattr(transaction.FileTransaction, "move", fail)
    cleanup.run(batch)
    assert batch.jobs[0].success and folder.is_dir()
    assert Path(movie.files[0]).read_bytes() == b"video"
    if failure == "rollback":
        assert batch.aborted and not batch.error.recovered and batch.exit_code == 1
        assert batch.error.stage == Stage.CLEANUP
        manifests = list(root.glob(".javsp-*/recovery.json"))
        assert manifests and Path(calls[0].target).exists()
    else:
        assert all(path.read_bytes() == contents for path, contents in originals.items())
        assert not list(root.glob(".javsp-*"))
        assert batch.aborted == (failure == "cancel")
        assert batch.exit_code == int(failure == "cancel")
        if failure == "new_video":
            assert (folder / "new.mp4").read_bytes() == b"new video"
        if failure != "cancel":
            assert batch.warnings


@pytest.mark.parametrize("failure", ["unlink_workspace", "rmdir", "unreadable"])
def test_cleanup_errors_warn_without_changing_success(scene, tmp_path, monkeypatch, capsys, failure):
    cfg, root, folder, movie = scene
    cleanup = SourceCleanup(root, cfg, ScanResult(movies=[movie]))
    batch = committed_batch(movie, tmp_path / "out")
    if failure == "unlink_workspace":
        def fail(path):
            raise PermissionError("cannot purge workspace")
        monkeypatch.setattr(transaction.shutil, "rmtree", fail)
    elif failure == "rmdir":
        rmdir = os.rmdir
        def fail(path, *args, **kwargs):
            if Path(path) == folder:
                raise PermissionError("cannot delete source directory")
            return rmdir(path, *args, **kwargs)
        monkeypatch.setattr(source_cleanup.os, "rmdir", fail)
    else:
        scandir = os.scandir
        def fail(path):
            if Path(path) == folder:
                raise PermissionError("cannot list source directory")
            return scandir(path)
        monkeypatch.setattr(source_cleanup.os, "scandir", fail)
    cleanup.run(batch)
    assert batch.warnings and batch.jobs[0].success and batch.exit_code == 0
    assert folder.is_dir() and root.is_dir()
    if failure == "unlink_workspace":
        assert list(root.glob(".javsp-*/recovery.json"))
    _, stats = summarize_batch(batch)
    print_summary(stats)
    assert "清理告警" in capsys.readouterr().out


def test_cancel_during_purge_reports_retained_workspace_and_keeps_committed_movie(scene, tmp_path, monkeypatch):
    cfg, root, folder, movie = scene
    cleanup = SourceCleanup(root, cfg, ScanResult(movies=[movie]))
    batch = committed_batch(movie, tmp_path / "out")
    def cancel(tx, remove_dirs=False):
        # 模拟删除阶段已删掉一个暂存文件后收到取消。
        Path(tx.entries[0].target).unlink()
        raise KeyboardInterrupt("cancel during purge")
    monkeypatch.setattr(transaction.FileTransaction, "cleanup", cancel)
    cleanup.run(batch)
    assert batch.aborted and batch.exit_code == 1 and batch.jobs[0].success
    assert Path(movie.files[0]).read_bytes() == b"video" and folder.is_dir()
    manifests = list(root.glob(".javsp-*/recovery.json"))
    assert manifests and str(manifests[0].parent) in batch.warnings[0]
    assert "部分暂存文件可能已经删除" in batch.warnings[0]


def test_target_modified_during_staging_restores_residuals(scene, tmp_path, monkeypatch):
    cfg, root, folder, movie = scene
    cleanup = SourceCleanup(root, cfg, ScanResult(movies=[movie]))
    batch = committed_batch(movie, tmp_path / "out")
    originals = {path: path.read_bytes() for path in folder.iterdir()}
    move = transaction.FileTransaction.move
    def change_target(tx, operation):
        move(tx, operation)
        Path(movie.files[0]).write_bytes(b"other")
    monkeypatch.setattr(transaction.FileTransaction, "move", change_target)
    cleanup.run(batch)
    assert batch.warnings and not batch.aborted
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert Path(movie.files[0]).read_bytes() == b"other"
    assert not list(root.glob(".javsp-*"))
