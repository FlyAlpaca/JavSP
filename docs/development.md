# 开发指南

## 目录

| 路径 | 用途 |
| --- | --- |
| `javsp/` | 容器命令入口、配置、基础数据类型和公共辅助模块 |
| `javsp/workflow/` | 流水线编排、阶段事件与结果契约 |
| `javsp/scan/` | 番号识别与本地文件扫描 |
| `javsp/crawl/` | R18.dev 本地查询与数据库准备 |
| `javsp/network.py` | 通用图片请求与下载 |
| `javsp/metadata/` | 来源合并、字段选择与元数据整理 |
| `javsp/publish/` | 路径规划、图片及 NFO 生成、文件提交和恢复 |
| `javsp/cropper/` | 海报裁剪后端 |
| `image/` | 运行时水印 |
| `tests/unit/` | 本地测试，网络请求由 mock 或本地 HTTP 服务提供 |
| `tests/data/` | 番号识别样本，不随 wheel 发布 |
| `tools/` | 版本查询和构建检查脚本 |
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
| CrawlerExecutor | `crawl(movie, progress=None) -> CrawlResult` | 仅查询 R18.dev 本地库；返回元数据与失败原因 |
| MetadataMerger | `merge(movie, result) -> MovieInfo` | 本地字段整理、必需字段、演员别名及来源；不修改输入来源 |
| OutputPlanner | `plan(movie, info) -> OutputPlan` | 在影片副本上只读规划路径与影片/字幕操作 |
| OutputExecutor | `prepare(plan)` / `commit(prepared)` / `discard(prepared)`；`execute(plan)` 组合入口 | 暂存、校验、提交、备份与逆序回滚 |
| JobRunner / Pipeline | `run(movie)` / `run(root)` | 任务状态、阶段错误、批次停止及未处理列表 |

翻译位于合并与规划之间。CLI 通过 `Pipeline.run()` 调用核心处理，进度和统计展示位于 `javsp/cli.py`。
运行上下文统一管理翻译器，流水线和单片任务只使用已准备的翻译器，不启动或关闭模型服务。

### 番号与补零规则

显示番号、匹配 key 和数据库查询候选分别处理，公共逻辑位于 `scan.avid`：

- `get_id()` 保留明确带分隔符的数字位数，以及特殊番号的既有识别规则。普通紧凑五位 `00xxx` 写法转换为至少三位，
  例如 `ure00013` → `URE-013`、`xvsr00060` → `XVSR-060`；四位或五位非 `00` 开头不删零。
  这是兼容 CID 文件名的启发式，不能保证从任意 CID 推导厂家番号；超出五位的普通数字不截断匹配。
- `normalize_id()` 只为普通英字前缀加纯数字番号生成忽略前导零的匹配 key，用于字幕匹配；不用于输出命名。
  FC2、日期式无码番号、HEYDOUGA、HEYZO、东热和带数字前缀/字母后缀的特殊体系不参与普通补零转换。
- `dvd_id_candidates()` 返回去掉连字符和空白后的精确写法，以及普通数字的有限补零候选（一至五位，去重）。
  R18.dev 查询沿用现有 `video_dvd` 表达式索引，先精确查询，再一次查询其他等价写法。
  精确记录优先；同等级命中多条时按 `content_id` 升序取第一条，选择规则见下文。
- `cid_candidates()` 保留原有未补零、五位补零优先级，再补充原数字和二至四位候选；调用方可提供本地库实际观测的厂商前缀。
  CID 记录的非空 DVD 番号必须在 DVD 候选中。用户指定的完整 CID 始终按原值精确查询。
  命中后的输出番号取数据库的非空 `dvd_id`；CID-only 记录保留输入 DVD 番号。

研究依据：[R18.dev 首页](https://r18.dev/) 的搜索框直接给出 `URE-013 / ure00013`；
[h.m.p 官方目录](https://www.hmp.jp/portal/catalog/goods/JH-HODV-20260/) 给出 `JH-HODV-20260`，
说明厂家番号也存在五位数字。它们支持区分 DVD 番号和 CID，但并未定义覆盖全部厂家的通用补零标准。
查询候选是兼容策略，只有实际数据记录才能确定匹配；不能把候选位数当作厂家规则表。

### R18.dev 查询与选择规则

本地查询按以下优先级选取一条记录：

1. 指定完整 CID 时只精确查询 CID；指定 DVD 番号时先精确查询规范化后的 DVD 编号。
2. DVD 未命中时查询仅前导零不同的 DVD 写法。
3. 仍未命中时，一次合并查询所有 CID 候选，并按候选优先级取第一条。原有无厂商前缀候选优先，
   随后按厂商前缀字典序尝试实际观测的前缀；每个前缀先五位、再三位补零，最后尝试其余兼容写法。
   候选 CID 记录的非空 DVD 编号不一致时仍拒绝使用。

同等级 DVD 匹配按照 `content_id` 的 SQLite 默认字典序升序选一条；完全相同的 CID 重复行按 `rowid` 取第一条。
不按导入顺序选择不同 CID，不按发行日期或字段数量推测优先级，也不混合多条记录的字段。
这一规则借鉴 [Javinizer-Go v1.6.1 的 R18.dev 转储查询源码](https://github.com/javinizer/javinizer-go/blob/v1.6.1/internal/r18devdump/store.go)：
其 DVD 查询使用 `ORDER BY content_id LIMIT 1`。这是公开实现的规则，并非 R18.dev 官方公布的完整消歧规范；
同一 DVD 编号涉及不同版本时，可用完整 CID 精确指定。

厂商前缀识别借鉴 [Javinizer-Go 的候选生成实现](https://github.com/javinizer/javinizer-go/blob/v1.6.1/internal/r18devdump/content_id_candidates.go)。
JavSP 不引入外部固定前缀清单，而是在导入时从本地 CID 提取 `1`、`118`、`h_086` 等实际系列前缀，
存入带主键索引的 `video_cid_prefix` 表；只解析无版本后缀、数字部分一至五位的普通 CID。
旧库启动时在事务内一次补建，失败则回滚并继续使用原库；直接查询旧库保持只读，但补建前仅支持原有无厂商前缀候选。
新转储重新生成前缀表。影片查询通过 `video_dvd`、`video_cid` 和前缀表索引完成，不逐片扫描整个影片表。

### 批次并发与资源生命周期

`concurrency.movies/images` 默认分别为 3/3，模型并发沿用 `translator.runtime.parallel`。
`BatchResources` 显式传入单片组件，持有批次共享图片池、停止信号与剧照启动节流。
影片线程池与子任务池独立，最多保持影片额度内的任务；准备完成但尚未提交的影片也占用额度。
剧照按有界窗口派发，完成任务的 Future 从资源登记中移除，避免整批任务历史持续累积。

每部影片独立创建抓取器、合并器、规划器和输出执行器；R18.dev 查询各自打开只读连接，启动构建的演员索引只读共享。
`movies: 1` 使用串行影片编排，子任务池仍共享并限制线程数。
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

`publish.naming` 在只读规划中同时检查全部目标路径的配置长度预算和文件系统限制。
POSIX 从目标路径最近的现有目录查询 `PC_NAME_MAX`、`PC_PATH_MAX`；名称限制查询不可用时按 255 回退，
Windows 名称按 UTF-16 单元计数。`length_by_byte` 只控制配置预算，POSIX 文件系统限制始终按实际编码字节检查。
影片与字幕计入实际扩展名和 `-CDx`，图片预留五字符扩展名，剧照计入 `extrafanart` 目录和编号。
只缩短用于命名的标题、原始标题、演员字段，按 Unicode 字符边界搜索可用前缀；演员优先保留完整名字并标记省略。
原始元数据及 NFO 标题不随路径截短而丢失。固定模板、其他字段或输出根目录无法满足预算时直接拒绝规划，
不再仅告警后进入写入阶段。规划器复查实际影片、字幕和产物目标，执行器在准备前及实际图片扩展名确定后再次校验。

临时产物与备份创建在对应目标文件系统中的 `.javsp-*` 目录。提交前检查文件状态；普通文件移动使用拒绝已有目标的重命名，
缺少该系统接口的平台使用硬链接发布后删除源名称；跨设备时先复制并验证大小，再发布和删除源文件。硬链接失败回退复制。元数据替换前备份旧文件，回滚按反向操作恢复。
只有整片提交成功后才发布影片结果和写正式移动日志。执行器直接调用时保留提交后清理直接空源目录的行为；
`Pipeline.run()` 使用 `cleanup_empty_dirs=False`，统一在批次结束后清理源子目录，始终保留扫描根目录。

`publish.source_cleanup.SourceCleanup` 在扫描完成、任务执行前记录整个输入目录的文件与目录快照，包含历史残留和根目录直属文件。
批次结束、线程回收后，在没有取消、批次错误、恢复不完整或 pending 时执行一次清理。普通单片失败不阻止其他合格目录清理。
未出现在成功移动记录中的本轮影片按扫描时的原始路径保护来源目录及其全部配套子目录，
即使失败影片文件已消失也保留来源目录；成功与失败影片共用目录时保留整个目录。
正常空扫描通过 CLI review 后也进入清理；扫描遍历或 NFO 目录读取失败直接终止流程，不把不完整扫描当作成功。
独立调用 `Scanner.scan()`、`Pipeline.scan()` 保持只读。

有效未处理影片以扫描后缀、`minimum_size` 和现有番号/CID 识别规则判定，重复与 NFO 跳过不影响有效性。
其所在目录及全部配套子目录保留；有效影片直接位于根目录时保留整个根目录内容。
其他普通残留包括 NFO、图片、字幕、文本、小视频与无法识别的视频，均可删除。
清理默认包含隐藏目录（包括旧演员头像目录）；`scanner.ignored_folder_name_pattern` 中的默认 `^\.` 仅用于扫描，其余排除规则继续用于清理。例如添加 `^\.private$` 可保留指定隐藏目录。
配置输出模板中首个动态目录组件以前的完整固定目录前缀及实际输出受保护；无法确定独立固定前缀时跳过清理。
除默认隐藏目录规则以外的扫描排除、符号链接、特殊文件、挂载点、`.javsp-*`、`recovery.json`、正在使用的文件移动日志和冲突跳过的字幕也受保护，保留必要祖先目录。
关闭移动、启用硬链接时不清理。

清理前按 `OutputResult.moved_files` 核对本轮影片转移记录、源文件消失、目标存在与大小，并复核目录身份与残留文件快照。
新增、被替换或意外变化的内容保留并告警。选取互不重叠的最上层合格子树和普通文件，
通过 `FileTransaction` 暂存到扫描根目录内、候选子树外的 `.javsp-*` 工作区。
确认暂存完成、来源没有新增内容且转移目标仍未变化后，删除暂存文件，再自底向上使用 `rmdir` 删除空目录。
暂存失败时逆序恢复并停止后续清理；恢复不完整或取消时保留清单，通过 `Stage.CLEANUP` 标记批次中断。
删除阶段失败保留尚存工作区及清单，可能已有部分残留文件被删除。
清理告警记录在 `BatchResult.warnings` 并显示于 CLI，不把已提交影片改判为失败；取消或恢复不完整使批次退出码为 1。

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

默认运行离线单元测试。也可显式运行 `pytest tests/unit`。

GitHub Actions 的 `Tests` 工作流在分支推送及 PR 时运行 Python 3.11、3.14 的 Ruff 检查和全部离线单元测试，
使用 `uv.lock` 锁定依赖。Docker 发布工作流（标签推送及手动发布）复用同一测试工作流；
全部检查通过后才允许构建和推送镜像。若需限制 PR 合并，仓库管理员还需在分支保护中将测试检查设为必需。

## 维护工具

```bash
uv run --locked --extra dev python tools/version.py
```

测试使用临时 SQLite 数据库、本地查询替身和本地 HTTP 服务，不运行真实媒体库整理。
在线站点测试、抓取样本更新和分类表生成工具已移除。

## 构建与清理

Docker 构建上下文排除测试、开发文档、虚拟环境、缓存和本地构建产物。
wheel 保留运行时资源，保留 `image/` 和默认配置的资源查找路径。
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
`LocalTranslator` 合并收集模型进程的 stdout/stderr；`ModelLogReader` 使用单个读取线程持续排空管道，
将 llama.cpp JSONL 的 INFO/DEBUG 映射为应用 DEBUG，保留 WARNING/ERROR，纯文本诊断也按级别转发。
模型原生日志阈值固定为 INFO，`other.debug` 控制应用端的明细可见性；不启用原生 verbose 或提示词日志。
停止时先 terminate，超时则 kill 并等待进程退出，再等待读取线程完成、关闭管道和自有 HTTP 会话。
启动失败在清理完成后附加有界的日志末尾；模型未就绪时仍不得扫描媒体。
独立组件通过参数接收配置；需要演员信息归一化时，向 `MetadataMerger` 显式传入映射或运行上下文。
`CrawlerExecutor` 仅调用 R18.dev 本地查询，不创建站点会话或抓取线程。
`prepare_database(path, *, session=None, proxy=None) -> Path` 返回经过校验的数据库路径；
更新失败可返回有效旧库，无有效库则抛出异常，启动层将其作为前置条件失败处理。
`r18dev.required_keys/normalize_actress_name/respect_source_id` 控制元数据规则，本地库始终启用。
旧 `crawler` 段、`r18dev.enabled`、`network.proxy_free` 和 `concurrency.crawlers` 在合并配置后被移除并提醒；
不继承旧通用规则，也不输出旧键的值。覆盖顺序保持默认模板、用户文件、环境变量、CLI。
图片下载使用独立的 `javsp.network`；数据库、映射和模型继续复用范围下载器。



## 内部接口迁移

Docker 挂载和批处理退出规则保持既有含义；配置迁移见上文。内部 Python 接口采用显式配置与结构化结果。

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
演员映射在有效 XML 缓存与版本元数据的来源、SHA-256 一致时，先使用条件 HEAD 检查远端 ETag；
返回 304 或 200 且 ETag 相同即复用缓存，不发起下载。HEAD 不支持（405/501）或未确认版本相同时，
继续使用条件 GET 范围探测；GET 返回 304 或 200/206 且 ETag 相同也不读取正文。
无有效缓存、元数据缺失或校验不符时重新获取；新 XML 校验后才发布，更新失败保留有效旧缓存。
演员中文名优先取有效上游字段；字段无效时，构建索引阶段按原名的汉字片段匹配同原名的纯汉字 keyword。
通用日文字符表、`々` 展开及 OpenCC 统一候选写法，合并重复条目；至少一个汉字锚点，候选须唯一，且在全表原名、中文字段、别名中无归属冲突。
纯汉字原名的同名中文占位值或简繁等价 keyword 采用同样的转换和归属检查；同名占位字段不会遮住有效的另一中文字段。
精确姓名优先于归一查表；组合名未精确匹配时，只有括号内外唯一属于同一演员才拆解，首名决定展示名和显式别名译名。
显式 alias 译名按父条目确认身份，不要求中文名全表唯一；同一别名译名冲突时保留输入。
`prepare_actor_mapping` 在解析有效缓存或新上游 XML 时叠加随包的 `data/actor-supplement.xml`，不改写上游缓存内容及版本摘要。
补充数据只补主名中文或缺失演员，不覆盖有效上游字段；显式别名译名同样以上游优先。没有主名译名的补充行须先有上游父条目。
补充文件及通用字符表通过包资源接口读取；来源和字符数据许可见 `javsp/metadata/data/README.md`，构建检查要求资源齐全。
回退索引构建后只读共享；字符结构匹配属于推断，具体边界见[元数据说明中的女优名称与别名](metadata.md#女优名称与别名)。
文件事务集中于 `publish.transaction`，输出执行器只协调暂存、校验、整片提交及提交后清理。
