# 开发指南

## 目录

| 路径 | 用途 |
| --- | --- |
| `javsp/` | 容器命令入口、配置、基础数据类型和公共辅助模块 |
| `javsp/workflow/` | 流水线编排、阶段事件与结果契约 |
| `javsp/scan/` | 番号识别与本地文件扫描 |
| `javsp/crawl/` | 抓取调度与 R18.dev 本地数据源 |
| `javsp/web/` | 动态加载的站点抓取器与网络请求 |
| `javsp/metadata/` | 来源合并、字段选择与元数据整理 |
| `javsp/publish/` | 路径规划、图片及 NFO 生成、文件提交和恢复 |
| `javsp/cropper/` | 海报裁剪后端 |
| `data/` | 运行时分类映射和演员别名，随安装包发布 |
| `image/` | 运行时水印 |
| `tests/unit/` | 本地测试，网络请求由 mock 或本地 HTTP 服务提供 |
| `tests/integration/` | 在线站点测试 |
| `tests/data/` | JSON 样本及番号识别数据，不随 wheel 发布 |
| `tools/` | 抓取样本、更新分类表和查询版本的维护脚本 |
| `docker/` | 容器构建和入口脚本 |
| `docs/` | 开发文档 |

`pyproject.toml` 管理依赖、wheel/sdist 构建及检查配置，`uv.lock` 锁定依赖。
运行产物仅为 Docker 镜像；本地 Python 环境只用于开发和测试。
`config_default.yml` 是随包发布的默认配置；用户的 `config.yml` 放在运行目录。

## 2.0 流水线

`javsp/workflow/models.py` 定义扫描、抓取、阶段事件、输出及单片/批次结果；`javsp/workflow/pipeline.py` 负责单片编排，`javsp/workflow/batch.py` 负责有界并发批次调度。
核心组件显式接收配置，CLI 将事件转成进度条。

代码从上述职责目录导入模块；分层前的根目录模块路径不再提供。

| 组件 | 接口 | 边界 |
| --- | --- | --- |
| Scanner | `scan(root) -> ScanResult` | 独立返回影片、无法识别、重复和跳过条目 |
| CrawlerExecutor | `crawl(movie, progress=None) -> CrawlResult` | 站点并发、重试、软截止、线程回收；返回最终数据源身份 |
| MetadataMerger | `merge(movie, result) -> MovieInfo` | 分类归一化、字段优先级、必需字段、演员别名及来源；不修改输入来源 |
| OutputPlanner | `plan(movie, info) -> OutputPlan` | 在影片副本上只读规划路径与影片/字幕操作 |
| OutputExecutor | `prepare(plan)` / `commit(prepared)` / `discard(prepared)`；`execute(plan)` 组合入口 | 暂存、校验、提交、备份与逆序回滚 |
| JobRunner / Pipeline | `run(movie)` / `run(root)` | 任务状态、阶段错误、批次停止及未处理列表 |

翻译位于合并与规划之间。CLI 通过 `Pipeline.run()` 调用核心处理，进度和统计展示位于 `javsp/cli.py`。
运行上下文统一管理翻译器，流水线和单片任务只使用已准备的翻译器，不启动或关闭模型服务。

### 批次并发与资源生命周期

`concurrency.movies/crawlers/images` 默认分别为 3/6/3，模型并发沿用 `translator.runtime.parallel`。
`BatchResources` 显式传入单片组件，持有批次共享抓取池、图片池、停止信号、站点冷却与剧照启动节流。
影片线程池与子任务池独立，最多保持影片额度内的任务；准备完成但尚未提交的影片也占用额度。
剧照按有界窗口派发，完成任务的 Future 从资源登记中移除，避免整批任务历史持续累积。

每部影片独立创建抓取器、合并器、规划器和输出执行器；R18.dev 查询各自打开只读连接，启动构建的演员索引只读共享。
站点身份显式传入任务，不依赖线程池工作线程名称；同一站点串行执行并在任务完成后冷却。
抓取截止从获得站点额度并实际开始任务时计算，超时仍等待请求结束并舍弃晚到结果。
`movies: 1` 使用串行编排与成功影片之间的节流，子任务池仍共享并限制线程数。
独立 `JobRunner` 注入自定义处理组件时沿用串行批次行为，避免复制或跨线程共享外部组件的可变状态。

`PreparedOutput` 持有规划副本、文件事务、暂存产物与目标状态；准备阶段不移动影片、不替换正式产物。
主调度线程按输入顺序调用提交，提交前复查文件状态，并记录已提交目标以拒绝批次内的目标重叠。
直接调用 `execute()` 保留原有组合行为；已提交或放弃的准备结果不能再次提交。

工作线程将 `StageEvent` 放入队列；批次主线程调用事件与结果回调。新增的可选 `task_id` 标识单片任务，
`JobResult.task_id` 与事件对应；最终 `BatchResult.jobs` 按输入顺序排列。
普通准备或提交失败形成失败结果，恢复完整时继续；停止信号引起的未提交任务进入 `pending`。
任何影片取消或恢复不完整都会通知调度器停止，已开始的提交允许完成或按原事务规则回滚，不再启动后续提交。
先等待影片线程及其子任务结束，再关闭共享池、清理准备结果，最后退出启动上下文并关闭模型服务。

本地翻译请求按模型槽位限流，每个请求使用独立 HTTP 会话并在请求结束后关闭；传入外部测试会话时串行保护。
默认共享一个模型进程，Slimeface 调用通过锁串行执行，水印图在启动时完成像素加载后只读使用。

### 输出事务与恢复边界

临时产物与备份创建在对应目标文件系统中的 `.javsp-*` 目录。提交前检查文件状态；普通文件移动使用拒绝已有目标的重命名，
缺少该系统接口的平台使用硬链接发布后删除源名称；跨设备时先复制并验证大小，再发布和删除源文件。硬链接失败回退复制。元数据替换前备份旧文件，回滚按反向操作恢复。
只有整片提交成功后才发布影片结果和写正式移动日志。执行器直接调用时保留提交后清理直接空源目录的行为；
`Pipeline.run()` 使用 `cleanup_empty_dirs=False`，统一在批次结束后清理源子目录，始终保留扫描根目录。

`publish.source_cleanup.SourceCleanup` 在任务执行前记录候选源目录及文件状态，批次结束后按 `OutputResult.moved_files`
核对所有原有影片的转移结果。影片范围沿用扫描配置后缀，且不应用大小、番号或重复过滤；无转移证据的影片即使已被外部删除，
也会阻止目录清理。清理前复核目录身份、残留文件状态、目标影片存在及大小，选取互不重叠的最上层合格目录。
普通单片失败不阻止其他合格目录清理；关闭移动、硬链接或批次中断时不清理。

残留 NFO、图片、文本和普通字幕通过 `FileTransaction` 暂存到扫描根目录内、候选目录外的 `.javsp-*` 工作区。
确认暂存完成、源目录没有新增内容且影片目标仍未变化后，删除暂存文件，再自底向上使用 `rmdir` 删除空目录。
暂存失败时逆序恢复，恢复不完整时保留清单并停止清理；删除阶段失败保留尚存工作区及清单，可能已有部分残留文件被删除。
清理告警记录在 `BatchResult.warnings` 并显示于 CLI，不把已提交影片改判为失败；取消或恢复不完整仍使批次退出码为 1。
`OutputResult.skipped_files` 记录跳过的源字幕，防止其随目录被删除。
输出、扫描排除目录、符号链接、其他挂载点、`.javsp-*` 和 `recovery.json` 都阻止相关目录清理。

回滚发现外部修改或恢复操作失败时保留可恢复文件及 `recovery.json`，任务错误标记为未恢复，批次停止。
清单包含操作路径、备份、文件状态和恢复错误，供人工处理；已完成回滚的操作会更新状态。
恢复支持运行期间异常和取消，不保证多文件同时可见，不实现断电、kill 后的启动恢复。

普通失败且恢复成功时继续；取消或恢复不完整时保留未处理影片列表。节流被取消时，上一部已成功提交的任务仍为成功。
组件测试使用本地媒体、图片与模拟抓取器，覆盖跨设备复制、替换失败、取消和回滚冲突。
工作线程与下载池在取消时仍等待实际任务结束；被中断的 join/shutdown 不会让后台写入与临时目录清理重叠。

## 环境与本地检查

在仓库根目录执行：

```bash
uv sync --locked --extra dev
uv run --locked --extra dev ruff check .
uv run --locked --extra dev pytest
uv build
```

默认运行本地测试，并跳过集成测试。也可单独运行 `pytest tests/unit`。

GitHub Actions 的 `Tests` 工作流在分支推送及 PR 时运行 Python 3.11、3.14 的 Ruff 检查和全部离线单元测试，
使用 `uv.lock` 锁定依赖。Docker 发布工作流（标签推送及手动发布）复用同一测试工作流；
全部检查通过后才允许构建和推送镜像。若需限制 PR 合并，仓库管理员还需在分支保护中将测试检查设为必需。

## 集成测试

```bash
# 仅运行指定站点的在线样本比较
uv run --locked --extra dev pytest tests/integration/test_crawlers.py --run-integration --only javbus

# 测试免代理域名发现
uv run --locked --extra dev pytest tests/integration/test_proxyfree.py --run-integration
```

集成测试依赖外部站点或凭据。
在线抓取器测试在本地比较失败时仍会更新对应 JSON 样本，运行后应检查 Git 差异。
各站点的样本即使内容相同，也保留独立文件以覆盖各自抓取器。

## 维护工具

```bash
uv run --locked --extra dev python tools/call_crawler.py ABC-123
uv run --locked --extra dev python tools/check_genre.py
uv run --locked --extra dev python tools/version.py
```

工具按自身文件位置定位仓库；从其他目录调用时可使用脚本的绝对路径。
抓取样本写入 `tests/data/`；分类表写入 `data/`，更新后应人工检查。

## 构建与清理

Docker 构建上下文排除测试、开发文档、虚拟环境、缓存和本地构建产物。
wheel 保留运行时资源，避免移动 `data/`、`image/` 或默认配置导致资源查找失败。
`__pycache__/`、`.pytest_cache/`、`.ruff_cache/` 均为可重新生成的缓存，不应提交。


## 版本与发布

版本由 Git tag 和 Hatch VCS 驱动。稳定发布使用中文注释标签；wheel/sdist 和安装元数据版本应与待发布标签一致。
发布前通过 Ruff、全部离线单元测试和构建，确认工作区只包含任务修改，再提交并在最终提交创建标签。
更新 tag 后使用 `uv sync --locked --extra dev --reinstall-package javsp` 刷新本地安装元数据；
`uv run --locked --extra dev python tools/version.py`、运行时版本和 wheel METADATA 应一致。

无 Git 元数据的 Docker 构建由 `SETUPTOOLS_SCM_PRETEND_VERSION` 传入版本。Docker 工作流验证三段或四段稳定版本标签，标签推送生成版本号和 `latest`，
手动触发只生成 `dev`。推送标签会触发发布；本地创建标签不会发布镜像。

## 配置与构建数据边界

用户配置不存在时使用默认值，空文件不覆盖默认值；已发现或显式指定的配置不可读、格式错误或顶层不是映射时终止加载。
`scanner.ignored_folder_name_pattern: []` 表示不按目录名过滤。
源码包采用明确文件范围；数据库、模型、日志、分片和 `.javsp-*` 恢复材料不会进入交付产物或 Docker 上下文。
构建检查同时验证 wheel 与 sdist；请对本次新建的独立输出目录运行 `tools/check_dist.py`，避免旧产物混入验证。

## 显式配置与运行上下文

入口通过 `load_config()` 加载配置；`Cfg` 只定义冻结模型，导入时不读取用户文件、不解析命令行。
各次加载独立，覆盖顺序为默认模板、用户文件、环境变量、CLI 参数。
`RuntimeContext` 持有单批次的绝对根路径、数据库位置、演员映射和翻译器，配置对象不再保存运行资源。
独立组件通过参数接收配置；需要演员信息归一化时，向 `MetadataMerger` 显式传入映射或运行上下文。
站点解析接口为 `parse_data(info, *, context)`，清洗接口同样接收上下文；调用者使用 `with SiteContext(cfg, site)` 管理会话。
每个站点任务拥有独立请求头、Cookie、动态域名和登录候选，重试复用会话，任务结束后关闭。
站点模块不再提供直接执行示例；手动抓取使用 `tools/call_crawler.py`。


## 内部接口迁移

配置键、Docker 挂载和批处理退出规则保持既有含义；内部 Python 接口采用显式配置与结构化结果。

| 原调用 | 新调用 |
| --- | --- |
| `Cfg()` | `load_config()`，或用完整数据构造 `Cfg` |
| `scan_movies(root)` / 全局失败列表 | `Scanner(cfg).scan(root)` 的 `movies` / `unrecognized` |
| `parallel_crawler(movie)` | `CrawlerExecutor(cfg, runtime=runtime).crawl(movie)` |
| `info_summary(movie, sources)` | `MetadataMerger(cfg, runtime=runtime).merge(movie, result)` |
| `Movie.rename_files(hardlink)` | `OutputExecutor(cfg).execute_files(movie, hardlink)`，从结果读取路径 |
| `RunNormalMode` / 旧单片包装 | `Pipeline.run()` / `JobRunner.run()`，显式传入翻译器或运行上下文 |
| `BatchResult.legacy()` | CLI 层 `summarize_batch(batch)` |
| `publish.processor` | `publish.naming` 与 `publish.media` |
| `publish.output.OutputPlanner` / `RecoveryError` | `publish.planner.OutputPlanner` / `publish.transaction.RecoveryError` |

输出规划在副本上执行；相对输出模板以扫描根目录为基准，命名阶段在校验长度前解析成绝对目录。
核心处理不切换进程工作目录。独立使用规划器可传入 `root`；独立任务应传入准备好的翻译器，资源的关闭由调用者负责。
`startup_context(cfg)` 返回单个 `RuntimeContext`，包含 `root`、数据库、演员映射及翻译器；退出上下文时关闭翻译服务。
文件事务集中于 `publish.transaction`，输出执行器只协调暂存、校验、整片提交及提交后清理。
