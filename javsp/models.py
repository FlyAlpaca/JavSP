"""流水线输入、阶段事件及结果契约，不包含用户界面或文件副作用。"""

from dataclasses import dataclass, field
from enum import StrEnum

from javsp.datatype import Movie, MovieInfo


class Stage(StrEnum):
    SCAN = "扫描影片"
    CRAWL = "抓取数据"
    MERGE = "汇总数据"
    TRANSLATE = "翻译影片信息"
    PLAN = "规划输出"
    PREPARE = "准备产物"
    COMMIT = "提交产物"
    ROLLBACK = "恢复文件"
    DONE = "整理完成"


@dataclass
class ScanResult:
    movies: list[Movie] = field(default_factory=list)
    unrecognized: list[Movie] = field(default_factory=list)
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


@dataclass
class CrawlResult:
    data: dict[str, MovieInfo]
    failures: list[tuple[str, str]]
    dvdid: str | None
    cid: str | None
    data_src: str

    def require_data(self):
        if not self.data:
            failures = "\n".join(f"  - {name}: {reason}" for name, reason in self.failures)
            raise ValueError(f"所有抓取器均失败 ({self.dvdid or self.cid}):\n{failures}")


@dataclass(frozen=True)
class StageEvent:
    movie_id: str
    stage: Stage
    detail: str = ""


@dataclass
class OutputResult:
    movie: Movie
    moved_files: list[tuple[str, str]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class JobError:
    movie_id: str
    stage: Stage
    cause: BaseException
    recovered: bool = True
    recovery_paths: list[str] = field(default_factory=list)

    def __str__(self):
        return f"[{self.movie_id}] {self.stage}: {self.cause}"


@dataclass
class JobResult:
    movie: Movie
    output: OutputResult | None = None
    error: JobError | None = None
    cancelled: bool = False

    @property
    def success(self) -> bool:
        return self.error is None and self.output is not None


@dataclass
class BatchResult:
    total: int
    jobs: list[JobResult] = field(default_factory=list)
    pending: list[Movie] = field(default_factory=list)
    aborted: bool = False
    scan: ScanResult | None = None

    @property
    def exit_code(self) -> int:
        return int(self.aborted or any(not job.success for job in self.jobs))

    def legacy(self, filemove_log=False):
        stats = {"total": self.total, "success": 0, "failed": 0, "success_list": [],
                 "failed_list": [], "filemove_list": [], "pending_list": [], "aborted": self.aborted}
        movies = []
        for job in self.jobs:
            movie_id = job.movie.dvdid or job.movie.cid or "未知番号"
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
        stats["pending_list"] = [m.dvdid or m.cid or "未知番号" for m in self.pending]
        return movies, stats
