# JavSP 2.0

JavSP 从文件名识别影片番号，汇总多个站点的元数据，整理本地影片，并为 Emby、Jellyfin、Kodi 等媒体库生成 NFO 和图片。当前版本以 Docker 批处理方式运行：处理完一批影片后退出，不持续监听目录，也不要求键盘输入。

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
  ghcr.io/flyalpaca/javsp:2.0.1
```

也可使用 `haobiubiu/javsp:2.0.1`。将三个 `/path/to/...` 替换为实际的宿主机路径；配置里的扫描路径应使用**容器内**路径 `/media`。首次运行会在挂载的配置目录生成 `config.yml`，默认扫描 `/media`。容器不会等待配置编辑，首次运行就会开始处理；需要预先自定义规则时，可先从 [默认配置](./config_default.yml) 复制并编辑宿主机的 `config.yml`，再运行容器。

启动时依次校验配置和扫描目录、准备数据源、验证本地翻译模型并启动翻译服务，随后扫描影片。默认只使用 R18.dev 本地数据库，不进行在线站点抓取；首次运行需要下载并导入数据库。默认模型为约 4.6 GB 的 Hy-MT2 7B GGUF；首次运行也需要下载模型，并为 `/app/models` 留出约 5 GB 空间。数据库和模型下载会显示进度条；数据库服务器未提供文件大小时显示已下载量。已缓存且校验通过的模型可以复用。数据库首次下载、模型下载或服务启动失败会阻止批处理；稳定版更新检查失败只会记录提醒。

默认仅处理符合番号识别规则且达到 `232MiB` 的视频，跳过已有 NFO 的目录，并忽略 `#整理完成` 等目录。若未找到可处理影片，程序会以非零状态退出。可在 [默认配置](./config_default.yml) 调整扫描规则。

| 镜像标签 | 用途 |
| --- | --- |
| `2.0.1` | 固定版本，由 Git 标签 `v2.0.1` 构建 |
| `latest` | 最近发布的稳定版本 |
| `dev` | 手动触发工作流构建的开发版本 |

推送 Git 标签后，发布工作流通过测试才会构建并发布镜像。镜像是否已可拉取，以容器仓库中的发布结果为准。

## 能做什么

- 识别影片番号与连续分片；同一影片的站点抓取并发进行，按配置优先级合并字段。
- 可选用 R18.dev 本地数据库，并以在线站点补齐未命中或为空的字段。
- 用容器内的 Hy-MT2 模型翻译含日文假名的标题和简介；整理演员别名、分类及标签。
- 下载封面和剧照，裁剪海报，生成 `folder.jpg`，匹配 `.srt`、`.ass` 字幕。
- 将影片和字幕移入目标目录，或保留原位置只更新元数据；支持硬链接整理。
- 提交前准备并校验产物，提交失败时尽量恢复本次移动和替换的文件。

默认要求抓取结果包含标题和封面。高清封面下载失败后会尝试普通封面；剧照单张失败不会阻止其他剧照或影片处理。

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

配置优先级从低到高为：包内默认配置、用户配置、`JAVSP_` 环境变量、`--o` 命令行覆盖。`-c` 可指定其他**容器内**用户配置路径；指定后不会自动查找 `/app/config/config.yml`。环境变量以双下划线表示层级，命令行覆盖使用点号路径，参数名和值用空格分隔：

```bash
docker run --rm \
  -v /path/to/media:/media -v /path/to/config:/app/config -v /path/to/models:/app/models \
  -e JAVSP_SCANNER__INPUT_DIRECTORY=/media \
  ghcr.io/flyalpaca/javsp:2.0.1

docker run --rm \
  -v /path/to/media:/media -v /path/to/config:/app/config -v /path/to/models:/app/models \
  ghcr.io/flyalpaca/javsp:2.0.1 \
  --oscanner.input_directory /media --osummarizer.move_files false
```

更多命名规则、网络代理、站点顺序和图片选项见 [默认配置](./config_default.yml)。

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

多分片影片使用 `-CD1`、`-CD2` 等后缀。图片扩展名随下载格式变化，可能为 `.jpg`、`.png` 或 `.webp`。`folder.jpg` 是不放大小图的海报缩略图，最大 300×450 像素；自定义输出已占用该路径时保留该图片。NFO 的 `genre` 来自自定义分类，站点分类与自定义标签写入 `tag`。

`move_files: false` 时，同一目录多部影片可能依次更新同名的 NFO 和图片，包括 `folder.jpg`；需要每部影片独立元数据时，请为它们使用独立目录。

### 本地翻译模型

翻译始终启用，仅处理含日文假名的标题和简介；无法可靠识别语言的纯汉字文本保持原样。翻译请求只发送给容器内的 `llama.cpp` 服务，首次模型下载需要访问 Hugging Face。可改用约 1.1 GB 的 `hy_mt2_1_8b_q4_k_m` 模型，以减少模型缓存空间。设置 `auto_download: false` 时，需要提前把配置所选的官方 GGUF 放入 `model_dir`；启动会校验文件大小和 SHA-256。

模型线程数、上下文和生成参数见 [默认配置](./config_default.yml)。默认 CPU 设置面向 i3-1315U／24 GB 内存，处理速度因机器而异。Slimeface 海报裁剪需要在构建镜像时安装 `crop` 可选依赖，并在配置里选择该引擎。

### R18.dev 本地数据库

默认启用本地库并关闭在线站点抓取。如需在本地库未命中或字段为空时使用在线站点，在用户配置里添加：

```yaml
crawler:
  online_enabled: true
```

程序启动时检查 [R18.dev 转储](https://r18.dev/dumps)；首次下载并导入，之后有新版才更新。库固定保存在**启动工作目录**的 `config/r18dev.sqlite`，Docker 中即 `/app/config/r18dev.sqlite`，与 `-c` 指定的配置文件位置无关。请持久化 `/app/config`，并为压缩转储、数据库及更新时的临时库预留空间。

处理影片时先查询本地库。只有启用 `crawler.online_enabled` 后，本地库未命中或可映射字段为空时才查询在线站点；在线字段只补空白。默认仅用本地库，未收录的影片会失败；本地库和在线抓取不能同时关闭。更新失败时保留可用旧库；首次下载失败且在线抓取开启时，退回在线抓取。

## 失败处理与日志

目标影片已存在时不会覆盖，当前影片整理失败；目标字幕已存在时跳过该字幕并保留源文件。已有 NFO 和图片允许更新，更新前会备份。异常或取消发生在当前进程内时，程序逆序恢复已移动的影片、字幕及被替换的元数据，并清理本次新增产物。普通单片失败且恢复完成后，继续处理下一部影片。

恢复不完整时，程序停止本批次，并在日志中列出 `.javsp-*` 恢复目录。目录内的 `recovery.json` 记录原始错误、未恢复操作和相关路径；请保留这些文件，按清单人工核对后再运行。断电或强制终止后不会自动恢复；多文件提交也不保证整目录同时可见。

控制台默认输出 INFO 及以上日志；`other.debug: true` 可显示 DEBUG。设置 `summarizer.filemove_log: true` 后，文件移动明细写入扫描目录下的 `filemove.log`。抓取超时会停止后续重试，但仍等待已启动的线程结束，因此超时不是整部影片的硬截止时间。

| 退出码 | 含义 |
| --- | --- |
| `0` | 已进入整理队列的影片全部成功 |
| `1` | 存在整理失败、取消、恢复不完整，或启动检查失败、未找到可处理影片 |

扫描时跳过的文件只记录在日志里，不计入整理队列的成功率。

## 从 1.x 升级

1. 保留原有 `/media` 和 `/app/config` 挂载，并新增持久化的 `/app/models` 挂载。首次启动前预留模型下载所需空间。
2. 检查旧 `config.yml`：删除 `translator.engine` 和 `translator.fields`，改用 `translator.model`、`model_dir`、`runtime` 和 `generation`。`engine: null` 也不能关闭翻译。
3. 检查自定义输出路径。若不同产物指向同一路径，2.0 会在提交前报错。先用少量影片验证命名结果，再处理整个目录。
4. 将镜像固定为 `2.0.1`。`latest` 随后续稳定版发布变化；历史四段版本标签仍用于 1.x 镜像。

本地 Python 环境仅用于开发和测试。升级本地安装后，可执行 `uv sync --locked --reinstall-package javsp` 刷新安装元数据。

## 开发、反馈与许可

代码结构、测试及构建命令见 [开发指南](./docs/development.md)。默认测试离线运行；在线抓取测试需显式启用。

[问题反馈](https://github.com/FlyAlpaca/JavSP/issues) · [功能建议](https://github.com/FlyAlpaca/JavSP/issues/new/choose) · [上游 Wiki](https://github.com/Yuukiy/JavSP/wiki)

保留原项目及社区贡献者的署名与许可。本项目受 [GPL-3.0](./LICENSE) 与 [Anti 996 License](https://github.com/996icu/996.ICU/blob/master/LICENSE_CN) 约束。使用本项目还须接受以下约定：

- 本软件仅供学习 Python 和技术交流使用。
- 请勿在微博、微信等墙内公共社交平台宣传本项目。
- 使用时遵守当地法律法规。
- 禁止用于商业用途。
