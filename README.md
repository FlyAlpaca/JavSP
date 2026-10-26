# JavSP

从文件名识别影片番号，查询 R18.dev 本地数据库，整理影片和字幕，并为 Emby、Jellyfin、Kodi 等媒体库生成 NFO、封面和海报。

[![Tests](https://github.com/FlyAlpaca/JavSP/actions/workflows/tests.yml/badge.svg)](https://github.com/FlyAlpaca/JavSP/actions/workflows/tests.yml)
[![Docker Build](https://github.com/FlyAlpaca/JavSP/actions/workflows/docker-build.yml/badge.svg)](https://github.com/FlyAlpaca/JavSP/actions/workflows/docker-build.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License](https://img.shields.io/badge/license-GPL--3.0-green)](./LICENSE)

## 概述

JavSP 是一个面向本地 JAV 影片的媒体库整理工具，适合将下载目录中的影片批量整理为媒体服务器可识别的目录和配套资料。它从文件名识别番号，查询 R18.dev 本地数据库获取标题、演员、发行日期等信息，完成本地翻译与演员名称整理，再按配置生成目录、重命名影片并匹配字幕。

整理结果包含影片文件、NFO 元数据、封面、海报及剧照，可供 Emby、Jellyfin、Kodi 等媒体服务器扫描入库。项目通过 Docker 批处理运行：挂载影片、配置和模型缓存目录后启动，完成本批次处理即退出；支持移动整理、保留源文件的硬链接整理，以及仅更新原目录中的元数据。

## V2.0 之后，变化在哪里？

**V2.0 是一次处理流程重构。** 扫描、元数据查询、合并、输出规划与文件提交各自独立，整理过程有明确的阶段、结果和失败恢复边界。后续 2.x 在此基础上继续调整数据来源与运行方式：

- **本地元数据**：从 2.1 起仅使用 R18.dev 本地库，移除在线多站点刮削；未收录影片保留源文件，不再回退其他站点。
- **本地翻译与名称整理**：使用容器内 Hy-MT2 模型翻译日文标题和简介，演员名称通过映射表整理为“原名（中文译名）”。
- **先准备，再提交**：校验输出、检查冲突后才移动文件；异常时尝试恢复，恢复不完整则停止并保留恢复材料。
- **批量处理**：默认同时准备 3 部影片，共享图片下载额度，按队列顺序提交；Docker 无交互运行，处理结束退出。

从旧版升级请先看[2.x 升级指南](docs/migration.md)；逐版本变化见[更新日志](CHANGELOG.md)。元数据查询在本地完成，数据库、演员映射、模型和图片仍会按需联网下载。

## 快速开始

镜像支持 `linux/amd64`。准备可写的影片、配置和模型缓存目录；首次启动需要下载数据库、演员映射和模型。默认模型约 4.6 GB，下载组装期间模型目录需约两倍空间，数据库另需预留下载与导入空间。

> **运行即开始整理，请先确认输入目录。** 默认移动影片并清理符合条件的源目录残留，包括过小或无法识别的视频；正常空扫描也可能清理历史残留。首次建议使用少量影片副本，完整规则见[文件清理与恢复](docs/usage.md#失败处理与日志)。

需要自定义规则时，先把[默认配置](config_default.yml)复制到宿主机配置目录并命名为 `config.yml`，编辑后再启动。没有配置文件时容器会自动生成并立即运行。

```bash
docker run --rm --name javsp \
  -v /path/to/media:/media \
  -v /path/to/config:/app/config \
  -v /path/to/models:/app/models \
  ghcr.io/flyalpaca/javsp:latest
```

将 `/path/to/...` 替换为宿主机路径；配置使用容器内路径，例如 `/media`。也可使用 `haobiubiu/javsp:latest`；需固定版本时，将 `latest` 换成已发布的版本标签。

默认扫描达到 `232MiB` 且可识别番号的视频，跳过已有 NFO 的目录，输出到 `/media/#整理完成/演员名/[番号] 标题/`，生成 NFO、封面、海报、`folder.jpg` 和剧照，并匹配字幕。支持只更新元数据或硬链接整理，设置方法见[使用指南](docs/usage.md#文件整理方式)。

## 文档

| 想了解什么 | 去哪里看 |
| --- | --- |
| Docker 部署、配置覆盖、命名、翻译模型与并发 | [使用指南](docs/usage.md) |
| 日志、退出码、源目录清理和失败恢复 | [运行与故障处理](docs/usage.md#失败处理与日志) |
| 本地库更新、番号匹配、演员别名和图片规则 | [元数据说明](docs/metadata.md) |
| 从 1.x / 早期 2.x 升级、旧配置迁移 | [升级指南](docs/migration.md) |
| 所有配置项及默认值 | [默认配置](config_default.yml) |
| 架构、开发测试、构建与发布 | [开发指南](docs/development.md) |

## 项目来源与许可

本仓库延续 [Yuukiy/JavSP](https://github.com/Yuukiy/JavSP)，基于 [darksoap/JavSP](https://github.com/darksoap/JavSP) 开发。

[问题反馈与功能建议](https://github.com/FlyAlpaca/JavSP/issues) · [上游 Wiki（历史参考）](https://github.com/Yuukiy/JavSP/wiki)

保留原项目及社区贡献者的署名与许可。本项目受 [GPL-3.0](./LICENSE) 与 [Anti 996 License](https://github.com/996icu/996.ICU/blob/master/LICENSE_CN) 约束。使用本项目还须接受以下约定：

- 本软件仅供学习 Python 和技术交流使用。
- 请勿在微博、微信等墙内公共社交平台宣传本项目。
- 使用时遵守当地法律法规。
- 禁止用于商业用途。
