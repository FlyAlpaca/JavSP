# JavSP 2.1

JavSP 从文件名识别影片番号，查询 R18.dev 本地数据库中的元数据，整理本地影片，并为 Emby、Jellyfin、Kodi 等媒体库生成 NFO 和图片。当前版本以 Docker 批处理方式运行：处理完一批影片后退出，不持续监听目录，也不要求键盘输入。

本仓库延续 [Yuukiy/JavSP](https://github.com/Yuukiy/JavSP)，基于 [darksoap/JavSP](https://github.com/darksoap/JavSP) 开发。2.0 将扫描、抓取、字段合并、输出规划和文件提交分开管理；具体改动见 [更新日志](./CHANGELOG.md)。

[![Tests](https://github.com/FlyAlpaca/JavSP/actions/workflows/tests.yml/badge.svg)](https://github.com/FlyAlpaca/JavSP/actions/workflows/tests.yml)
[![Docker Build](https://github.com/FlyAlpaca/JavSP/actions/workflows/docker-build.yml/badge.svg)](https://github.com/FlyAlpaca/JavSP/actions/workflows/docker-build.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License](https://img.shields.io/badge/license-GPL--3.0-green)](./LICENSE)

## 快速开始

准备三个宿主机目录：影片目录、配置目录、模型缓存目录。影片和配置目录需要容器写入权限。镜像仅支持 `linux/amd64`。

```bash
docker run --rm --name javsp \
  -v /path/to/media:/media \
  -v /path/to/config:/app/config \
  -v /path/to/models:/app/models \
  ghcr.io/flyalpaca/javsp:2.1.0
```

也可使用 `haobiubiu/javsp:2.1.0`。将三个 `/path/to/...` 替换为实际的宿主机路径；配置里的扫描路径应使用**容器内**路径 `/media`。首次运行会在挂载的配置目录生成 `config.yml`，默认扫描 `/media`。容器不会等待配置编辑，首次运行就会开始处理；需要预先自定义规则时，可先从 [默认配置](./config_default.yml) 复制并编辑宿主机的 `config.yml`，再运行容器。

启动时依次校验配置和扫描目录、准备数据源、验证本地翻译模型并启动翻译服务，随后扫描影片。始终使用 R18.dev 本地数据库；首次运行需要下载并导入数据库。默认模型为约 4.6 GB 的 Hy-MT2 7B GGUF；首次运行也需要下载模型，并为 `/app/models` 留出约 5 GB 空间。数据库和模型下载会显示进度条。已缓存且校验通过的模型可以复用。数据库首次下载、模型下载或服务启动失败会阻止批处理；稳定版更新检查失败只会记录提醒。

默认仅处理符合番号识别规则且达到 `232MiB` 的视频，跳过已有 NFO 的目录，并忽略 `#整理完成` 等目录。若未找到可处理影片，程序会以非零状态退出。可在 [默认配置](./config_default.yml) 调整扫描规则。

番号数字不统一补成固定长度：带分隔符的写法保留原位数；普通紧凑写法中的五位 `00xxx` 按兼容规则转换为至少三位，例如 `ure00013` → `URE-013`、`bbi00177` → `BBI-177`。四位、五位非 `00` 开头以及特殊编号体系保留原位数。该转换只是扫描启发式，厂家正式番号以命中的元数据为准，详见[番号与补零规则](docs/development.md#番号与补零规则)。

| 镜像标签 | 用途 |
| --- | --- |
| `2.1.0` | 固定版本，由 Git 标签 `v2.1.0` 构建 |
| `latest` | 最近发布的稳定版本 |
| `dev` | 手动触发工作流构建的开发版本 |

推送 Git 标签后，发布工作流通过测试才会构建并发布镜像。镜像是否已可拉取，以容器仓库中的发布结果为准。

## 能做什么

- 识别影片番号与连续分片；默认同时准备 3 部影片，查询本地元数据并共享图片下载线程池。
- R18.dev 是唯一元数据来源，本地未收录或缺少必需字段的影片会失败并保留源文件。
- 用容器内的 Hy-MT2 模型翻译含日文假名的标题和简介；整理演员别名、分类及标签。
- 下载封面和剧照，裁剪海报，生成 `folder.jpg`，匹配 `.srt`、`.ass` 字幕。
- 将影片和字幕移入目标目录，或保留原位置只更新元数据；支持硬链接整理。
- 提交前准备并校验产物，提交失败时尽量恢复本次移动和替换的文件。

默认要求抓取结果包含标题和封面。DMM 封面按原图 CDN、普通大图、缩略图的顺序尝试，并优先使用独立竖版原图生成海报；`folder.jpg` 保留海报尺寸。其他来源仍先尝试高清地址再回退普通地址。剧照单张失败不会阻止其他剧照或影片处理。

## 配置

容器首次启动会将镜像内的默认模板复制到 `/app/config/config.yml`。之后编辑宿主机的 `/path/to/config/config.yml` 即可，下一次运行生效。程序始终先读取随包发布的默认配置，再读取用户配置，所以用户文件也可以只保留需要修改的字段：

```yaml
scanner:
  input_directory: /media
network:
  proxy_server: null
summarizer:
  move_files: true
  match_subtitles: true
  path:
    output_folder_pattern: '#整理完成/{actress}/[{num}] {title}'
    hard_link: false
translator:
  model: hy_mt2_7b_q4_k_m
  model_dir: /app/models
  auto_download: true
```

空用户配置使用默认值；配置不可读、格式错误或顶层不是映射时，程序会报告配置加载失败并退出。
`scanner.ignored_folder_name_pattern: []` 表示不按目录名忽略子目录。相对输出模板始终以扫描目录为基准。

配置优先级从低到高为：包内默认配置、用户配置、`JAVSP_` 环境变量、`--o` 命令行覆盖。`-c` 可指定其他**容器内**用户配置路径；指定后不会自动查找 `/app/config/config.yml`。环境变量以双下划线表示层级，命令行覆盖使用点号路径，参数名和值用空格分隔：

```bash
docker run --rm \
  -v /path/to/media:/media -v /path/to/config:/app/config -v /path/to/models:/app/models \
  -e JAVSP_SCANNER__INPUT_DIRECTORY=/media \
  ghcr.io/flyalpaca/javsp:2.1.0

docker run --rm \
  -v /path/to/media:/media -v /path/to/config:/app/config -v /path/to/models:/app/models \
  ghcr.io/flyalpaca/javsp:2.1.0 \
  --oscanner.input_directory /media --osummarizer.move_files false
```

更多命名规则、网络代理、本地数据规则和图片选项见 [默认配置](./config_default.yml)。

### 文件整理方式

| 设置 | 结果 |
| --- | --- |
| `summarizer.move_files: true` | 按命名模板移动影片；启用 `match_subtitles` 时一起移动匹配的字幕 |
| `summarizer.move_files: false` | 影片和字幕留在原处，在影片所在目录生成或更新元数据 |
| `summarizer.path.hard_link: true` | 整理影片时优先使用硬链接并保留源文件；不支持或跨设备时复制 |

默认输出示例：

```text
#整理完成/
└── 演员名/
    └── [ABC-123] 影片标题/
        ├── ABC-123.mp4
        ├── ABC-123.srt
        ├── movie.nfo
        ├── fanart.jpg
        ├── poster.jpg
        ├── folder.jpg
        └── extrafanart/
            ├── 0.jpg
            └── 1.jpg
```

多分片影片使用 `-CD1`、`-CD2` 等后缀。图片扩展名随下载格式变化，可能为 `.jpg`、`.png` 或 `.webp`。`folder.jpg` 是不放大小图的海报缩略图，最大 300×450 像素；自定义输出已占用该路径时保留该图片。NFO 的 `genre` 来自自定义分类，数据库分类与自定义标签写入 `tag`。

`move_files: false` 时，同一目录多部影片可能依次更新同名的 NFO 和图片，包括 `folder.jpg`；需要每部影片独立元数据时，请为它们使用独立目录。

### 女优名称与别名

女优名称使用 [Jav Actors Mapping](https://github.com/li-peifeng/Jav-Actors-Mapping) 的 `actor-mapping.xml`，不再使用内置 JSON 表。每次启动都会检查 GitHub 更新：已有有效缓存和匹配的版本元数据时，先通过 HEAD 请求检查 ETag，返回 304 或 ETag 相同则直接复用缓存，不下载 XML；确认版本变化后才下载并校验，再开始整理。服务器不支持 HEAD 时回退条件 GET 检查。缓存或版本元数据缺失、损坏时重新获取。缓存固定保存在**启动工作目录**的 `config/actor-mapping.xml`，Docker 中为 `/app/config/actor-mapping.xml`；版本信息保存在同目录的 `actor-mapping.json`。请持久化 `/app/config`。下载支持 `network.proxy_server` 和环境代理。服务器支持可靠范围请求且提供强 ETag 时，使用 1 MiB 分片、最多四连接下载；连接/读取超时分别为 30/120 秒，网络失败最多尝试三次，并保留分片供同版本续传，远端版本变化时清理旧分片。不支持可靠范围请求时回退单连接。完整下载通过 XML 校验后才替换缓存。更新失败时告警并使用有效旧缓存；首次无法获取映射且没有有效缓存时终止启动。

名称统一展示为 `原名（中文译名）`，例如 `新ありな（新有菜）`；中文优先使用简体，其次使用繁体。中文字段须包含汉字且不含日文假名，日文或纯罗马音占位值不作为中文译名；简体字段无效时尝试繁体字段。同一日文原名的重复条目中，空值、日文占位及与原名相同的值不会遮住唯一有效译名，例如 `木下ひまり（木下日葵）`；存在多个不同有效译名时保留输入名称。没有中文译名或名称相同时只展示原名，不用模型猜测人名。该格式始终生效；`r18dev.normalize_actress_name` 仅控制是否把 `keyword` 别名归到主名，默认开启。关闭时别名仍查表并格式化，但保留抓取到的别名，例如 `橋本ありな（新有菜）`。上游没有逐个别名的中文译名字段，因此别名采用所属原名的中文名。

对于上游中文字段缺失或与原名相同的已核实条目，补充 `二階堂麗 → 二阶堂丽`（上游 keyword）、[彩月七緒 → 彩月七绪](https://www.renwudang.com/person/satsuki-nao)、[蒼山愛奈 → 苍山爱奈](https://actresseshub.com/japanese_actresses_archive/468.html)。补全只对映射表已收录的对应原名生效，新的有效上游译名优先，冲突译名仍保留输入。

上游没有收录的名称、缺少原名的条目及无法唯一匹配的别名保留抓取名。上游别名覆盖与旧表不同，主名也可能变化。对于已核实的 `雛形みくる`，中文字段无效时可回退到同表 `keyword` 中的 `雏形美久琉`，其次为 `雛形美久琉`；须确认该别名唯一属于此原名。有效中文字段仍优先，不从其他汉字别名猜测译名（别名可能是旧艺名）。目录、命名模板、NFO 演员和头像引用使用同一份汇总结果，转换后的重复姓名会合并。R18.dev 有头像地址时，成功下载的头像保存在影片目录的 `.actors/`，NFO 引用对应本地图片；下载失败时保留远程地址并记录警告。媒体中心是否显示演员头像仍取决于它对 NFO `actor/thumb` 的支持。已整理的影片不会自动批量改名或改写 NFO。

### 本地翻译模型

翻译始终启用，仅处理含日文假名的标题和简介；无法可靠识别语言的纯汉字文本保持原样。翻译请求只发送给容器内的 `llama.cpp` 服务，首次模型下载需要访问 Hugging Face。可改用约 1.1 GB 的 `hy_mt2_1_8b_q4_k_m` 模型，以减少模型缓存空间。设置 `auto_download: false` 时，需要提前把配置所选的官方 GGUF 放入 `model_dir`；启动会校验文件大小和 SHA-256。

自动下载使用 4 个连接分片，下载中断后会从模型目录中的临时分片续传。下载源不支持可靠的范围请求时，自动改用可续传的单连接下载；服务器不支持续传时重新下载。终端显示总进度条，容器日志定期记录总进度。下载完成后校验大小与 SHA-256，再替换正式模型。组装期间，模型目录短时需要约两倍模型大小的可用空间。

若首次下载报 `Connection to huggingface.co timed out`，先确认容器能访问 Hugging Face。使用代理时，在 `config.yml` 设置 `network.proxy_server` 为**容器可访问**的代理地址；模型下载也会使用它。容器里的 `127.0.0.1` 指向容器自身，不能直接指向宿主机代理。也可以在宿主机下载与 `translator.model` 对应的官方 GGUF，放入挂载到 `/app/models` 的目录，设置 `translator.auto_download: false` 后重启；文件名、大小和 SHA-256 必须与程序内的模型规格一致。

模型线程数、上下文和生成参数见 [默认配置](./config_default.yml)。默认 CPU 设置面向 i3-1315U／24 GB 内存，处理速度因机器而异。Slimeface 海报裁剪需要在构建镜像时安装 `crop` 可选依赖，并在配置里选择该引擎。

### R18.dev 本地数据库

R18.dev 本地库始终启用，无启停开关。以下通用规则可在用户配置中调整：

```yaml
r18dev:
  required_keys: [cover, title]
  normalize_actress_name: true
  respect_source_id: true
```

`required_keys` 指定整理必需的字段；`normalize_actress_name` 控制别名归一；
`respect_source_id` 控制是否采用数据库记录中的正式番号，关闭时保留文件名识别的番号。

升级到 2.1 时，删除旧 `crawler` 配置段、`r18dev.enabled`、`network.proxy_free` 和 `concurrency.crawlers`。
程序会忽略这些旧键并提示迁移，不继承旧值；原 `crawler.required_keys`、`crawler.normalize_actress_name`
分别移至 `r18dev` 同名键，`crawler.respect_site_avid` 改为 `r18dev.respect_source_id`。
用户文件、环境变量和 CLI 覆盖均遵守这条规则；旧开关无法关闭本地库或开启在线刮削。

首次启动时查询 [R18.dev 公开 Wasabi 转储](https://r18dotdev.s3.eu-west-1.wasabisys.com/?list-type=2&prefix=dumps%2Fr18dotdev_dump_) 并下载导入；本地数据库上次成功导入未满 7 天时，启动会跳过远端更新检查。满 7 天后检查远端，只在转储日期更新或同日对象 ETag 变化时重新导入；检查未发现新转储时，后续启动仍会继续检查。旧版数据库没有导入时间时，以文件修改时间判断；没有 ETag 时会先重新导入一次以记录对象身份。下载使用四连接分片并支持中断续传；范围请求不可靠时回退单连接。库固定保存在**启动工作目录**的 `config/r18dev.sqlite`，Docker 中即 `/app/config/r18dev.sqlite`，与 `-c` 指定的配置文件位置无关。请持久化 `/app/config`，并为下载分片、组装的压缩转储、旧数据库及导入时的临时库预留空间；组装时压缩转储会短时占用约两份空间。

处理影片时先查询本地库。DVD 番号优先精确匹配，再查询仅数字前导零不同的写法；同等级多条命中时，按 `content_id` 字典序升序选第一条。仍未命中时，一次查询所有 CID 候选：保留未补零、五位补零等既有优先级，再尝试本地库实际观测的厂商前缀，例如 `1start00538`、`118ipx00658`、`h_086fsdss00615`。厂商前缀表在导入时生成，旧库启动时一次补建。若 CID 记录带有不一致的 DVD 番号，则不使用该记录。指定完整 CID 时只精确查询，不改写其数字；详细依据见[开发指南](docs/development.md#r18dev-查询与选择规则)。本地未收录、查询失败或缺少必需字段时，该影片失败并保留源文件，普通失败可继续批次。
数据库更新失败时保留可用旧库；首次无有效库且准备失败时终止启动。
元数据查询不访问在线站点；数据库更新、演员映射、模型和图片仍按需联网下载。


### 输出命名

输出命名同时检查完整路径预算与文件系统的单个目录/文件名限制，包含影片、字幕的分片后缀及 NFO、图片、剧照。
超长时自动缩短路径中的标题、原始标题或演员字段；演员字段优先保留完整名字并以 `…` 标记省略，NFO 保留完整标题和演员列表。
`summarizer.path.length_by_byte: false` 仅改变配置预算的计数方式，不能绕过文件系统的字节限制。
固定模板或输出目录本身过长且无法缩短时，影片在规划阶段失败并保留源文件，请缩短模板或输出目录。

## 大批量影片与并发

默认最多同时准备 3 部影片，抓取、翻译等待、封面和剧照下载可以在影片之间重叠执行。
全批次最多使用 3 个图片下载线程；等待提交的影片也占用影片额度，避免临时产物随影片数量积压。
文件移动与产物提交按扫描队列顺序串行进行，适合普通 NAS 或网络挂载。实际提速取决于网络、磁盘和模型耗时。

```yaml
concurrency:
  movies: 3
  images: 3
```

两个值均须大于 0。旧用户配置未填写这些键时使用上述默认值；设置 `movies: 1` 恢复影片串行处理，
本地查询随影片执行，图片下载仍受共享线程额度限制。扫描、数据库和模型启动准备、批次结束后的源目录清理保持顺序执行；
启动资源下载仍使用原有分片机制，其连接数不由上述参数控制。

`summarizer.extra_fanarts.scrap_interval` 在批次内统一控制剧照下载启动间隔。

翻译请求并发沿用 `translator.runtime.parallel`，默认 1；增加影片并发不会自动增加模型槽位或另启模型进程。
提高模型并发前需评估可用内存及上下文容量。Slimeface 推理串行执行。
控制台显示总进度和各活动任务的番号、阶段；非终端日志按任务编号记录阶段，同番号也能区分。

同一并发批次中，多个影片指向相同影片或产物目标时，后续任务报告冲突并保留源影片，避免覆盖已提交结果。
取消或恢复不完整时停止派发和后续提交，等待在途线程结束再清理未提交产物；已成功提交的影片保留成功状态。
未提交且因停止退出的影片列入未处理列表。

## 失败处理与日志

目标影片已存在时不会覆盖，当前影片整理失败；目标字幕已存在时跳过该字幕并保留源文件。已有 NFO 和图片允许更新，更新前会备份。异常或取消发生在当前进程内时，程序逆序恢复已移动的影片、字幕及被替换的元数据，并清理本次新增产物。普通单片失败且恢复完成后，继续处理下一部影片。

全部影片任务和在途线程结束后，在没有取消、未完成任务、批次错误或恢复不完整时，统一清理扫描根目录中的残留文件和子目录。普通影片失败时，保留该影片原始来源目录及全部配套文件，其他合格目录仍会清理；成功与失败影片共用来源目录时保留整个目录；没有待处理影片但扫描正常时，也会清理历史残留。清理覆盖往轮留下的 NFO、图片、文本、字幕及根目录直属残留文件，不受 `scanner.skip_nfo_dir` 影响。

有效未处理影片会保留：后缀属于 `scanner.filename_extensions`、大小达到 `scanner.minimum_size`，且按现有规则能识别番号或 CID。重复、跳过的有效影片及其所在目录的全部配套子目录均保留；若有效影片直接位于扫描根目录，则保留整个根目录内容。**没有有效影片的其他目录及残留文件会删除，其中也包括过小视频和无法识别番号的视频。**

扫描根目录本身、配置输出模板的固定目录前缀及本轮实际输出、扫描排除目录、目标冲突跳过的源字幕、符号链接、特殊文件、其他挂载点及恢复材料始终保留。输出模板没有可独立保护的固定目录前缀时跳过清理。扫描失败不会进入清理；清理复核发现新增文件、目录被替换、内容变化或转移目标异常时保留相关内容并告警。正常情况下根目录仅剩目标目录；失败影片目录、有效未处理影片或受保护内容仍在时，日志说明保留原因。

关闭 `summarizer.move_files`、启用 `summarizer.path.hard_link` 时，不执行上述清理。启用 `summarizer.filemove_log` 时保留正在使用的 `filemove.log`。残留文件先事务暂存，暂存失败时恢复；确认全部暂存成功后再删除。删除阶段的普通失败记录为清理告警，不改变影片转移成功状态；暂存文件删除后不能再恢复这些残留文件。

恢复不完整时，程序停止本批次，并在日志中列出 `.javsp-*` 恢复目录。目录内的 `recovery.json` 记录原始错误、未恢复操作和相关路径；请保留这些文件，按清单人工核对后再运行。断电或强制终止后不会自动恢复；多文件提交也不保证整目录同时可见。

控制台默认输出 INFO 及以上日志，保留主要处理阶段、资源下载总进度、整理结果及告警和错误。
模型服务的输出也使用统一的应用时间戳、级别和格式，默认显示启动等待、就绪、告警和错误。
模型加载明细、slot 调度、推理 timing、缓存校验与复用、本地查询进度、图片下载明细、路径截短和文件重命名等细节使用 DEBUG；
`other.debug` 默认为 `false`，设为 `true` 可查看这些排查信息。设置 `summarizer.filemove_log: true` 后，文件移动明细写入扫描目录下的 `filemove.log`。
模型服务提前退出或就绪超时时，错误附带最多 10 条、每条最多 1000 字符的近期模型日志，便于在默认日志级别下诊断启动失败。

批次失败摘要每部最多显示 240 字符，番号和阶段各显示一次，多条原因合并为一行；超长内容保留首尾，方便查看错误类型和路径末尾。完整错误仍保留在批次结果及前面的“整理失败”日志中。

| 退出码 | 含义 |
| --- | --- |
| `0` | 已进入整理队列的影片全部成功，或正常扫描后没有待处理影片 |
| `1` | 存在整理失败、取消、恢复不完整，或启动检查、扫描失败 |

扫描时跳过的文件只记录在日志里，不计入整理队列的成功率。

## 从 1.x 升级

1. 保留原有 `/media` 和 `/app/config` 挂载，并新增持久化的 `/app/models` 挂载。首次启动前预留模型下载所需空间。
2. 检查旧 `config.yml`：删除 `translator.engine` 和 `translator.fields`，改用 `translator.model`、`model_dir`、`runtime` 和 `generation`。`engine: null` 也不能关闭翻译。
3. 检查自定义输出路径。若不同产物指向同一路径，2.0 会在提交前报错。先用少量影片验证命名结果，再处理整个目录。
4. 将镜像固定为 `2.1.0`。`latest` 随后续稳定版发布变化；历史四段版本标签仍用于 1.x 镜像。

本地 Python 环境仅用于开发和测试。升级本地安装后，可执行 `uv sync --locked --reinstall-package javsp` 刷新安装元数据。

## 开发、反馈与许可

代码结构、测试及构建命令见 [开发指南](./docs/development.md)。测试使用临时数据、本地查询替身和本地 HTTP 服务。

[问题反馈](https://github.com/FlyAlpaca/JavSP/issues) · [功能建议](https://github.com/FlyAlpaca/JavSP/issues/new/choose) · [上游 Wiki](https://github.com/Yuukiy/JavSP/wiki)

保留原项目及社区贡献者的署名与许可。本项目受 [GPL-3.0](./LICENSE) 与 [Anti 996 License](https://github.com/996icu/996.ICU/blob/master/LICENSE_CN) 约束。使用本项目还须接受以下约定：

- 本软件仅供学习 Python 和技术交流使用。
- 请勿在微博、微信等墙内公共社交平台宣传本项目。
- 使用时遵守当地法律法规。
- 禁止用于商业用途。
