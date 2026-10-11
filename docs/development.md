# 开发指南

## 目录

| 路径 | 用途 |
| --- | --- |
| `javsp/` | 容器命令入口、配置、基础数据类型和公共辅助模块 |
| `javsp/workflow/` | 流水线编排、阶段事件与结果契约 |
| `javsp/scan/` | 番号识别与本地文件扫描 |
| `javsp/crawl/` | 抓取调度与 R18.dev 本地数据源 |
| `javsp/web/` | 动态加载的站点抓取器、网络请求和翻译 |
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

`javsp/workflow/models.py` 定义扫描、抓取、阶段事件、输出及单片/批次结果；`javsp/workflow/pipeline.py` 负责逐部影片编排。
核心组件显式接收配置，CLI 将事件转成进度条。

代码从上述职责目录导入模块；分层前的根目录模块路径不再提供。

| 组件 | 接口 | 边界 |
| --- | --- | --- |
| Scanner | `scan(root) -> ScanResult` | 独立返回影片、无法识别、重复和跳过条目 |
| CrawlerExecutor | `crawl(movie, progress=None) -> CrawlResult` | 站点并发、重试、软截止、线程回收；返回最终数据源身份 |
| MetadataMerger | `merge(movie, result) -> MovieInfo` | 分类归一化、字段优先级、必需字段、演员别名及来源；不修改输入来源 |
| OutputPlanner | `plan(movie, info) -> OutputPlan` | 在影片副本上只读规划路径与影片/字幕操作 |
| OutputExecutor | `execute(plan) -> OutputResult` | 暂存、校验、提交、备份与逆序回滚 |
| JobRunner / Pipeline | `run(movie)` / `run(root)` | 任务状态、阶段错误、批次停止及未处理列表 |

翻译位于合并与规划之间。现有列表/字典接口通过适配器继续使用，包括 `scan_movies`、`parallel_crawler`、
`info_summary`、`generate_names`、`Movie.rename_files` 和 `RunNormalMode`，实际处理复用新实现。

### 输出事务与恢复边界

临时产物与备份创建在对应目标文件系统中的 `.javsp-*` 目录。提交前检查文件状态；普通文件移动使用拒绝已有目标的重命名，
缺少该系统接口的平台使用硬链接发布后删除源名称；跨设备时先复制并验证大小，再发布和删除源文件。硬链接失败回退复制。元数据替换前备份旧文件，回滚按反向操作恢复。
只有整片提交成功后才发布影片结果、写正式移动日志和清理空源目录。

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
python tools/call_crawler.py ABC-123
python tools/check_genre.py
python tools/version.py
```

工具按自身文件位置定位仓库；从其他目录调用时可使用脚本的绝对路径。
抓取样本写入 `tests/data/`；分类表写入 `data/`，更新后应人工检查。

## 构建与清理

Docker 构建上下文排除测试、开发文档、虚拟环境、缓存和本地构建产物。
wheel 保留运行时资源，避免移动 `data/`、`image/` 或默认配置导致资源查找失败。
`__pycache__/`、`.pytest_cache/`、`.ruff_cache/` 均为可重新生成的缓存，不应提交。


## 版本与发布

版本由 Git tag 和 Hatch VCS 驱动。当前稳定版使用中文注释标签 `v2.0.2`；wheel/sdist 和安装元数据版本为 `2.0.2`。
发布前通过 Ruff、全部离线单元测试和构建，确认工作区只包含任务修改，再提交并在最终提交创建标签。
更新 tag 后使用 `uv sync --locked --extra dev --reinstall-package javsp` 刷新本地安装元数据；
`uv run --locked python tools/version.py`、运行时版本和 wheel METADATA 应一致。

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
