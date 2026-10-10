# 开发指南

## 目录

| 路径 | 用途 |
| --- | --- |
| `javsp/` | 应用源码；`web/` 为动态加载的站点抓取器，`cropper/` 为裁剪后端 |
| `data/` | 运行时分类映射和演员别名，随安装包发布 |
| `image/` | README 标志、Windows 图标和运行时水印 |
| `tests/unit/` | 本地测试，网络请求由 mock 或本地 HTTP 服务提供 |
| `tests/integration/` | 在线站点测试和 Windows 可执行文件测试 |
| `tests/data/` | JSON 样本及番号识别数据，不随 wheel 发布 |
| `tools/` | 抓取样本、更新分类表和查询版本的维护脚本 |
| `docker/` | 容器构建和入口脚本 |
| `docs/` | 开发文档 |

`pyproject.toml` 管理依赖、wheel/sdist 构建及检查配置，`uv.lock` 锁定依赖。
根目录的 `setup.py` 专用于 cx_Freeze 可执行文件构建，仍需保留。
`config_default.yml` 是随包发布的默认配置；用户的 `config.yml` 放在运行目录。

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

# 在 Windows 上验证已生成的 dist/JavSP-bin.exe
uv run --locked --extra dev pytest tests/integration/test_exe.py --run-integration
```

集成测试依赖外部站点、凭据或打包产物。Windows 可执行文件测试在缺少环境或产物时跳过。
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
SVG 标志仅保留实际使用的 Roboto 字体，删除导出工具嵌入的其他字体。
`__pycache__/`、`.pytest_cache/`、`.ruff_cache/` 均为可重新生成的缓存，不应提交。
