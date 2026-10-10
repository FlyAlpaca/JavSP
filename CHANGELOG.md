# Changelog

## Unreleased

### Changed
- 数据库与翻译模型改用固定 4 MiB 小片下载，最多同时使用四个连接；旧版四等分分片不会被当作新分片续传。

## 2.0.2 (2026-10-10)

### Changed
- R18.dev 数据库改从公开 Wasabi 对象列表发现新版本，记录转储日期和 ETag；同日期对象重传时也更新，旧库首次升级会重新导入一次。
- 数据库与翻译模型共用四连接分片及断点续传下载器；数据库更新继续在校验通过后原子替换，失败时保留可用旧库。
- 翻译模型下载进度同步输出到容器日志。

### Fixed
- 保留运行时 PATH 中已有命令路径，确保翻译服务可被找到。

## 2.0.1 (2026-10-10)

### Changed
- 启动下载 R18.dev 数据库和本地翻译模型时显示下载进度条；数据库响应未提供文件大小时显示已下载量。

## 2.0.0 (2026-10-10)

### Added
- 引入 Pipeline / JobRunner、Scanner、CrawlerExecutor、MetadataMerger、OutputPlanner、OutputExecutor 六组件流水线，提供独立扫描结果、阶段事件及单片/批次结果。
- 输出事务统一管理影片、字幕、NFO、封面、海报、文件夹封面和成功下载的剧照；产物先暂存校验，已有元数据备份后更新，异常或取消时逆序恢复。
- 恢复不完整时保留副本、备份与 recovery.json 清单并停止批次，显示未处理影片；不提供强制终止或断电后的自动恢复。
- 增加组件隔离、只读规划、离线端到端、跨设备提交与恢复、取消及外部修改保护测试。
- 在系统调用已成功但结果尚未登记时收到中断，仍能通过文件身份补全回滚状态；已提交任务在后续取消时保留成功结果。

### Changed
- CLI 仅负责初始化和交互，进度和统计由流水线结果驱动；保留现有配置格式及扫描、抓取、汇总、命名、文件移动兼容入口。
- 保持逐部影片、站点并发，抓取重试、线程回收和成功任务后的节流由 CrawlerExecutor 管理。
- 输出路径冲突在提交前检查，非兼容的产物目标重叠提前报错；发布操作原子拒绝新出现的目标文件。
- 全面重写 README，补充架构、配置优先级、Docker 配置初始化、失败恢复与 1.x 升级说明。
- Git tag v2.0.0 定义版本；Docker 发布支持三段新版本与四段历史版本，并明确 latest 和 dev 标签规则。

### Fixed
- 移动分片视频前预检全部目标；视频或字幕操作失败时回滚本次移动/硬链接，清理复制失败的部分目标，回滚失败时报告恢复路径。
- 爬虫超时后停止后续重试，丢弃超时源的数据，并回收所有工作线程后再汇总；在途请求仍须等其结束，不提供强制终止线程的硬截止时间。
- 下载保留图片真实扩展名，支持流式下载；封面与剧照最多并发三张，剧照统一按启动间隔节流，folder.jpg 使用海报缩略图。
- 封面选出成功结果后取消排队任务，等待在途下载结束和临时文件清理，避免批量运行时后台请求累积。
- JavBus/JavDB 分类汇总有效标签，避免后续标签覆盖有码/无码判定。
- 批处理中任一影片失败时返回退出码 1，全部成功时返回 0。

## [v1.10.0](https://github.com/darksoap/JavSP/compare/v1.9.3...v1.10.0) (2026-06-13)

### Added
- 重构翻译模块，支持五种引擎类型：
  - `openai_compatible`：兼容所有 OpenAI API 格式的服务（OpenAI、DeepSeek、Groq、硅基流动等）
  - `anthropic`：Claude 原生 API，支持自定义 base_url（中转代理）
  - `google`：Google 翻译非官方 API（免费，大陆需代理）
  - `bing`：Bing 翻译（免费，无需 API Key，推荐）
  - `alibaba`：阿里翻译（免费，无需 API Key，大陆可用）
- 新增 49 个翻译模块测试用例
- `ori_plot` 作为 MovieInfo 正式字段，替代之前的动态属性
- 配置文件分离：`config.yml` 改为 `config_default.yml`（随代码分发），用户配置读取 `~/.javsp/config.yml`（只需写需要覆盖的项）

### Changed
- 翻译默认引擎从 google（大陆不可用）改为 bing
- 移除 translators 库依赖，改用 requests 直接调用翻译 API，减少 11 个间接依赖
- Bing 翻译 `source_language=auto` 时不传 `from` 参数，由 API 自动检测源语言
- 更新 config_default.yml 翻译配置注释，按实测结果分类各翻译服务可用性
- 优化工作流触发条件，忽略测试数据和文档变更
- 更新 Issue 反馈链接指向新仓库

### Fixed
- 修复 API 翻译响应边界处理：空 choices、空 content 等异常情况
- 修复 wait_exit 在 Windows 上键盘缓冲区残留按键导致立即退出的问题
- 修复字幕匹配逻辑，先过滤字幕文件再匹配，增加目录存在性检查
- 禁用清华源，CI 使用 PyPI 官方源（海外服务器访问清华源被拒绝）

### Removed
- 移除无用的 pywin32-ctypes 依赖

## [v1.9.3](https://github.com/Yuukiy/JavSP/compare/v1.9.2...v1.9.3) (2026-06-11)

### Added
- 新增 `xpath_first` 工具函数，统一 XPath 空值检查与错误提示
- 新增 XP 选择器集中定义模式，各站点爬虫统一使用集中维护的 XPath 字典
- 新增 fanza GraphQL API 解析策略，适配 Next.js 前端渲染改版
- 刮削失败时报错信息包含番号、失败步骤和具体原因（爬虫名/封面URL/缺失字段/翻译错误）
- 运行结束后显示统计摘要（总计/成功/失败/失败详情），5秒后自动退出或按任意键立即退出

### Changed
- 迁移构建系统从 Poetry 到 Hatch + uv，并使用 ruff 格式化代码
- 更新 README 徽章指向新仓库，Python 版本标识升至 3.14

### Fixed
- 修复无分隔符番号的前导零处理（如 bbi00177 → BBI-177）
- 改进跨平台 Cookie 解密：Chrome 127+ ABE 感知、KDE KWallet 支持、临时文件安全
- 修复 CI 工作流配置错误

### Removed
- 移除 arzon/arzon_iv 爬虫源

## [v1.9 - v1.9.2](https://github.com/Yuukiy/JavSP/compare/v1.8...v1.9.2) (2026-05-22 ~ 2026-06-04)

### Added
- 添加无码和字幕的水印 [#73](https://github.com/Yuukiy/JavSP/commit/eaedc84049597eaab1ba064229f9b5bcf38aa504)
- 支持刮削剧照 [#176](https://github.com/Yuukiy/JavSP/issues/176)
- direnv 适配 [134b279](https://github.com/Yuukiy/JavSP/commit/134b279151aead587db0b12d1a30781f2e1be5b1)
- 添加硬链接支持 [#374](https://github.com/Yuukiy/JavSP/pull/374)
- 添加Docker镜像，每次tag发布将会同步更新一个Docker镜像到ghcr.io上。[#322](https://github.com/Yuukiy/JavSP/pull/322)

  参见[Package javsp](https://github.com/Yuukiy/JavSP/pkgs/container/javsp)。
- 添加新的爬虫`arzon`, `arzon_iv` [#377](https://github.com/Yuukiy/JavSP/pull/377)
- Slimeface人脸识别 [#380](https://github.com/Yuukiy/JavSP/pull/380)
- 支持Linux和MacOS(x64)二进制 [a754e1c](https://github.com/Yuukiy/JavSP/commit/a754e1ce0f14b0ca9dcc6d43d8e7d322a3da1c43)
- 添加选项`other.interactive`来表示程序是否应该在interactive模式下运行
- 智能错误分级报告：单源失败降级为 INFO，仅全部失败时汇总报错，curl_cffi 网络错误不再暴露 traceback
- replace cloudscraper with curl_cffi 绕过 CloudFlare TLS 指纹检测
- 支持 Python 3.14
- 添加本地字幕匹配功能
- 启用 poetry-dynamic-versioning，版本号由 git tag 驱动
- 新增 macOS Keychain Cookie 解密支持

### Changed
- 使用 Poetry 作为构建系统 [134b279](https://github.com/Yuukiy/JavSP/commit/134b279151aead587db0b12d1a30781f2e1be5b1)
- 使用 Cx_Freeze 作为打包工具 [134b279](https://github.com/Yuukiy/JavSP/commit/134b279151aead587db0b12d1a30781f2e1be5b1)
- 将 Groq 翻译接口重构为 OpenAI 通用翻译接口 [#371](https://github.com/Yuukiy/JavSP/pull/371)
- 用更高清的Logo替换旧的Logo [7b8690f](https://github.com/Yuukiy/JavSP/commit/7b8690fb4af831c0e5ad5ed97cac61d51117c7eb)
- 重构配置文件，现在使用YAML保存配置文件 [e096d83](https://github.com/Yuukiy/JavSP/commit/e096d8394a4db29bb4a1123b3d05021de201207d)

  旧用户迁移可以使用[这个脚本](./tools/config_migration.py)
- 除了`-c,--config`以外的其他命令行参数，都被改为与配置文件的命名一致的传入方法。 [e096d83](https://github.com/Yuukiy/JavSP/commit/e096d8394a4db29bb4a1123b3d05021de201207d)

  比如需要重写扫描的目录可以这样传入参数：
  ```
  poetry run javsp -- --oscanner.input_directory '/some/directory'
  ```
- 删除了所有环境变量，现在环境变量的传入方法如下。 [e096d83](https://github.com/Yuukiy/JavSP/commit/e096d8394a4db29bb4a1123b3d05021de201207d)
  ```
  env JAVSP_SCANNER.INPUT_DIRECTORY='/some/directory' poetry run javsp
  ```
- 为了引入对类型注释的支持，最低Python版本现在为3.10
- 重构封面剪裁逻辑 [#380](https://github.com/Yuukiy/JavSP/pull/380)
- 重构 `parallel_crawler` 异常处理：区分网络错误/站点封禁/影片不存在，curl_cffi 异常被正确识别并自动重试
- 简化爬虫日志名称（`javsp.web.javlib` → `javlib`）
- 合并 arzon 双搜索模式，提取 fc2 通用工具
- 重构 CI workflow 并升级 Actions 版本
- 移除 dependabot，改用 renovate 统一管理依赖更新
- 优化 Docker 镜像构建，runner 阶段只复制运行时必要文件

### Fixed
- 修复网络超时设置无效的问题：`Duration.seconds` 误用导致超时始终为 1 秒，改用 `total_seconds()`
- 修复 Windows/跨平台兼容性问题：extrafanart 跨平台路径、macOS 文件名非法字符替换、硬链接 OSError fallback、stderr 编码
- 修复 urlretrieve 安全问题：headers 空值防护与副作用消除、referer 提取边界处理、文件写入模式修正
- 修复自动更新逻辑：os.rename 添加 fallback、Zip Slip 防护、移除矛盾的 p.wait/p.terminate
- 修复 cx_freeze 产物版本号并优化 CI 版本策略
- 修复 Linux/macOS Chrome Cookie 解密：原实现使用错误的 AES-GCM 算法，改为正确的 AES-CBC（v10 固定密码 + v11 keyring）
- FIX: 修复图标没有添加到封面上的 bug [#262](https://github.com/Yuukiy/JavSP/issues/176)
- 修复 SQL 注入风险：cookie 查询改用参数化查询

### Removed
- Pyinstaller 打包描述文件 [134b279](https://github.com/Yuukiy/JavSP/commit/134b279151aead587db0b12d1a30781f2e1be5b1)
- requirements.txt [134b279](https://github.com/Yuukiy/JavSP/commit/134b279151aead587db0b12d1a30781f2e1be5b1)
- MovieID.ignore_whole_word 功能和ignore_regex重复 [e096d83](https://github.com/Yuukiy/JavSP/commit/e096d8394a4db29bb4a1123b3d05021de201207d)
- NamingRule.media_servers：由于不常用删除，之后会出更general的解决方案 [#353](https://github.com/Yuukiy/JavSP/issues/353)
- Baidu AIP人脸识别，请使用Slimeface替代。
- airav: 站点已失效，移除爬虫及相关测试数据和工具脚本

## v1.8 - 2024-09-28（Fork 前历史）

### Added
- 新增站点njav, fc2ppvdb
- 添加选项控制封面选择逻辑，优先使用非javdb的封面以避免水印
- 支持自定义要写入到nfo的genre和tag中的字段
- 支持添加uncensored标签到poster图片
- 支持调用Claude(haiku)和Groq(llama3.1-70b)翻译接口

### Changed
- 适配网页和接口的变化: avsox, fc2, fanza, mgstage, prestige, javmenu
- 修复写入nfo时的拼写问题
- 修复Windows下无法读取Cookies的问题
- 修复封面图片url存在?参数时下载图片失败的问题
- 解决图片下载请求被javbus拦截的问题
- 优化google翻译参数和速率，减少被QoS
- 为Cloudflare拦截导致的失败请求给出提示
- 改进T38-000系列影片的番号识别
- msin: 站点关闭，移除相应代码及测试用例
