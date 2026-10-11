"""CLI 进度展示和批次统计，不承载核心处理。"""

import os

from tqdm import tqdm

from javsp.workflow.models import Stage


def summarize_batch(batch, filemove_log=False):
    stats = {"total": batch.total, "success": 0, "failed": 0, "success_list": [],
             "failed_list": [], "filemove_list": [], "pending_list": [], "aborted": batch.aborted,
             "batch_error": str(batch.error) if batch.error else "", "warnings": list(batch.warnings)}
    movies = []
    for job in batch.jobs:
        movie_id = job.error.movie_id if job.error else job.movie.dvdid or job.movie.cid or "未知番号"
        if job.success:
            movies.append(job.movie)
            stats["success"] += 1
            sources = {}
            for name, crawler in (job.movie.info.field_sources or {}).items():
                sources.setdefault(crawler, []).append(name)
            stats["success_list"].append((movie_id, ", ".join(f"{c}[{len(fs)}]" for c, fs in sources.items())))
            if filemove_log:
                summary = " | 来源: " + ", ".join(f"{c}({', '.join(fs)})" for c, fs in sources.items()) if sources else ""
                stats["filemove_list"].extend((movie_id, src, dst, summary) for src, dst in job.output.moved_files)
        else:
            stats["failed"] += 1
            stats["failed_list"].append((movie_id, str(job.error)))
    stats["pending_list"] = [m.dvdid or m.cid or "未知番号" for m in batch.pending]
    return movies, stats


def print_summary(stats):
    """打印运行统计摘要"""
    total = stats["total"]
    success = stats["success"]
    failed = stats["failed"]
    width = 50
    print()
    print("=" * width)
    print("  运行统计".center(width))
    print("-" * width)
    print(f"  总计: {total}  成功: {success}  失败: {failed}")
    if stats["success_list"]:
        print("-" * width)
        print("  成功详情:")
        for movie_id, source_summary in stats["success_list"]:
            print(f"    {movie_id}")
            if source_summary:
                print(f"      {source_summary}")
    if stats["failed_list"]:
        print("-" * width)
        print("  失败详情:")
        for movie_id, reason in stats["failed_list"]:
            # 截断过长的原因
            short_reason = reason if len(reason) <= 60 else reason[:57] + "..."
            print(f"    {movie_id}: {short_reason}")
    if stats["filemove_list"]:
        print("-" * width)
        print("  文件移动详情:")
        for movie_id, src, dst, source_info in stats["filemove_list"]:
            src_rel = os.path.relpath(src)
            dst_rel = os.path.relpath(dst)
            print(f"    [{movie_id}]{source_info}")
            print(f"      {src_rel}")
            print(f"      -> {dst_rel}")
    if stats.get("pending_list"):
        print("-" * width)
        print("  未处理影片: " + ", ".join(stats["pending_list"]))
    if stats.get("batch_error"):
        print("  批次终止原因: " + stats["batch_error"])
    if stats.get("warnings"):
        print("  清理告警:")
        for warning in stats["warnings"]:
            print(f"    {warning}")
    print("=" * width)
    print()


class ProgressDisplay:
    def __init__(self):
        self.outer = None
        self.inner = None

    def review(self, movies, root):
        from javsp.startup import StartupError

        if not movies:
            raise StartupError("未找到影片文件")
        self.outer = tqdm(total=len(movies), desc="整理影片", ascii=True, leave=False)

    def on_event(self, event):
        if event.stage == Stage.CRAWL and not event.detail:
            if self.inner:
                self.inner.close()
            self.inner = tqdm(total=6, desc="步骤", ascii=True, leave=False)
        if self.inner:
            self.inner.set_description(event.detail or str(event.stage))
            if not event.detail and event.stage not in (Stage.CRAWL, Stage.ROLLBACK):
                self.inner.update()

    def on_result(self, job):
        if self.inner:
            self.inner.close()
            self.inner = None
        if self.outer:
            self.outer.update()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.inner:
            self.inner.close()
        if self.outer:
            self.outer.close()
